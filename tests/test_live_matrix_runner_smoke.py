"""Smoke and unit tests for the live_matrix runner (Task 3 + fix round 1).

Tests in ``TestRunnerSmokeExplicitTools`` spawn the real MCP server and are
marked ``@pytest.mark.slow``.  Other classes are unit/fast tests that verify
arg parsing, collision logic, child-env pinning, and parity-mismatch exit
without spawning a subprocess.

Import via ``spec_from_file_location`` for module isolation (consistent with
``test_live_matrix_core.py`` and ``test_live_matrix_catalog.py``).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pathlib
import subprocess
import types
from typing import Any

import pytest

import mcp.client.stdio as _mcp_stdio  # imported here so monkeypatch can reach it

from tests.characterization.conftest import FAKE_KUBECONFIG

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parent.parent
_LM_PATH = REPO_ROOT / "scripts" / "live_matrix.py"


def _load_live_matrix() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("live_matrix", _LM_PATH)
    assert spec is not None, f"spec_from_file_location returned None for {_LM_PATH}"
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_lm = _load_live_matrix()


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

EXPLICIT_TOOLS = "list_sources,refresh_capabilities,connect_cluster"
EXPLICIT_TOOLS_SET = set(EXPLICIT_TOOLS.split(","))
VALID_STATUSES = {"ok", "error", "timeout", "skipped"}


def _setup_env(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """Write FAKE_KUBECONFIG and set KUBECONFIG + LIVE_MATRIX_ROOT."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(FAKE_KUBECONFIG)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv("LIVE_MATRIX_ROOT", str(tmp_path))
    return kubeconfig


# ---------------------------------------------------------------------------
# Arg-parsing unit tests (F-3, F-5) — no server spawn, no @pytest.mark.slow
# ---------------------------------------------------------------------------


class TestArgParsing:
    """Strict arg parsing: unknown flags / missing values exit 2 (F-5)."""

    def test_unknown_flag_exits_2(self):
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--foo", "bar"])
        assert exc_info.value.code == 2

    def test_unknown_flag_typo_exits_2(self):
        """--tool (missing 's') must be rejected, not silently ignored."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--tool", "list_sources"])
        assert exc_info.value.code == 2

    def test_tools_missing_value_exits_2(self):
        """--tools at end of argv with no value must exit 2."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--tools"])
        assert exc_info.value.code == 2

    def test_timeout_missing_value_exits_2(self):
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--timeout"])
        assert exc_info.value.code == 2

    def test_unexpected_positional_exits_2(self):
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "unexpected-positional"])
        assert exc_info.value.code == 2

    def test_timeout_float_accepted_via_parser(self, tmp_path, monkeypatch):
        """float(val) must not raise; 0.5 used to crash with int()."""
        # We can't call run_matrix all the way through (it would spawn a server),
        # but we can verify that 0.5 is accepted without a parse error by checking
        # it doesn't exit 2 — we expect it to proceed (and then stop at spawn).
        # A _StopCapture mock lets us inspect without needing a real server.
        _setup_env(tmp_path, monkeypatch)
        captured: dict = {}

        class _StopCapture(Exception):
            pass

        @contextlib.asynccontextmanager
        async def _mock_stdio_client(params):
            captured["reached_spawn"] = True
            raise _StopCapture("stop before spawning")
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _mock_stdio_client)

        with pytest.raises(_StopCapture):
            _lm.run_matrix(["run", "--tools", "list_sources", "--timeout", "0.5"])

        assert captured.get("reached_spawn"), "--timeout 0.5 should not exit 2"


# ---------------------------------------------------------------------------
# Collision unit tests (F-8) — pure filesystem, no server spawn
# ---------------------------------------------------------------------------


class TestCollisionUniqueness:
    """_make_unique_dirs must append -2, -3, … when the base name is taken."""

    def test_no_collision_returns_base_name(self, tmp_path):
        tmp_d, final_d = _lm._make_unique_dirs(tmp_path, "20260729-120000-fake")
        assert final_d == tmp_path / "20260729-120000-fake"
        assert tmp_d == tmp_path / "20260729-120000-fake.tmp"

    def test_single_collision_uses_suffix_2(self, tmp_path):
        (tmp_path / "20260729-120000-fake").mkdir()
        tmp_d, final_d = _lm._make_unique_dirs(tmp_path, "20260729-120000-fake")
        assert final_d == tmp_path / "20260729-120000-fake-2"
        assert tmp_d == tmp_path / "20260729-120000-fake-2.tmp"

    def test_double_collision_uses_suffix_3(self, tmp_path):
        (tmp_path / "20260729-120000-fake").mkdir()
        (tmp_path / "20260729-120000-fake-2").mkdir()
        _, final_d = _lm._make_unique_dirs(tmp_path, "20260729-120000-fake")
        assert final_d == tmp_path / "20260729-120000-fake-3"


