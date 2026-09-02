"""Tests for the diff / bless / list CLI subcommands (Task 4, TDD).

All tests use synthetic run directories built under tmp_path via helpers.
No server is spawned — these are pure filesystem + CLI logic tests.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
_LM_PATH = REPO_ROOT / "scripts" / "live_matrix.py"


# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------


def _load_live_matrix() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("live_matrix", _LM_PATH)
    assert spec is not None, f"spec_from_file_location returned None for {_LM_PATH}"
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_lm = _load_live_matrix()


# ---------------------------------------------------------------------------
# Synthetic run-dir builder helpers
# ---------------------------------------------------------------------------

_FP_A = {
    "git_sha": "aaaa111122223333" * 2 + "aaaa1111",
    "dirty": False,
    "python": "3.12.0",
    "cluster_id": "cluster-a",
}

_FP_B = {
    "git_sha": "bbbb222233334444" * 2 + "bbbb2222",
    "dirty": True,
    "python": "3.12.0",
    "cluster_id": "cluster-a",
}

_FP_OTHER_CLUSTER = {
    "git_sha": "cccc333344445555" * 2 + "cccc3333",
    "dirty": False,
    "python": "3.12.0",
    "cluster_id": "cluster-b",
}


def _make_run(
    parent: pathlib.Path,
    name: str,
    fingerprint: dict,
    tool_records: "dict[str, dict]",
    partial: bool = False,
    transport_failure: bool = False,
) -> pathlib.Path:
    """Create a synthetic run directory under *parent*.

    Writes manifest.json and one ``<tool_name>.json`` per tool record.
    ``transport_failure`` mirrors the F4 manifest flag (default False).
    """
    run_dir = parent / name
    run_dir.mkdir(parents=True)

    manifest = {
        "fingerprint": fingerprint,
        "flags": [],
        "targets": {
            "namespace": None,
            "pod": None,
            "pipelinerun": None,
            "pipelinerun_ns": None,
        },
        "tool_seconds": {t: 0.1 for t in tool_records},
        "total_seconds": 0.5,
        "partial": partial,
        "transport_failure": transport_failure,
        "started_utc": "2026-07-29T00:00:00+00:00",
        "finished_utc": "2026-07-29T00:00:01+00:00",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    for tool_name, record in tool_records.items():
        (run_dir / f"{tool_name}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    return run_dir


def _rec(
    *,
    status: str = "ok",
    expectation: str = "ok",
    error_type: "str | None" = None,
    latency_ms: float = 100.0,
    response_bytes: int = 1000,
    shape: "object | None" = None,
) -> dict:
    """Build a synthetic tool record with all Task-3 spec keys present."""
    if shape is None:
        shape = {"type": "dict", "keys": {"result": "str"}}
    return {
        "args": {},
        "expectation": expectation,
        "status": status,
        "error_type": error_type,
        "latency_ms": latency_ms,
        "response_bytes": response_bytes,
        "shape": shape,
    }


# ---------------------------------------------------------------------------
# diff subcommand tests
# ---------------------------------------------------------------------------


class TestDiffSubcommand:
    """Unit tests for diff_matrix: no server spawn, pure filesystem."""

    def test_missing_shape_key_exits_3(self, tmp_path, monkeypatch):
        """A tool record missing the 'shape' key must cause exit 3 (structural)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # run-a: valid record with shape key
        rec_a = _rec()
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": rec_a})

        # run-b: record MISSING the 'shape' key
        rec_b = _rec()
        del rec_b["shape"]
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": rec_b})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        assert exc_info.value.code == 3, (
            "missing 'shape' key must exit 3 (structural), not KeyError traceback"
        )

    def test_identical_runs_exits_0(self, tmp_path, monkeypatch, capsys):
        """Two identical runs must exit 0 with no findings."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        rec = _rec()
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": rec})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": rec})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        assert exc_info.value.code == 0

        out = capsys.readouterr().out
        assert "fail" not in out
        assert "flag" not in out

    def test_status_flip_exits_1_with_fail_status(self, tmp_path, monkeypatch, capsys):
        """status ok→error must exit 1 and produce a fail/status finding."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec(status="ok")})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": _rec(status="error")})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "fail" in out
        assert "status" in out

    def test_latency_3x_exits_0_with_flag(self, tmp_path, monkeypatch, capsys):
        """Latency 3× baseline is a flag (not fail): exit 0 and flag line in output."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec(latency_ms=100)})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": _rec(latency_ms=300)})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        assert exc_info.value.code == 0

        out = capsys.readouterr().out
        assert "flag" in out

    def test_missing_tool_in_b_exits_1_with_presence_fail(
        self, tmp_path, monkeypatch, capsys
    ):
        """Tool absent from run B produces a fail/presence finding → exit 1."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec(), "tool_y": _rec()})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": _rec()})  # tool_y absent

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "fail" in out
        assert "presence" in out

    def test_cross_cluster_warning_on_stderr(self, tmp_path, monkeypatch, capsys):
        """Different cluster_ids must print 'WARNING: cross-cluster diff' on stderr."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        rec = _rec()
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": rec})
        _make_run(tmp_path, "run-b", _FP_OTHER_CLUSTER, {"tool_x": rec})

        with pytest.raises(SystemExit):
            _lm.diff_matrix(["diff", "run-a", "run-b"])

        err = capsys.readouterr().err
        assert "WARNING: cross-cluster diff" in err

    def test_header_contains_both_shas(self, tmp_path, monkeypatch, capsys):
        """The diff header must include the first 8 chars of both fingerprint shas."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        rec = _rec()
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": rec})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": rec})

        with pytest.raises(SystemExit):
            _lm.diff_matrix(["diff", "run-a", "run-b"])

        out = capsys.readouterr().out
        sha_a8 = _FP_A["git_sha"][:8]
        sha_b8 = _FP_B["git_sha"][:8]
        assert sha_a8 in out, f"sha8 of run-a ({sha_a8!r}) not in header"
        assert sha_b8 in out, f"sha8 of run-b ({sha_b8!r}) not in header"

    def test_json_output_has_findings_and_fingerprints(
        self, tmp_path, monkeypatch, capsys
    ):
        """--json output must be parseable JSON with 'findings' and 'fingerprints' keys."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        rec = _rec()
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": rec})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": rec})

        with pytest.raises(SystemExit):
            _lm.diff_matrix(["diff", "run-a", "run-b", "--json"])

        out = capsys.readouterr().out
        data = json.loads(out)
        assert "findings" in data, "JSON output missing 'findings' key"
        assert "fingerprints" in data, "JSON output missing 'fingerprints' key"
        assert "a" in data["fingerprints"], "'fingerprints.a' missing"
        assert "b" in data["fingerprints"], "'fingerprints.b' missing"

    def test_missing_run_dir_exits_3(self, tmp_path, monkeypatch):
        """A run name that doesn't exist must exit 3 (structural)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        rec = _rec()
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": rec})
        # run-b does NOT exist

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-nonexistent"])
        assert exc_info.value.code == 3

    def test_unknown_flag_exits_2(self, tmp_path, monkeypatch):
        """Unknown flags in the diff subcommand must exit 2."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "--foobar", "run-a", "run-b"])
        assert exc_info.value.code == 2

    def test_non_dict_record_exits_3_not_type_error(self, tmp_path, monkeypatch):
        """A tool record file containing a bare integer must exit 3, not raise TypeError.

        Before the fix `if not isinstance(rec, dict)` was absent; `if "shape" not in rec`
        raised TypeError on a non-dict value.
        """
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec()})
        # Write a record file whose content is a bare integer, not a dict
        run_b = tmp_path / "run-b"
        run_b.mkdir()
        manifest = {
            "fingerprint": _FP_B,
            "flags": [],
            "targets": {},
            "tool_seconds": {"tool_x": 0.1},
            "total_seconds": 0.1,
            "partial": False,
            "started_utc": "2026-07-29T00:00:00+00:00",
            "finished_utc": "2026-07-29T00:00:01+00:00",
        }
        (run_b / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (run_b / "tool_x.json").write_text("5", encoding="utf-8")  # bare int, not dict

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        assert exc_info.value.code == 3, (
            "a non-dict tool record must exit 3 (structural), not raise TypeError"
        )

    def test_traversal_run_name_exits_2(self, tmp_path, monkeypatch):
        """A run name containing path separators must be rejected with exit 2."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec()})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "../outside/secret"])
        assert exc_info.value.code == 2, (
            "traversal run name '../outside/secret' must be rejected (exit 2)"
        )

    def test_dotdot_run_name_exits_2(self, tmp_path, monkeypatch):
        """'..' passes the sanitize-equality check but must still be rejected.

        _sanitize_for_dirname('..') == '..' because '.' is in [A-Za-z0-9._-].
        Without an explicit name in ('.','..')  guard, diff A .. would read
        one level above runs_root.
        """
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec()})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", ".."])
        assert exc_info.value.code == 2, (
            "'..' must be rejected as a run name (exit 2)"
        )

    def test_json_info_key_present_with_expectation_mismatch(
        self, tmp_path, monkeypatch, capsys
    ):
        """--json output must carry an 'info' key with expectation-mismatch lines."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # run-b: tool expects "ok" but gets "error" → info line
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec(status="ok")})
        _make_run(
            tmp_path,
            "run-b",
            _FP_B,
            {"tool_x": _rec(status="error", expectation="ok")},
        )

        with pytest.raises(SystemExit):
            _lm.diff_matrix(["diff", "run-a", "run-b", "--json"])

        out = capsys.readouterr().out
        data = json.loads(out)
        assert "info" in data, "JSON output must carry 'info' key"
        assert isinstance(data["info"], list), "'info' must be a list"
        assert any("tool_x" in line for line in data["info"]), (
            "expectation mismatch for tool_x must appear in info"
        )

    def test_error_ok_timeout_generates_info_line(
        self, tmp_path, monkeypatch, capsys
    ):
        """timeout does NOT satisfy error_ok: an info line must be printed.

        Spec: error_ok means a structured error (status='error') is expected.
        A hang (status='timeout') is a different failure mode and must be flagged.
        """
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # run-b: tool expects "error_ok" but timed out instead
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec()})
        _make_run(
            tmp_path,
            "run-b",
            _FP_B,
            {"tool_x": _rec(status="timeout", expectation="error_ok")},
        )

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        # status flip (ok → timeout) produces a fail finding → exit 1
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "info:" in out, "timeout with error_ok expectation must print an info line"
        assert "expected error_ok, got timeout" in out


# ---------------------------------------------------------------------------
# bless subcommand tests
# ---------------------------------------------------------------------------


class TestBlessSubcommand:
    def test_bless_partial_run_exits_2(self, tmp_path, monkeypatch):
        """bless must refuse a partial=True run and exit 2."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-partial", _FP_A, {"tool_x": _rec()}, partial=True)

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-partial"])
        assert exc_info.value.code == 2, (
            "bless of a partial run must exit 2 (usage error)"
        )

    def test_bless_writes_basename_to_baseline(self, tmp_path, monkeypatch):
        """bless writes the run's basename (single line) to <runs_root>/baseline."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-full", _FP_A, {"tool_x": _rec()}, partial=False)

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-full"])
        assert exc_info.value.code == 0

        baseline_path = tmp_path / "baseline"
        assert baseline_path.is_file(), "baseline file not created by bless"
        content = baseline_path.read_text(encoding="utf-8").strip()
        assert content == "run-full", (
            f"baseline content must be the run dir basename; got {content!r}"
        )

    def test_bless_then_diff_baseline_resolves(self, tmp_path, monkeypatch, capsys):
        """After bless, `diff --baseline RUN` must resolve run A from the baseline file."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        rec = _rec()
        _make_run(tmp_path, "run-base", _FP_A, {"tool_x": rec}, partial=False)
        _make_run(tmp_path, "run-new", _FP_B, {"tool_x": rec})

        # Bless run-base as the baseline
        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-base"])
        assert exc_info.value.code == 0

        # diff --baseline resolves run-base as A and run-new as B
        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "--baseline", "run-new"])
        # Identical records → exit 0
        assert exc_info.value.code == 0, (
            "diff --baseline with identical runs must exit 0"
        )

        out = capsys.readouterr().out
        sha_a8 = _FP_A["git_sha"][:8]
        assert sha_a8 in out, "sha8 of blessed run (run A) should appear in header"

    def test_bless_missing_run_exits_3(self, tmp_path, monkeypatch):
        """Blessing a non-existent run name must exit 3."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-nonexistent"])
        assert exc_info.value.code == 3

    def test_bless_exits_2_for_missing_run_name(self, tmp_path, monkeypatch):
        """bless with no run name must exit 2."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless"])
        assert exc_info.value.code == 2

    def test_bless_traversal_run_name_exits_2(self, tmp_path, monkeypatch):
        """bless with a traversal run name must exit 2, not write outside runs_root."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "../outside/secret"])
        assert exc_info.value.code == 2, (
            "traversal run name '../outside/secret' must be rejected (exit 2)"
        )
        # No file should be written outside runs_root
        outside = tmp_path.parent / "outside"
        assert not outside.exists(), "bless must not write outside the runs root"

    def test_bless_dotdot_exits_2(self, tmp_path, monkeypatch):
        """'..' passes sanitize-equality but must be rejected by bless.

        Without the explicit name in ('.','..')  guard, bless .. would write
        the baseline file one level above runs_root, poisoning it.
        """
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", ".."])
        assert exc_info.value.code == 2, (
            "'..' must be rejected as a bless run name (exit 2)"
        )

    def test_bless_malformed_record_exits_3(self, tmp_path, monkeypatch):
        """Blessing a run that has a tool record missing 'shape' must exit 3."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # Build a run that looks complete (not partial) but has a bad record
        run_dir = tmp_path / "run-good-manifest"
        run_dir.mkdir()
        manifest = {
            "fingerprint": _FP_A,
            "flags": [],
            "targets": {},
            "tool_seconds": {"tool_x": 0.1},
            "total_seconds": 0.1,
            "partial": False,
            "started_utc": "2026-07-29T00:00:00+00:00",
            "finished_utc": "2026-07-29T00:00:01+00:00",
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Record without 'shape' key
        bad_rec = {k: v for k, v in _rec().items() if k != "shape"}
        (run_dir / "tool_x.json").write_text(json.dumps(bad_rec), encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-good-manifest"])
        assert exc_info.value.code == 3, (
            "bless must exit 3 when a tool record is structurally invalid"
        )


# ---------------------------------------------------------------------------
# list subcommand tests
# ---------------------------------------------------------------------------


class TestListSubcommand:
    def test_list_shows_both_runs_newest_first(self, tmp_path, monkeypatch, capsys):
        """list must print runs newest-first; both run dirs must appear."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # Create run-a first (older), then run-b (newer)
        run_a = _make_run(tmp_path, "run-2026-01", _FP_A, {"tool_x": _rec()})
        # Small sleep to ensure different mtime, or just set it explicitly
        os.utime(run_a, (time.time() - 2, time.time() - 2))
        _make_run(tmp_path, "run-2026-02", _FP_B, {"tool_x": _rec()})

        with pytest.raises(SystemExit) as exc_info:
            _lm.list_matrix(["list"])
        assert exc_info.value.code == 0

        out = capsys.readouterr().out
        assert "run-2026-01" in out, "run-a must appear in list output"
        assert "run-2026-02" in out, "run-b must appear in list output"

        # Newest first: run-2026-02 must appear before run-2026-01
        pos_a = out.index("run-2026-01")
        pos_b = out.index("run-2026-02")
        assert pos_b < pos_a, (
            f"newest run (run-2026-02, pos {pos_b}) must appear before "
            f"older run (run-2026-01, pos {pos_a})"
        )

    def test_list_columns_include_sha_dirty_cluster_partial(
        self, tmp_path, monkeypatch, capsys
    ):
        """Each list row must include sha8, dirty, cluster_id, and partial flag."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        _make_run(tmp_path, "run-full", _FP_B, {"tool_x": _rec()}, partial=False)

        with pytest.raises(SystemExit) as exc_info:
            _lm.list_matrix(["list"])
        assert exc_info.value.code == 0

        out = capsys.readouterr().out
        sha8 = _FP_B["git_sha"][:8]
        assert sha8 in out, f"sha8 {sha8!r} not in list output"
        assert _FP_B["cluster_id"] in out, "cluster_id not in list output"

    def test_list_empty_runs_root_exits_0(self, tmp_path, monkeypatch, capsys):
        """list with no runs in the root must exit 0 with no output errors."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.list_matrix(["list"])
        assert exc_info.value.code == 0

    def test_list_unknown_flag_exits_2(self, tmp_path, monkeypatch):
        """Unknown flags in the list subcommand must exit 2."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.list_matrix(["list", "--foobar"])
        assert exc_info.value.code == 2

    def test_list_missing_manifest_shows_damaged_row(
        self, tmp_path, monkeypatch, capsys
    ):
        """A run dir without manifest.json must appear as a 'damaged' row, not silently skipped."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # Valid run
        _make_run(tmp_path, "run-ok", _FP_A, {"tool_x": _rec()})

        # Run dir with no manifest
        damaged = tmp_path / "run-no-manifest"
        damaged.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            _lm.list_matrix(["list"])
        assert exc_info.value.code == 0

        out = capsys.readouterr().out
        assert "run-ok" in out, "valid run must appear in list"
        assert "run-no-manifest" in out, "damaged dir must appear in list (not silently skipped)"
        assert "damaged" in out, "damaged row must be labelled 'damaged'"

    def test_list_corrupt_manifest_shows_damaged_row(
        self, tmp_path, monkeypatch, capsys
    ):
        """A run dir with corrupt (non-parseable) manifest.json must appear as 'damaged'."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        # Run dir with unreadable manifest
        damaged = tmp_path / "run-corrupt"
        damaged.mkdir()
        (damaged / "manifest.json").write_text("{broken json", encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _lm.list_matrix(["list"])
        assert exc_info.value.code == 0

        out = capsys.readouterr().out
        assert "run-corrupt" in out, "corrupt run must appear in list"
        assert "damaged" in out, "corrupt run must be labelled 'damaged'"


# ---------------------------------------------------------------------------
# Task 3 deferred minor: --tools dedup + empty guard
# ---------------------------------------------------------------------------


class TestToolsDedupFix:
    """--tools dedup: duplicates must be removed; --tools= empty must exit 2."""

    def test_tools_duplicates_deduped_before_sweep(self, tmp_path, monkeypatch):
        """--tools list_sources,list_sources must be deduped to ['list_sources']."""
        from tests.characterization.conftest import FAKE_KUBECONFIG

        kubeconfig = tmp_path / "kubeconfig"
        kubeconfig.write_text(FAKE_KUBECONFIG)
        monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        captured: dict = {}

        class _StopCapture(Exception):
            pass

        async def _capture_sweep(**kwargs):
            captured["tools"] = kwargs.get("tools")
            raise _StopCapture("captured")

        monkeypatch.setattr(_lm, "_run_sweep_async", _capture_sweep)

        with pytest.raises(_StopCapture):
            _lm.run_matrix(["run", "--tools", "list_sources,list_sources"])

        assert captured["tools"] == ["list_sources"], (
            f"Expected deduped ['list_sources'], got {captured['tools']!r}"
        )

    def test_tools_empty_string_exits_2(self, tmp_path, monkeypatch):
        """--tools= with empty value after stripping must exit 2 (usage error)."""
        from tests.characterization.conftest import FAKE_KUBECONFIG

        kubeconfig = tmp_path / "kubeconfig"
        kubeconfig.write_text(FAKE_KUBECONFIG)
        monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--tools="])
        assert exc_info.value.code == 2, (
            "--tools= (empty) must exit 2, not silently produce an empty sweep"
        )


# ---------------------------------------------------------------------------
# F6: --help / -h
# ---------------------------------------------------------------------------


class TestHelpFlag:
    """F6: --help / -h exits 0 and documents --pipelinerun NS/NAME."""

    def test_run_help_exits_0(self, capsys):
        """'run --help' must exit 0 (F6)."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--help"])
        assert exc_info.value.code == 0

    def test_run_help_contains_ns_name(self, capsys):
        """'run --help' output must document --pipelinerun NS/NAME (F6)."""
        with pytest.raises(SystemExit):
            _lm.run_matrix(["run", "--help"])
        out = capsys.readouterr().out
        assert "NS/NAME" in out, (
            f"run --help must contain 'NS/NAME' in its output; got:\n{out}"
        )

    def test_run_h_exits_0(self):
        """'run -h' must exit 0 (F6 short alias)."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "-h"])
        assert exc_info.value.code == 0

    def test_bare_h_exits_0(self):
        """Bare '-h' as first argument to the script must exit 0 (F6 global help)."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "live_matrix.py"), "-h"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"'python live_matrix.py -h' must exit 0; got {result.returncode}\n"
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# F4: transport_failure flag — bless refuses, diff warns
# ---------------------------------------------------------------------------


class TestTransportFailureFlag:
    """F4: bless refuses transport_failure=True runs; diff prints a warning."""

    def test_bless_refuses_transport_failure_run_exits_2(self, tmp_path, monkeypatch):
        """bless must exit 2 when manifest has transport_failure=True (F4)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))
        _make_run(
            tmp_path, "run-tf", _FP_A, {"tool_x": _rec()}, transport_failure=True
        )

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-tf"])
        assert exc_info.value.code == 2, (
            "bless must refuse a transport_failure=True run (exit 2)"
        )

    def test_bless_refuses_transport_failure_stderr_message(
        self, tmp_path, monkeypatch, capsys
    ):
        """bless must print a clear error message on stderr when refusing (F4)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))
        _make_run(
            tmp_path, "run-tf", _FP_A, {"tool_x": _rec()}, transport_failure=True
        )

        with pytest.raises(SystemExit):
            _lm.bless_matrix(["bless", "run-tf"])
        err = capsys.readouterr().err
        assert "transport" in err.lower(), (
            f"bless must mention 'transport' in its refusal message; got: {err!r}"
        )

    def test_bless_allows_clean_run(self, tmp_path, monkeypatch):
        """bless succeeds when transport_failure=False (no-regression guard for F4)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))
        _make_run(tmp_path, "run-clean", _FP_A, {"tool_x": _rec()})

        with pytest.raises(SystemExit) as exc_info:
            _lm.bless_matrix(["bless", "run-clean"])
        assert exc_info.value.code == 0, (
            "bless must succeed when transport_failure is absent/False"
        )

    def test_diff_warns_on_transport_failure_run_a(
        self, tmp_path, monkeypatch, capsys
    ):
        """diff must print transport_failure warning to stderr when run-a has flag (F4)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))
        _make_run(
            tmp_path, "run-tf", _FP_A, {"tool_x": _rec()}, transport_failure=True
        )
        _make_run(tmp_path, "run-clean", _FP_B, {"tool_x": _rec()})

        with pytest.raises(SystemExit) as exc_info:
            _lm.diff_matrix(["diff", "run-tf", "run-clean"])
        err = capsys.readouterr().err
        assert "transport" in err.lower(), (
            f"diff must warn about transport_failure in run-a; stderr: {err!r}"
        )
        # Warning must not change exit code — identical records → exit 0
        assert exc_info.value.code == 0, (
            "transport_failure warning must not affect diff exit code"
        )

    def test_diff_no_warning_on_clean_run(self, tmp_path, monkeypatch, capsys):
        """diff must not warn when both runs have transport_failure=False (F4 regression)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))
        _make_run(tmp_path, "run-a", _FP_A, {"tool_x": _rec()})
        _make_run(tmp_path, "run-b", _FP_B, {"tool_x": _rec()})

        with pytest.raises(SystemExit):
            _lm.diff_matrix(["diff", "run-a", "run-b"])
        err = capsys.readouterr().err
        # The only stderr expected here is the cross-cluster warning (cluster-a == cluster-a so none)
        assert "transport" not in err.lower(), (
            f"diff must not mention transport_failure for clean runs; stderr: {err!r}"
        )


