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
    assert any("search stopped at a limit" in note for note in batch.provenance.notes)
    assert batch.provenance.truncated is True


def _count_scandir(monkeypatch):
    """Count directory listings the walker makes."""
    calls = {"n": 0}
    real_scandir = file_roots.os.scandir

    def counting(path):
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(file_roots.os, "scandir", counting)
    return calls


def test_walk_stops_at_the_cap(tmp_path, monkeypatch):
    root = _root(tmp_path, files=[f"d{i:02d}/f.log" for i in range(40)])
    monkeypatch.setattr(file_roots, "MAX_MATCHES", 3)
    calls = _count_scandir(monkeypatch)
    matches, capped = file_roots.resolve_matches_bounded("**/*.log", (root,))
    assert capped and len(matches) == 3
    assert calls["n"] <= 5, calls


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


def _outside_tree(tmp_path, dirs=30, with_files=True):
    outside = tmp_path / "outside"
    for i in range(dirs):
        (outside / f"d{i:02d}").mkdir(parents=True)
        if with_files:
            for j in range(10):
                (outside / f"d{i:02d}" / f"f{j}.zzz").write_text("x\n")
    return outside


@pytest.mark.parametrize("pattern", ["link/**/*.zzz", "*/*/*.zzz", "link/**/*.nomatch", "**/*.zzz"])
def test_symlink_out_of_the_root_is_never_entered(tmp_path, monkeypatch, pattern):
    """A symlinked directory inside the root (link -> elsewhere, even /) is
    not followed, whether or not anything there would match."""
    root = _root(tmp_path)
    (root / "link").symlink_to(_outside_tree(tmp_path), target_is_directory=True)
    calls = _count_scandir(monkeypatch)
    matches, capped = file_roots.resolve_matches_bounded(pattern, (root,))
    assert matches == [] and not capped
    assert calls["n"] <= 2, calls


def test_tree_that_matches_nothing_stops_at_the_scan_budget(tmp_path, monkeypatch):
    root = _root(tmp_path)
    for i in range(200):
        (root / "big" / f"d{i:03d}").mkdir(parents=True)
    monkeypatch.setattr(file_roots, "MAX_SCANNED", 50)
    calls = _count_scandir(monkeypatch)
    matches, capped = file_roots.resolve_matches_bounded("**/*.nomatch", (root,))
    assert matches == [] and capped
    assert calls["n"] <= 55, calls


def test_symlinked_directory_inside_the_root_is_followed(tmp_path):
    root = tmp_path / "root"
    (root / "releases" / "v2").mkdir(parents=True)
    (root / "releases" / "v2" / "app.log").write_text("x\n")
    (root / "current").symlink_to(root / "releases" / "v2", target_is_directory=True)
    matches, capped = file_roots.resolve_matches_bounded("current/*.log", (root,))
    assert [rel for _, rel in matches] == ["releases/v2/app.log"] and not capped


def test_symlink_loop_terminates(tmp_path):
    root = _root(tmp_path, files=("sub/a.log",))
    (root / "sub" / "loop").symlink_to(root, target_is_directory=True)
    matches, capped = file_roots.resolve_matches_bounded("**/*.log", (root,))
    assert sorted(rel for _, rel in matches) == ["sub/a.log"] and not capped


@pytest.mark.parametrize("pattern", [
    "*.log", "**/*.log", "sub/*.log", "**/sub/*.log", "*/*", "**/a*", "a?.log",
    "[ab]*.log", "**", "sub/**/x.log", "**/*", ".hidden/*.log", "*/deep/*.txt",
])
def test_walker_matches_path_glob_without_symlinks(tmp_path, pattern):
    root = _root(tmp_path, files=(
        "a1.log", "b.log", "c.txt", "sub/x.log", "sub/a2.log", "sub/inner/x.log",
        "other/sub/y.log", "other/deep/z.txt", ".hidden/h.log", "sub/inner/deeper/x.log"))
    expected = sorted(str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file())
    got = [rel for _, rel in resolve_matches(pattern, (root,))]
    assert got == expected


def test_fetch_runs_on_the_file_executor_with_the_callers_context(tmp_path, monkeypatch):
    import contextvars
    import threading
    marker = contextvars.ContextVar("marker", default=None)
    seen = {}
    root = _root(tmp_path)
    source = FileLogSource((str(root),))
    real = source._fetch_sync

    def spy(*args):
        seen["thread"] = threading.current_thread().name
        seen["marker"] = marker.get()
        return real(*args)

    monkeypatch.setattr(source, "_fetch_sync", spy)

    async def main():
        marker.set("from-caller")
        return await source.fetch_logs(Entity("*.log"), TimeWindow(), Limit())

    asyncio.run(main())
    assert seen["thread"].startswith("file-source")
    assert seen["marker"] == "from-caller"


def test_walker_only_descends_where_the_pattern_can_match(tmp_path, monkeypatch):
    root = _root(tmp_path, files=("sub/x.log",))
    for i in range(20):
        (root / "unrelated" / f"d{i:02d}").mkdir(parents=True)
    calls = _count_scandir(monkeypatch)
    matches, _ = file_roots.resolve_matches_bounded("sub/*.log", (root,))
    assert [rel for _, rel in matches] == ["sub/x.log"]
    assert calls["n"] == 2, calls   # the root and sub/, nothing under unrelated/
