"""Tests for src/adapters/file/logs.py — FileLogSource.

TDD order: tests written first, implementation follows.

Behaviour contract (spec §4.7 + phase-3 plan):
  - Entity-only selector: Matchers/Native raise SelectorNotSupported.
  - Per matched file (relpath-sorted): sample → detect_format → parse_line → records.
  - TimeWindow: records WITH parseable timestamps are filtered; undated records
    (timestamp=None or unparseable) are KEPT under an active window with a note.
  - Limit.max_records: head-N across the stream, provenance.truncated=True when cut.
  - Limit.max_bytes: accumulated in provenance.query["total_bytes"].
  - Empty match → empty LogBatch (no exception).
  - PathOutsideRoots propagates from resolve_matches.
  - Missing root raises at construction (FileNotFoundError from resolve(strict=True)).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.file.logs import FileLogSource
from adapters.file.roots import PathOutsideRoots
from core.selector import Entity, Limit, Matchers, Native, SelectorNotSupported, TimeWindow
from core.signals import LogBatch


# ─── helpers ─────────────────────────────────────────────────────────────────


def _run(coro):
    """Run a coroutine in an event loop (simple test helper)."""
    return asyncio.run(coro)


def _no_window() -> TimeWindow:
    return TimeWindow(start=None, end=None)


def _no_limit() -> Limit:
    return Limit(max_records=None, max_bytes=None)


# ─── construction ─────────────────────────────────────────────────────────────


def test_construction_with_missing_root_raises(tmp_path):
    """FileLogSource raises at construction when a root directory does not exist."""
    missing = tmp_path / "nonexistent_root"
    with pytest.raises(FileNotFoundError):
        FileLogSource((str(missing),))


def test_construction_with_valid_root_succeeds(tmp_path):
    """FileLogSource constructs without error when all roots exist."""
    source = FileLogSource((str(tmp_path),))
    assert source is not None


# ─── selector validation ─────────────────────────────────────────────────────


def test_matchers_selector_raises_selector_not_supported(tmp_path):
    """Matchers selector raises SelectorNotSupported with variant name in message."""
    source = FileLogSource((str(tmp_path),))
    with pytest.raises(SelectorNotSupported) as exc_info:
        _run(source.fetch_logs(Matchers(), _no_window(), _no_limit()))
    assert exc_info.value.requested == "Matchers"
    assert "Entity" in exc_info.value.supported


def test_native_selector_raises_selector_not_supported(tmp_path):
    """Native selector raises SelectorNotSupported with variant name in message."""
    source = FileLogSource((str(tmp_path),))
    with pytest.raises(SelectorNotSupported) as exc_info:
        _run(source.fetch_logs(Native(query="select *"), _no_window(), _no_limit()))
    assert exc_info.value.requested == "Native"
    assert "Entity" in exc_info.value.supported


# ─── empty match ─────────────────────────────────────────────────────────────


def test_empty_glob_returns_empty_batch(tmp_path):
    """A pattern that matches no files returns an empty LogBatch (never raises)."""
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("*.log"), _no_window(), _no_limit()))
    assert isinstance(batch, LogBatch)
    assert batch.records == []
    assert batch.provenance.adapter == "file"
    assert not batch.provenance.truncated


# ─── plain-text format ────────────────────────────────────────────────────────


def test_plain_file_records_have_none_timestamp_and_relpath(tmp_path):
    """Plain log file → records with timestamp=None and attributes['file']==relpath."""
    (tmp_path / "app.log").write_text("line one\nline two\nline three\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("app.log"), _no_window(), _no_limit()))

    assert isinstance(batch, LogBatch)
    assert len(batch.records) == 3

    bodies = [r.body for r in batch.records]
    assert "line one" in bodies
    assert "line two" in bodies
    assert "line three" in bodies

    for record in batch.records:
        assert record.timestamp is None
        assert record.attributes["file"] == "app.log"
        assert not Path(record.attributes["file"]).is_absolute()


# ─── jsonlines format ─────────────────────────────────────────────────────────


def test_jsonlines_file_records_parsed_fields_and_leftover_attrs(tmp_path):
    """Jsonlines file → structured fields (timestamp/severity/body) + leftover in attributes."""
    line = json.dumps({
        "ts": "2024-01-01T12:00:00Z",
        "level": "info",
        "message": "server started",
        "host": "node-1",
        "pid": 42,
    })
    # Need >= 60% JSON lines for jsonlines detection; use 4 json lines + 0 plain
    lines = [line] * 4
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("app.log"), _no_window(), _no_limit()))

    assert len(batch.records) == 4
    r = batch.records[0]
    assert r.timestamp == "2024-01-01T12:00:00Z"
    assert r.severity == "info"
    assert r.body == "server started"
    assert r.attributes["host"] == "node-1"
    assert r.attributes["pid"] == 42
    assert r.attributes["file"] == "app.log"
    # Consumed keys must NOT appear in attributes
    assert "ts" not in r.attributes
    assert "level" not in r.attributes
    assert "message" not in r.attributes


# ─── klog format ─────────────────────────────────────────────────────────────


def test_klog_file_records_severity_and_body(tmp_path):
    """klog file → severity mapped (I→INFO, W→WARNING, E→ERROR, F→CRITICAL) + body extracted."""
    klog_lines = [
        "I0101 12:00:00.000000 1234 main.go:10] server started",
        "W0202 08:00:00.000000 5678 foo.go:99] slow response",
        "E0303 09:00:00.000000 9999 bar.go:42] connection refused",
    ]
    (tmp_path / "kube.log").write_text("\n".join(klog_lines) + "\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("kube.log"), _no_window(), _no_limit()))

    assert len(batch.records) == 3
    severities = {r.severity for r in batch.records}
    assert severities == {"INFO", "WARNING", "ERROR"}
    bodies = [r.body for r in batch.records]
    assert any("server started" in b for b in bodies)
    assert any("slow response" in b for b in bodies)
    assert any("connection refused" in b for b in bodies)
    for r in batch.records:
        assert r.timestamp is not None, "klog records must have a timestamp"
        assert r.attributes["file"] == "kube.log"


# ─── glob across two files: relpath-sorted order ──────────────────────────────


def test_glob_two_files_records_in_relpath_sorted_order(tmp_path):
    """Glob matching two files → records interleaved in relpath-sorted file order."""
    # a.log < b.log alphabetically → a's records come before b's
    (tmp_path / "a.log").write_text("a-line-1\na-line-2\n")
    (tmp_path / "b.log").write_text("b-line-1\nb-line-2\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("*.log"), _no_window(), _no_limit()))

    assert len(batch.records) == 4
    file_seq = [r.attributes["file"] for r in batch.records]
    # All a.log records must precede all b.log records
    assert file_seq == ["a.log", "a.log", "b.log", "b.log"], (
        f"expected relpath-sorted order; got {file_seq}"
    )


# ─── provenance ───────────────────────────────────────────────────────────────


def test_provenance_adapter_name_is_file(tmp_path):
    """Provenance.adapter is 'file' for all FileLogSource batches."""
    (tmp_path / "x.log").write_text("hello\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("x.log"), _no_window(), _no_limit()))
    assert batch.provenance.adapter == "file"


# ─── TimeWindow filtering ─────────────────────────────────────────────────────


def test_timewindow_filters_dated_keeps_undated_with_note(tmp_path):
    """Active TimeWindow: in-window dated kept, out-of-window dated filtered,
    undated records kept with a note in provenance.notes."""
    # Window: 2024-01-01 11:00 to 15:00 UTC
    window = TimeWindow(
        start=datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc),
        end=datetime(2024, 1, 1, 15, 0, 0, tzinfo=timezone.utc),
    )
    # 3 JSON lines (60% of 5 → detected as jsonlines) + 2 plain fallback lines
    in1 = json.dumps({"ts": "2024-01-01T12:00:00+00:00", "message": "in-window-1"})
    in2 = json.dumps({"ts": "2024-01-01T14:00:00+00:00", "message": "in-window-2"})
    out1 = json.dumps({"ts": "2024-01-01T10:00:00+00:00", "message": "out-of-window"})
    # These lines fail JSON parse → plain fallback → timestamp=None → undated
    undated1 = "plain undated record one"
    undated2 = "another undated line"

    content = "\n".join([in1, in2, out1, undated1, undated2]) + "\n"
    (tmp_path / "mixed.log").write_text(content)

    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("mixed.log"), window, _no_limit()))

    bodies = [r.body for r in batch.records]

    # In-window dated records must be present
    assert "in-window-1" in bodies
    assert "in-window-2" in bodies
    # Out-of-window dated record must be filtered OUT
    assert "out-of-window" not in bodies
    # Undated records must be KEPT
    assert "plain undated record one" in bodies
    assert "another undated line" in bodies
    # Total: 4 records (2 dated in-window + 2 undated)
    assert len(batch.records) == 4
    # A note about undated records must appear
    assert any("undated" in note.lower() for note in batch.provenance.notes), (
        f"expected note about undated records; got notes={batch.provenance.notes!r}"
    )


def test_null_timewindow_keeps_all_records(tmp_path):
    """TimeWindow(None, None) is inactive → all records kept regardless of timestamp."""
    lines = [
        json.dumps({"ts": "2024-01-01T12:00:00+00:00", "message": "dated"}),
        "plain undated",
    ]
    # 1 of 2 lines is JSON (50 %) → detected as plain → everything body-only
    (tmp_path / "a.log").write_text("\n".join(lines) + "\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(source.fetch_logs(Entity("a.log"), _no_window(), _no_limit()))
    assert len(batch.records) == 2


# ─── Limit: max_records ───────────────────────────────────────────────────────


def test_max_records_truncates_stream_and_sets_truncated_flag(tmp_path):
    """max_records=2 with a 5-line file: first 2 records returned, truncated=True."""
    lines = [f"line {i}" for i in range(5)]
    (tmp_path / "big.log").write_text("\n".join(lines) + "\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(
        source.fetch_logs(Entity("big.log"), _no_window(), Limit(max_records=2))
    )
    assert len(batch.records) == 2
    assert batch.records[0].body == "line 0"
    assert batch.records[1].body == "line 1"
    assert batch.provenance.truncated is True


def test_max_records_exact_fit_no_truncation(tmp_path):
    """max_records == number of records in file → truncated=False (nothing cut)."""
    lines = ["line a", "line b", "line c"]
    (tmp_path / "exact.log").write_text("\n".join(lines) + "\n")
    source = FileLogSource((str(tmp_path),))
    batch = _run(
        source.fetch_logs(Entity("exact.log"), _no_window(), Limit(max_records=3))
    )
    assert len(batch.records) == 3
    assert batch.provenance.truncated is False


def test_max_records_head_n_across_two_files(tmp_path):
    """max_records applies across all files (not per-file): head-N of the sorted stream."""
    (tmp_path / "a.log").write_text("a1\na2\na3\n")
    (tmp_path / "b.log").write_text("b1\nb2\nb3\n")
    source = FileLogSource((str(tmp_path),))
    # Limit to 4 → gets a1, a2, a3 from a.log and b1 from b.log
    batch = _run(
        source.fetch_logs(Entity("*.log"), _no_window(), Limit(max_records=4))
    )
    assert len(batch.records) == 4
    bodies = [r.body for r in batch.records]
    assert bodies == ["a1", "a2", "a3", "b1"]
    assert batch.provenance.truncated is True


# ─── security: PathOutsideRoots propagates ───────────────────────────────────


def test_escape_pattern_propagates_path_outside_roots(tmp_path):
    """An exact non-glob pattern that escapes the root propagates PathOutsideRoots."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    outside = tmp_path / "secret.log"
    outside.write_text("sensitive data")

    source = FileLogSource((str(root_dir),))
    with pytest.raises(PathOutsideRoots):
        _run(source.fetch_logs(Entity("../secret.log"), _no_window(), _no_limit()))