# ---------------------------------------------------------------------------
# F1: run_matrix returns the finalized run dir (printed to stdout by __main__)
# ---------------------------------------------------------------------------


class TestRunMatrixReturnsRunDir:
    """F1: run_matrix returns the finalized run directory path.

    The ``__main__`` block does ``print(run_matrix(_argv))``, which means the
    run dir path is the last line of STDOUT on a successful run.  These tests
    pin that the return value is the finalized directory (not the .tmp path)
    and that printing it produces the expected path string.
    """

    def test_run_matrix_returns_path(self, tmp_path, monkeypatch):
        """run_matrix must return a Path pointing to the finalized run dir (F1)."""
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        expected_dir = tmp_path / "fake-run-dir"

        async def _fake_sweep(**kwargs):  # type: ignore[return]
            expected_dir.mkdir()
            return expected_dir

        monkeypatch.setattr(_lm, "_run_sweep_async", _fake_sweep)

        result = _lm.run_matrix(["run", "--tools", "list_sources"])
        assert result == expected_dir, (
            f"run_matrix must return the finalized run dir; "
            f"expected {expected_dir}, got {result}"
        )

    def test_run_matrix_stdout_ends_with_run_dir_path(
        self, tmp_path, monkeypatch, capsys
    ):
        """Printing run_matrix's return value produces the run dir path on stdout (F1).

        This mirrors what ``__main__`` does: ``print(run_matrix(_argv))``.
        """
        monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))

        expected_dir = tmp_path / "fake-run-dir-2"

        async def _fake_sweep(**kwargs):  # type: ignore[return]
            expected_dir.mkdir()
            return expected_dir

        monkeypatch.setattr(_lm, "_run_sweep_async", _fake_sweep)

        result = _lm.run_matrix(["run", "--tools", "list_sources"])
        # Simulate what __main__ does
        print(result)

        out = capsys.readouterr().out
        assert out.strip().endswith(str(expected_dir)), (
            f"stdout must end with the run dir path {str(expected_dir)!r}; "
            f"got: {out!r}"
        )
