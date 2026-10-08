"""File-source glob safety (H21).

A file source pattern such as "../../../../**/*.zzz" made root.glob() walk the
whole filesystem inside ``async def fetch_logs`` (an 8.9 s stall of the event
loop in review, a walk of / killed at 100 s), and every matched file was read
whole with read_text before the record/byte limits applied.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import adapters.file.logs as file_logs  # noqa: E402
import adapters.file.roots as file_roots  # noqa: E402
from adapters.file.logs import FileLogSource  # noqa: E402
from adapters.file.roots import PathOutsideRoots, resolve_matches  # noqa: E402
from core.selector import Entity, Limit, TimeWindow  # noqa: E402


def _root(tmp_path, files=("a.log",)):
    root = tmp_path / "root"
    root.mkdir()
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("2026-01-01T00:00:00Z INFO line one\n2026-01-01T00:00:01Z INFO line two\n")
    return root


# ── '..' never reaches glob ──────────────────────────────────────────────────

@pytest.mark.parametrize("pattern", [
    "../../../../../../../../../../**/*.zzz",
    "**/../../*.log",
    "sub/../../outside.log",
    "sub/../a.log",          # stays inside, but '..' is refused outright
    "..",
])
def test_dotdot_patterns_are_rejected(tmp_path, monkeypatch, pattern):
    root = _root(tmp_path)

    def no_glob(self, *args, **kwargs):
        raise AssertionError("glob must not run for a '..' pattern")

    monkeypatch.setattr(Path, "glob", no_glob)
    with pytest.raises(PathOutsideRoots, match=r"\.\."):
        resolve_matches(pattern, (root,))


# ── the walk and the reads run off the event loop ───────────────────────────

def test_glob_and_reads_do_not_block_the_event_loop(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real = file_roots.resolve_matches_bounded

    def slow_resolve(*args, **kwargs):
        time.sleep(0.6)  # a long filesystem walk
        return real(*args, **kwargs)

    monkeypatch.setattr(file_logs, "resolve_matches_bounded", slow_resolve)
    source = FileLogSource((str(root),))

    async def main():
        gaps = []

        async def ticker():
            last = time.monotonic()
            for _ in range(12):
                await asyncio.sleep(0.05)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.06)  # the ticker is running before the walk starts
        batch = await source.fetch_logs(Entity("*.log"), TimeWindow(), Limit())
        await tick
        return batch, max(gaps)

    batch, worst_gap = asyncio.run(main())
    assert len(batch.records) == 2
    assert worst_gap < 0.3, f"event loop stalled for {worst_gap:.2f} s"


# ── the number of matched files is capped ────────────────────────────────────

def test_match_count_is_capped_with_a_note(tmp_path, monkeypatch):
    root = _root(tmp_path, files=[f"f{i:02d}.log" for i in range(10)])
    monkeypatch.setattr(file_roots, "MAX_MATCHES", 3)

    matches, capped = file_roots.resolve_matches_bounded("*.log", (root,))
    assert len(matches) == 3 and capped

    batch = asyncio.run(FileLogSource((str(root),)).fetch_logs(Entity("*.log"), TimeWindow(), Limit()))
    assert len({r.attributes["file"] for r in batch.records}) == 3
    assert any("matching files" in note for note in batch.provenance.notes)


def test_walk_stops_at_the_cap(tmp_path, monkeypatch):
    """The glob is consumed lazily: a huge tree is not listed whole."""
    root = _root(tmp_path, files=[f"f{i:02d}.log" for i in range(40)])
    monkeypatch.setattr(file_roots, "MAX_MATCHES", 3)
    consumed = 0
    real_glob = Path.glob

    def counting_glob(self, pattern):
        nonlocal consumed
        for path in real_glob(self, pattern):
            consumed += 1
            yield path

    monkeypatch.setattr(Path, "glob", counting_glob)
    matches, capped = file_roots.resolve_matches_bounded("*.log", (root,))
    assert capped and len(matches) == 3
    assert consumed <= 4, consumed


def test_exactly_the_cap_is_not_reported_as_capped(tmp_path, monkeypatch):
    root = _root(tmp_path, files=[f"f{i:02d}.log" for i in range(3)])
    monkeypatch.setattr(file_roots, "MAX_MATCHES", 3)
    matches, capped = file_roots.resolve_matches_bounded("*.log", (root,))
    assert len(matches) == 3 and not capped


# ── files are streamed, so limits stop the read ──────────────────────────────

def test_record_limit_stops_reading_a_large_file(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    big = root / "big.log"
    with big.open("w") as fh:
        for i in range(200_000):
            fh.write(f"2026-01-01T00:00:00Z INFO line {i}\n")

    def no_read_text(self, *args, **kwargs):
        raise AssertionError("files must be streamed, not read whole")

    monkeypatch.setattr(Path, "read_text", no_read_text)
    lines_read = 0
    real_iter = file_logs._iter_lines

    def counting_iter(path):
        nonlocal lines_read
        for line in real_iter(path):
            lines_read += 1
            yield line

    monkeypatch.setattr(file_logs, "_iter_lines", counting_iter)
    batch = asyncio.run(FileLogSource((str(root),)).fetch_logs(
        Entity("big.log"), TimeWindow(), Limit(max_records=5)))

    assert [r.body.split()[-1] for r in batch.records] == ["0", "1", "2", "3", "4"]
    assert batch.provenance.truncated is True
    assert lines_read < 100, lines_read


def test_streamed_lines_split_like_splitlines(tmp_path):
    """Streaming keeps the old str.splitlines() record boundaries."""
    root = tmp_path / "root"
    root.mkdir()
    text = "one\r\ntwo\rthree\x0cfour\n\nfive"
    (root / "x.log").write_bytes(text.encode())
    assert list(file_logs._iter_lines(root / "x.log")) == text.splitlines()


def test_very_long_line_is_read_in_bounded_pieces(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(file_logs, "MAX_LINE_CHARS", 1000)
    (root / "x.log").write_text("a" * 2500 + "\nb\n")
    pieces = list(file_logs._iter_lines(root / "x.log"))
    assert pieces == ["a" * 1000, "a" * 1000, "a" * 500, "b"]