# ---------------------------------------------------------------------------
# Child-env pinning test (F-2) — intercepts before spawn, no server needed
# ---------------------------------------------------------------------------


class TestChildEnvPinning:
    """StdioServerParameters.env must be non-None with KUBECONFIG + transport keys."""

    def test_child_env_has_required_vars(self, tmp_path, monkeypatch):
        """Monkeypatch stdio_client to capture StdioServerParameters without spawning."""
        kubeconfig = _setup_env(tmp_path, monkeypatch)

        captured: dict = {}

        class _StopCapture(Exception):
            pass

        @contextlib.asynccontextmanager
        async def _capturing_stdio_client(params):
            captured["params"] = params
            raise _StopCapture("captured, not spawning")
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _capturing_stdio_client)

        with pytest.raises(_StopCapture):
            _lm.run_matrix(["run", "--tools", "list_sources", "--timeout", "60"])

        params = captured["params"]
        assert params.env is not None, (
            "env=None would let the MCP SDK allowlist drop KUBECONFIG"
        )
        assert "KUBECONFIG" in params.env, "KUBECONFIG must reach the child process"
        assert params.env["KUBECONFIG"] == str(kubeconfig), (
            "child KUBECONFIG must match the monkeypatched value"
        )
        assert params.env.get("LUMINO_TRANSPORT") == "stdio"
        assert params.env.get("KUBEARCHIVE_ENABLED") == "false"
        # Full equality: must be exactly {**os.environ, overrides}
        expected_env = {
            **os.environ,
            "LUMINO_TRANSPORT": "stdio",
            "KUBEARCHIVE_ENABLED": "false",
        }
        assert params.env == expected_env


# ---------------------------------------------------------------------------
# Parity-mismatch and atomic-dir tests (F-1, F-4) — server spawn needed
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestParityMismatch:
    """ToolSetMismatch must surface as sys.exit(3) at the sync boundary (F-1).
    No final-named run directory must exist after the abort (F-4).

    Parity check compares advertised tools against _parity_names() (read from
    parity_reference.json), NOT against CATALOG.  Tests monkeypatch _parity_names
    to simulate a diverged tool surface without touching CATALOG.
    """

    def test_parity_mismatch_exits_3(self, tmp_path, monkeypatch):
        """_parity_names() returning a mismatched set causes run_matrix to exit 3."""
        _setup_env(tmp_path, monkeypatch)

        # Simulate parity_reference missing one tool: server (48) vs parity (47)
        short = _lm._parity_names() - {"list_sources"}
        monkeypatch.setattr(_lm, "_parity_names", lambda: short)

        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--tools", "list_sources", "--timeout", "60"])
        assert exc_info.value.code == 3, (
            "parity mismatch must exit 3, not 1 (BaseExceptionGroup wrapping would give 1)"
        )

    def test_abort_leaves_no_final_dir(self, tmp_path, monkeypatch):
        """After a parity-mismatch abort, runs_root must contain no final-named dirs."""
        _setup_env(tmp_path, monkeypatch)

        short = _lm._parity_names() - {"list_sources"}
        monkeypatch.setattr(_lm, "_parity_names", lambda: short)

        with pytest.raises(SystemExit):
            _lm.run_matrix(["run", "--tools", "list_sources", "--timeout", "60"])

        final_dirs = [
            p for p in tmp_path.iterdir()
            if p.is_dir() and not p.name.endswith(".tmp")
        ]
        assert not final_dirs, (
            f"Found final-named dirs in runs_root after abort: {final_dirs}"
        )

    def test_parity_mismatch_full_sweep_exits_3(self, tmp_path, monkeypatch):
        """Parity check is unconditional: mismatch on full sweep (no --tools) exits 3.

        Pins the unconditional guard.  Reinstating ``if tools is not None:``
        around the parity block causes ONLY this test to fail while the two
        ``--tools`` parity tests above remain green.
        """
        _setup_env(tmp_path, monkeypatch)

        # Mini CATALOG so the sweep (if it erroneously ran) would be fast
        mini = {k: _lm.CATALOG[k] for k in _MINI_CATALOG_KEYS}
        monkeypatch.setattr(_lm, "CATALOG", mini)

        # Simulate parity divergence: server (48) vs reference (47)
        short = _lm._parity_names() - {"list_sources"}
        monkeypatch.setattr(_lm, "_parity_names", lambda: short)

        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--timeout", "60"])  # NO --tools
        assert exc_info.value.code == 3, (
            "parity check must fire even for full sweeps (unconditional gate)"
        )

        # No finalized run dir must remain after parity abort
        final_dirs = [
            p for p in tmp_path.iterdir()
            if p.is_dir() and not p.name.endswith(".tmp")
        ]
        assert not final_dirs, (
            f"Unexpected finalized dirs after parity abort: {final_dirs}"
        )


# ---------------------------------------------------------------------------
# Timeout-path tests (F-3 + F-6) — server spawn, tiny float timeout
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestTimeoutPath:
    """--timeout accepts floats; tool calls that exceed it record status=timeout (F-3, F-6)."""

    def test_float_timeout_records_timeout_status(self, tmp_path, monkeypatch):
        """A sub-millisecond float timeout causes each tool call to record timeout."""
        _setup_env(tmp_path, monkeypatch)

        # 0.001 s (1 ms) is shorter than any stdio round-trip; all calls should timeout.
        run_dir = _lm.run_matrix(
            ["run", "--tools", "list_sources,refresh_capabilities", "--timeout", "0.001"]
        )

        assert run_dir.is_dir()
        # Manifest must be written even when all tools timeout
        assert (run_dir / "manifest.json").exists()

        for name in ("list_sources", "refresh_capabilities"):
            rec_path = run_dir / f"{name}.json"
            assert rec_path.exists(), f"Missing record for {name}"
            rec = json.loads(rec_path.read_text())
            assert rec["status"] == "timeout", (
                f"{name}: expected timeout, got {rec['status']!r}"
            )

        # No leftover .tmp files after a complete (even all-timeout) run
        leftover = list(run_dir.rglob("*.tmp"))
        assert not leftover, f"Leftover .tmp files: {leftover}"

    def test_float_timeout_equals_form(self, tmp_path, monkeypatch):
        """--timeout=0.001 (equals form, float) must be accepted without parse error."""
        _setup_env(tmp_path, monkeypatch)

        run_dir = _lm.run_matrix(
            ["run", "--tools=list_sources", "--timeout=0.001"]
        )
        assert run_dir.is_dir()
        rec = json.loads((run_dir / "list_sources.json").read_text())
        assert rec["status"] == "timeout"


# ---------------------------------------------------------------------------
# Bogus tool name test (F-7) — server spawn
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Broken-session guard (N-1) — monkeypatched, no server spawn
# ---------------------------------------------------------------------------


class TestBrokenSession:
    """BrokenResourceError before sweep start must abort (exit 1, no final dir)."""

    def test_pre_sweep_broken_session_aborts_no_final_dir(self, tmp_path, monkeypatch):
        """Session broken during initialize() → completeness guard → exit(1), .tmp left."""
        import anyio as _anyio

        _setup_env(tmp_path, monkeypatch)

        @contextlib.asynccontextmanager
        async def _broken_client(params):
            # Raise the same exception the real stdio_client stdout_reader raises
            # during teardown; here we raise it before yielding to simulate a
            # failure during initialize() (e.g. EPIPE on a dead subprocess).
            raise _anyio.BrokenResourceError()
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _broken_client)

        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--tools", "list_sources"])

        assert exc_info.value.code == 1, (
            f"expected exit(1) for broken session, got exit({exc_info.value.code})"
        )
        # No finalized run directory (only .tmp dirs may remain for post-mortem)
        final_dirs = [
            d for d in tmp_path.iterdir()
            if d.is_dir() and not d.name.endswith(".tmp")
        ]
        assert not final_dirs, f"Unexpected finalized dirs after broken session: {final_dirs}"


# ---------------------------------------------------------------------------
# Bogus/adversarial tool name tests (F-7, F-D1) — server spawn
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestBogusToolName:
    """Unknown tool names must record status=error and not halt the sweep (F-7)."""

    def test_bogus_tool_records_error_sweep_continues(self, tmp_path, monkeypatch):
        """BOGUS_TOOL → KeyError → status=error; list_sources is still swept."""
        _setup_env(tmp_path, monkeypatch)

        run_dir = _lm.run_matrix(
            ["run", "--tools", "BOGUS_TOOL,list_sources", "--timeout", "60"]
        )

        # BOGUS_TOOL: not in CATALOG → KeyError caught → status=error
        bogus_path = run_dir / "BOGUS_TOOL.json"
        assert bogus_path.exists(), "BOGUS_TOOL.json must be written even for unknown tools"
        bogus_rec = json.loads(bogus_path.read_text())
        assert bogus_rec["status"] == "error"
        assert bogus_rec["error_type"] == "KeyError"

        # list_sources: sweep continued past the bogus tool
        real_path = run_dir / "list_sources.json"
        assert real_path.exists(), "list_sources.json missing — sweep did not continue"
        real_rec = json.loads(real_path.read_text())
        assert real_rec["status"] in VALID_STATUSES

        # Manifest present and total_seconds > 0
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["total_seconds"] >= 0

    def test_traversal_tool_name_sanitized(self, tmp_path, monkeypatch):
        """--tools ../ESCAPED must not write any file outside the run directory (F-D1)."""
        _setup_env(tmp_path, monkeypatch)

        run_dir = _lm.run_matrix(
            ["run", "--tools", "../ESCAPED", "--timeout", "60"]
        )

        # The traversal target (two levels up from run_dir, into tmp_path parent) must
        # NOT exist.  The record must be inside run_dir under a sanitized name.
        assert not (tmp_path / "ESCAPED.json").exists(), (
            "traversal: wrote outside run dir (tmp_path/ESCAPED.json exists)"
        )
        # Sanitized name: "../ESCAPED" → "..-ESCAPED" (slash replaced by '-')
        sanitized = run_dir / "..-ESCAPED.json"
        assert sanitized.exists(), (
            f"sanitized record not found at {sanitized}; "
            f"contents of run_dir: {list(run_dir.iterdir())}"
        )
        rec = json.loads(sanitized.read_text())
        assert rec["status"] == "error", "adversarial tool name should record error (not in CATALOG)"


# ---------------------------------------------------------------------------
# Core smoke tests (F-9, F-10 updates) — server spawn
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRunnerSmokeExplicitTools:
    """run_matrix with --tools produces a complete, valid run directory."""

    def test_run_dir_exists_under_live_matrix_root(self, tmp_path, monkeypatch):
        """The returned path must be a directory whose parent is LIVE_MATRIX_ROOT."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        assert run_dir.parent == tmp_path
        assert run_dir.is_dir()

    def test_manifest_has_all_required_keys(self, tmp_path, monkeypatch):
        """manifest.json must contain every key declared in the Task 3 brief."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        manifest = json.loads((run_dir / "manifest.json").read_text())

        for key in ("fingerprint", "flags", "targets", "tool_seconds",
                    "total_seconds", "partial", "started_utc", "finished_utc"):
            assert key in manifest, f"manifest missing key: {key!r}"

        assert manifest["partial"] is True

        fp = manifest["fingerprint"]
        for fp_key in ("git_sha", "dirty", "python", "cluster_id"):
            assert fp_key in fp, f"fingerprint missing key: {fp_key!r}"

    def test_manifest_git_sha_matches_head(self, tmp_path, monkeypatch):
        """fingerprint.git_sha must equal the output of git rev-parse HEAD."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        expected_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
        ).strip()
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["fingerprint"]["git_sha"] == expected_sha

    def test_tool_records_present_with_valid_keys(self, tmp_path, monkeypatch):
        """Each tool must have a <name>.json with all 7 required fields (F-10: error_type added)."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        for name in EXPLICIT_TOOLS_SET:
            rec_path = run_dir / f"{name}.json"
            assert rec_path.exists(), f"Missing tool record: {name}.json"
            rec = json.loads(rec_path.read_text())
            # F-10: error_type must be in the field list (was missing before fix)
            for field in ("args", "expectation", "status", "error_type",
                          "latency_ms", "response_bytes", "shape"):
                assert field in rec, f"{name}.json missing field: {field!r}"
            assert rec["status"] in VALID_STATUSES, (
                f"{name}: unexpected status {rec['status']!r}"
            )

    def test_connect_cluster_error_type_is_stable_code(self, tmp_path, monkeypatch):
        """connect_cluster must report error_type='ref_outside_allowlist' (F-9 ruling).

        The server returns payload['code']='ref_outside_allowlist'; the updated
        extraction order (code → error_type → error[:80]) must pick up 'code' first
        so that wording changes in the 'error' field don't invalidate future diffs.
        """
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", "connect_cluster", "--timeout", "60"]
        )
        rec = json.loads((run_dir / "connect_cluster.json").read_text())
        assert rec["status"] == "error", (
            "connect_cluster is error_ok — expected status=error"
        )
        assert rec["error_type"] == "ref_outside_allowlist", (
            f"expected 'ref_outside_allowlist', got {rec['error_type']!r}; "
            "check that _extract_error_code uses code > error_type > error[:80]"
        )

    def test_tool_records_have_non_empty_shape(self, tmp_path, monkeypatch):
        """Every tool record must have a non-None, non-empty shape."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        for name in EXPLICIT_TOOLS_SET:
            rec = json.loads((run_dir / f"{name}.json").read_text())
            assert rec["shape"] is not None, f"{name}: shape is None"
            assert rec["shape"] != "", f"{name}: shape is empty string"

    def test_raw_dir_and_files_exist(self, tmp_path, monkeypatch):
        """raw/<name>.txt must exist for every swept tool."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        raw_dir = run_dir / "raw"
        assert raw_dir.is_dir(), "raw/ subdirectory missing"
        for name in EXPLICIT_TOOLS_SET:
            assert (raw_dir / f"{name}.txt").exists(), (
                f"Missing raw file: raw/{name}.txt"
            )

    def test_no_leftover_tmp_files(self, tmp_path, monkeypatch):
        """After a successful run there must be no *.tmp files in the run dir."""
        _setup_env(tmp_path, monkeypatch)
        run_dir = _lm.run_matrix(
            ["run", "--tools", EXPLICIT_TOOLS, "--timeout", "60"]
        )
        leftover = list(run_dir.rglob("*.tmp"))
        assert not leftover, f"Leftover .tmp files: {leftover}"

    def test_two_runs_produce_distinct_dirs(self, tmp_path, monkeypatch):
        """Two runs in rapid succession must always land in distinct directories (F-8)."""
        _setup_env(tmp_path, monkeypatch)
        run_dir_1 = _lm.run_matrix(
            ["run", "--tools", "list_sources", "--timeout", "60"]
        )
        run_dir_2 = _lm.run_matrix(
            ["run", "--tools", "list_sources", "--timeout", "60"]
        )
        assert run_dir_1 != run_dir_2
        assert run_dir_1.is_dir()
        assert run_dir_2.is_dir()


# ---------------------------------------------------------------------------
# Task 3b: Discovery phase, full-sweep default, --source passthrough
# ---------------------------------------------------------------------------


# Mini-catalog for fast full-sweep tests: 3 tools instead of 48.
# Using connect_cluster (error_ok, no cluster needed), list_sources, and
# refresh_capabilities because all three have no {placeholder} args and
# produce stable responses even without a live cluster.
_MINI_CATALOG_KEYS = ["connect_cluster", "list_sources", "refresh_capabilities"]


@pytest.mark.slow
class TestDiscoveryPhase:
    """Full-sweep default: partial=False when --tools unset; discovery degrades gracefully."""

    def test_full_sweep_partial_false_null_targets(self, tmp_path, monkeypatch):
        """No --tools flag → partial=False; CATALOG monkeypatched to 3 entries.

        With FAKE_KUBECONFIG (no cluster) discovery returns all-null targets
        but the run completes with error statuses for each tool.  partial must
        be False even with the monkeypatched catalog.
        """
        _setup_env(tmp_path, monkeypatch)

        mini_catalog = {k: _lm.CATALOG[k] for k in _MINI_CATALOG_KEYS}
        monkeypatch.setattr(_lm, "CATALOG", mini_catalog)

        run_dir = _lm.run_matrix(["run", "--timeout", "30"])

        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["partial"] is False, (
            "partial must be False when --tools is not given, even with monkeypatched CATALOG"
        )

        # All 3 mini-catalog tools must have been swept
        for name in _MINI_CATALOG_KEYS:
            rec_path = run_dir / f"{name}.json"
            assert rec_path.exists(), f"Missing tool record: {name}.json"
            rec = json.loads(rec_path.read_text())
            assert rec["status"] in VALID_STATUSES, (
                f"{name}: unexpected status {rec['status']!r}"
            )

        # Discovery failed (no cluster) → targets must be all None in manifest
        targets = manifest["targets"]
        for key in ("namespace", "pod", "pipelinerun", "pipelinerun_ns"):
            assert targets[key] is None, (
                f"targets[{key!r}] should be None (no cluster), got {targets[key]!r}"
            )

    def test_flag_overrides_land_in_manifest_targets(self, tmp_path, monkeypatch):
        """--namespace/--pod/--pipelinerun flags override discovery and appear in manifest targets."""
        _setup_env(tmp_path, monkeypatch)

        run_dir = _lm.run_matrix([
            "run",
            "--tools", "list_sources",
            "--timeout", "30",
            "--namespace", "x",
            "--pod", "y",
            "--pipelinerun", "a/b",
        ])

        manifest = json.loads((run_dir / "manifest.json").read_text())
        targets = manifest["targets"]
        assert targets["namespace"] == "x", (
            f"--namespace x must appear in manifest targets, got {targets['namespace']!r}"
        )
        assert targets["pod"] == "y", (
            f"--pod y must appear in manifest targets, got {targets['pod']!r}"
        )
        assert targets["pipelinerun_ns"] == "a", (
            f"pipelinerun_ns from --pipelinerun a/b must be 'a', got {targets['pipelinerun_ns']!r}"
        )
        assert targets["pipelinerun"] == "b", (
            f"pipelinerun from --pipelinerun a/b must be 'b', got {targets['pipelinerun']!r}"
        )

    def test_source_flag_in_manifest_and_tool_args(self, tmp_path, monkeypatch):
        """--source my-source injects source into tool call args (for accepts_source tools)."""
        _setup_env(tmp_path, monkeypatch)

        run_dir = _lm.run_matrix([
            "run",
            "--tools", "list_namespaces",
            "--timeout", "30",
            "--source", "my-source",
        ])

        # --source must appear in the manifest flags (raw argv)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert "--source" in manifest["flags"]
        assert "my-source" in manifest["flags"]

        # list_namespaces accepts_source=True → source must be in the persisted args
        rec = json.loads((run_dir / "list_namespaces.json").read_text())
        assert rec["args"].get("source") == "my-source", (
            f"source not injected into list_namespaces args: {rec['args']!r}"
        )


class TestDiscoveryArgParsing:
    """Fast parser tests for new Task 3b flags — no server spawn."""

    def test_namespace_flag_accepted(self, tmp_path, monkeypatch):
        """--namespace must not exit 2 (unknown flag)."""
        _setup_env(tmp_path, monkeypatch)
        captured: dict = {}

        class _Stop(Exception):
            pass

        @contextlib.asynccontextmanager
        async def _mock(params):
            captured["reached"] = True
            raise _Stop()
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _mock)

        with pytest.raises(_Stop):
            _lm.run_matrix(["run", "--tools", "list_sources", "--namespace", "test-ns"])
        assert captured.get("reached"), "--namespace should not exit 2"

    def test_pod_flag_accepted(self, tmp_path, monkeypatch):
        """--pod must not exit 2."""
        _setup_env(tmp_path, monkeypatch)
        captured: dict = {}

        class _Stop(Exception):
            pass

        @contextlib.asynccontextmanager
        async def _mock(params):
            captured["reached"] = True
            raise _Stop()
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _mock)

        with pytest.raises(_Stop):
            _lm.run_matrix(["run", "--tools", "list_sources", "--pod", "my-pod"])
        assert captured.get("reached"), "--pod should not exit 2"

    def test_pipelinerun_flag_accepted(self, tmp_path, monkeypatch):
        """--pipelinerun NS/NAME must not exit 2."""
        _setup_env(tmp_path, monkeypatch)
        captured: dict = {}

        class _Stop(Exception):
            pass

        @contextlib.asynccontextmanager
        async def _mock(params):
            captured["reached"] = True
            raise _Stop()
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _mock)

        with pytest.raises(_Stop):
            _lm.run_matrix(["run", "--tools", "list_sources", "--pipelinerun", "ns/run"])
        assert captured.get("reached"), "--pipelinerun ns/run should not exit 2"

    def test_pipelinerun_bad_format_exits_2(self):
        """--pipelinerun without a slash separator must exit 2."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--tools", "list_sources", "--pipelinerun", "noslash"])
        assert exc_info.value.code == 2

    def test_source_flag_accepted(self, tmp_path, monkeypatch):
        """--source must not exit 2."""
        _setup_env(tmp_path, monkeypatch)
        captured: dict = {}

        class _Stop(Exception):
            pass

        @contextlib.asynccontextmanager
        async def _mock(params):
            captured["reached"] = True
            raise _Stop()
            yield  # pragma: no cover

        monkeypatch.setattr(_mcp_stdio, "stdio_client", _mock)

        with pytest.raises(_Stop):
            _lm.run_matrix(["run", "--tools", "list_sources", "--source", "my-src"])
        assert captured.get("reached"), "--source should not exit 2"

    def test_empty_namespace_exits_2(self):
        """--namespace with empty value must exit 2."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--namespace=", "--tools", "list_sources"])
        assert exc_info.value.code == 2

    def test_empty_pod_exits_2(self):
        """--pod with empty value must exit 2."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--pod=", "--tools", "list_sources"])
        assert exc_info.value.code == 2

    def test_empty_source_exits_2(self):
        """--source with empty value must exit 2."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--source=", "--tools", "list_sources"])
        assert exc_info.value.code == 2

    def test_pipelinerun_multi_slash_exits_2(self):
        """--pipelinerun with more than one '/' must exit 2."""
        with pytest.raises(SystemExit) as exc_info:
            _lm.run_matrix(["run", "--pipelinerun", "a/b/c", "--tools", "list_sources"])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Task 3b fix-round: _discover_targets_async unit tests (no server spawn)
# ---------------------------------------------------------------------------


class TestDiscoverTargetsUnit:
    """Unit tests for _discover_targets_async using a fake synchronous session.

    These tests cover the success paths that are invisible to the slow
    integration tests (which run against FAKE_KUBECONFIG and always get
    all-null targets).  They also kill two surviving mutants identified by
    the code reviewer.
    """

    @staticmethod
    def _run(coro: Any) -> Any:
        """Run a coroutine synchronously."""
        import asyncio
        return asyncio.run(coro)

    @staticmethod
    def _make_session(responses: dict) -> Any:
        """Create a fake session whose call_tool returns canned JSON payloads."""
        import types

        class _FakeSession:
            async def call_tool(self, name: str, args: dict) -> Any:
                payload = responses.get(name, {"error": "not found"})
                text = json.dumps(payload)
                content = types.SimpleNamespace(text=text)
                return types.SimpleNamespace(content=[content], isError=False)

        return _FakeSession()

    def test_preference_ordering_prefers_konflux(self):
        """Namespaces matching konflux|tenant are probed first.

        Mutant killed: if _PREF_RE is replaced with a never-matching pattern,
        'aaa' (alphabetically first) would be chosen instead of 'tenant-prod'.
        """
        session = self._make_session({
            "list_namespaces": ["aaa", "tenant-prod", "zzz"],
            "list_pods_in_namespace": [{"name": "p1", "status": "Running"}],
            "list_recent_pipeline_runs": {},
        })
        result = self._run(_lm._discover_targets_async(session, source=None, timeout_secs=30))
        assert result["namespace"] == "tenant-prod", (
            f"preferred (konflux|tenant) namespace must be probed first; "
            f"got {result['namespace']!r}"
        )
        assert result["pod"] == "p1"

    def test_non_preferred_fills_remaining_probe_slots(self):
        """Non-preferred namespaces fill remaining probe budget after preferred ones.

        With one preferred namespace (no pods) and two non-preferred (with pods),
        the tiebreak ordering [preferred + non-preferred] means 'zz' (non-preferred)
        is found as the fallback, not skipped.
        """
        import types

        probed: list[str] = []

        class _Session:
            async def call_tool(self, name: str, args: dict) -> Any:
                if name == "list_namespaces":
                    payload: Any = ["non-a", "non-b", "konflux-ci"]
                elif name == "list_pods_in_namespace":
                    ns = args.get("namespace")
                    probed.append(ns)
                    # preferred namespace has no pods; others do
                    payload = (
                        [] if ns == "konflux-ci"
                        else [{"name": f"pod-{ns}", "status": "Running"}]
                    )
                else:
                    payload = {"error": "none"}
                text = json.dumps(payload)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=text)], isError=False
                )

        result = self._run(_lm._discover_targets_async(_Session(), source=None, timeout_secs=30))
        # preferred namespace probed first (even though it has no pods)
        assert probed[0] == "konflux-ci", (
            f"preferred namespace must be probed first; got {probed!r}"
        )
        # fallback to a non-preferred namespace since preferred had no pods
        assert result["namespace"] is not None
        assert result["namespace"] != "konflux-ci"

    def test_running_pod_selected_over_non_running(self):
        """Running pods are preferred; non-Running pods serve as fallback only."""
        session = self._make_session({
            "list_namespaces": ["ns1"],
            "list_pods_in_namespace": [
                {"name": "failed-pod", "status": "Failed"},
                {"name": "running-pod", "status": "Running"},
                {"name": "pending-pod", "status": "Pending"},
            ],
            "list_recent_pipeline_runs": {},
        })
        result = self._run(_lm._discover_targets_async(session, source=None, timeout_secs=30))
        assert result["pod"] == "running-pod", (
            f"Running pod must be selected over non-Running; got {result['pod']!r}"
        )

    def test_non_dict_entries_skipped(self):
        """Non-dict entries (e.g. the '_truncation' sentinel string) are skipped."""
        session = self._make_session({
            "list_namespaces": ["ns1"],
            # list_pods returns a mix of strings (sentinel) and dicts (real pods)
            "list_pods_in_namespace": ["_truncation", {"name": "real-pod", "status": "Running"}],
            "list_recent_pipeline_runs": {},
        })
        result = self._run(_lm._discover_targets_async(session, source=None, timeout_secs=30))
        assert result["pod"] == "real-pod", (
            f"String entries must be skipped; got {result['pod']!r}"
        )

    def test_pipelinerun_ns_name_extracted(self):
        """pipelinerun_ns and pipelinerun are extracted from list_recent_pipeline_runs."""
        session = self._make_session({
            "list_namespaces": [],
            "list_recent_pipeline_runs": {
                "outer-ns": [{"name": "pr-1", "namespace": "real-ns", "status": "Succeeded"}]
            },
        })
        result = self._run(_lm._discover_targets_async(session, source=None, timeout_secs=30))
        assert result["pipelinerun"] == "pr-1"
        assert result["pipelinerun_ns"] == "real-ns"

    def test_error_key_in_pipeline_runs_skipped(self):
        """Error response from list_recent_pipeline_runs yields null pipelinerun targets.

        Mutant killed: if 'error' key check is removed, the error response
        {'error': [{'name': 'decoy', 'namespace': 'x'}]} would be iterated
        and pipelinerun='decoy' would be incorrectly extracted.
        """
        session = self._make_session({
            "list_namespaces": [],
            # Value is a list so it would parse as runs if the 'error' check is removed
            "list_recent_pipeline_runs": {
                "error": [{"name": "decoy-run", "namespace": "decoy-ns"}]
            },
        })
        result = self._run(_lm._discover_targets_async(session, source=None, timeout_secs=30))
        assert result["pipelinerun"] is None, (
            f"'error' key must be skipped; got pipelinerun={result['pipelinerun']!r}"
        )
        assert result["pipelinerun_ns"] is None

    def test_source_threaded_to_all_discovery_calls(self):
        """When source is given, it is forwarded to every call_tool invocation."""
        import types

        captured_args: dict[str, list[dict]] = {}

        class _Session:
            async def call_tool(self, name: str, args: dict) -> Any:
                captured_args.setdefault(name, []).append(dict(args))
                if name == "list_namespaces":
                    payload: Any = ["ns1"]
                elif name == "list_pods_in_namespace":
                    payload = [{"name": "p1", "status": "Running"}]
                else:
                    payload = {"error": "none"}
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=json.dumps(payload))], isError=False
                )

        self._run(_lm._discover_targets_async(_Session(), source="my-src", timeout_secs=30))
        assert captured_args["list_namespaces"][0].get("source") == "my-src", (
            "source not threaded to list_namespaces"
        )
        assert captured_args["list_pods_in_namespace"][0].get("source") == "my-src", (
            "source not threaded to list_pods_in_namespace"
        )
        assert captured_args["list_recent_pipeline_runs"][0].get("source") == "my-src", (
            "source not threaded to list_recent_pipeline_runs"
        )

    def test_no_source_omits_source_arg(self):
        """When source=None, no 'source' key is added to call_tool args."""
        import types

        captured_args: dict[str, list[dict]] = {}

        class _Session:
            async def call_tool(self, name: str, args: dict) -> Any:
                captured_args.setdefault(name, []).append(dict(args))
                payload: Any = [] if name != "list_recent_pipeline_runs" else {"error": "x"}
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=json.dumps(payload))], isError=False
                )

        self._run(_lm._discover_targets_async(_Session(), source=None, timeout_secs=30))
        for tool_name, calls in captured_args.items():
            for call_args in calls:
                assert "source" not in call_args, (
                    f"source=None must not add 'source' key; found in {tool_name}: {call_args!r}"
                )
