"""Behavioral tests for phase-3 dispatch plumbing:
  _get_file_source factory (caching + missing-roots ValueError),
  _route_log_source router (six cases), and
  _logbatch_to_legacy_envelope converter (three cases).

Also tests the six-tool unwired-invariant: every unwired log-family tool still
surfaces a gate error (routing or capability) for a configured file source,
confirming they are not wired to the adapter yet.
"""
import inspect
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Insert order is load-bearing: src/ MUST end up at sys.path[0].  Same
# rationale as test_source_gating.py (module-level core.* imports).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.config_types import ResolvedConfig, SourceConfig
from core.registry import build_registry
from core.selector import TimeWindow
from core.signals import LogBatch, LogRecord, Provenance
from adapters.file.logs import FileLogSource


# ─── helpers ─────────────────────────────────────────────────────────────────


def _registry_with(monkeypatch, server, sources):
    reg = build_registry(ResolvedConfig(profile="test", sources=sources))
    monkeypatch.setattr(server, "_source_registry", reg)
    return reg


def _config_with(monkeypatch, server, sources):
    cfg = ResolvedConfig(profile="test", sources=sources)
    monkeypatch.setattr(server, "_lumino_config", cfg)
    return cfg


def _file_src_cfg(tmp_path):
    return SourceConfig(adapter="file", options={"roots": (str(tmp_path),)})


# ─── _route_log_source: routing cases ────────────────────────────────────────


def test_route_empty_source_returns_none_none(server):
    """Empty source string → legacy path: (None, None)."""
    adapter, err = server._route_log_source("analyze_logs", "")
    assert adapter is None and err is None


def test_route_unknown_source_returns_none_err(server, monkeypatch):
    """Unknown source → (None, err) with the unknown-source error shape."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    adapter, err = server._route_log_source("analyze_logs", "nope")
    assert adapter is None
    assert err is not None
    assert err.get("requested_source") == "nope"
    assert "list_sources" in err.get("error", "")


def test_route_incapable_source_returns_none_err_with_capable_sources(
        server, monkeypatch):
    """Incapable source (prometheus for a Log tool) → (None, capability-err)."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus"),
    })
    adapter, err = server._route_log_source("analyze_logs", "prometheus")
    assert adapter is None
    assert err is not None and "capable_sources" in err


def test_route_file_source_returns_file_log_source_adapter(server, monkeypatch, tmp_path):
    """Configured file source → (FileLogSource instance, None)."""
    cfg = _file_src_cfg(tmp_path)
    _registry_with(monkeypatch, server, {"myfile": cfg})
    _config_with(monkeypatch, server, {"myfile": cfg})
    monkeypatch.setattr(server, "_adapter_instances", {})
    adapter, err = server._route_log_source("analyze_logs", "myfile")
    assert err is None
    assert isinstance(adapter, FileLogSource)


def test_route_file_source_caches_same_instance(server, monkeypatch, tmp_path):
    """Two calls for the same file source return the identical adapter instance."""
    cfg = _file_src_cfg(tmp_path)
    _registry_with(monkeypatch, server, {"myfile": cfg})
    _config_with(monkeypatch, server, {"myfile": cfg})
    monkeypatch.setattr(server, "_adapter_instances", {})
    adapter1, _ = server._route_log_source("analyze_logs", "myfile")
    adapter2, _ = server._route_log_source("analyze_logs", "myfile")
    assert adapter1 is adapter2


def test_route_missing_roots_raises_value_error(server, monkeypatch, tmp_path):
    """File source with no roots → ValueError raised from _get_file_source."""
    no_roots_cfg = SourceConfig(adapter="file", options={})
    _registry_with(monkeypatch, server, {"myfile": no_roots_cfg})
    _config_with(monkeypatch, server, {"myfile": no_roots_cfg})
    monkeypatch.setattr(server, "_adapter_instances", {})
    with pytest.raises(ValueError, match="no roots"):
        server._route_log_source("analyze_logs", "myfile")


def test_route_kubearchive_capable_nonfie_returns_none_routing_err(
        server, monkeypatch):
    """Kubearchive (Log-capable, adapter='kubearchive') → (None, routing-err).

    Only the file adapter is routed in phase 3; every other capable-but-non-file
    source gets the routing error back unchanged.
    """
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "kubearchive": SourceConfig(adapter="kubearchive"),
    })
    adapter, err = server._route_log_source("analyze_logs", "kubearchive")
    assert adapter is None
    assert err is not None and err.get("routable") is False


# ─── _logbatch_to_legacy_envelope converter ──────────────────────────────────


def _make_record(body, file_attr=None):
    attrs = {"file": file_attr} if file_attr is not None else {}
    return LogRecord(timestamp=None, body=body, attributes=attrs)


def _make_batch(records):
    return LogBatch(
        records=records,
        provenance=Provenance(adapter="file", query={}),
    )


def test_converter_empty_batch_returns_empty_logs(server):
    """Empty batch → {"logs": {}}."""
    result = server._logbatch_to_legacy_envelope(_make_batch([]))
    assert result == {"logs": {}}


def test_converter_multi_file_groups_by_relpath(server):
    """Records with different file attributes group under their relpath keys."""
    records = [
        _make_record("line-a1", file_attr="subdir/a.log"),
        _make_record("line-a2", file_attr="subdir/a.log"),
        _make_record("line-b1", file_attr="b.log"),
    ]
    result = server._logbatch_to_legacy_envelope(_make_batch(records))
    assert result == {
        "logs": {
            "subdir/a.log": "line-a1\nline-a2",
            "b.log": "line-b1",
        }
    }


def test_converter_no_file_attr_groups_under_log_key(server):
    """Records with no 'file' attribute group under the fallback 'log' key."""
    records = [
        _make_record("line-1"),
        _make_record("line-2"),
    ]
    result = server._logbatch_to_legacy_envelope(_make_batch(records))
    assert result == {"logs": {"log": "line-1\nline-2"}}


# ─── Unwired-tools invariant: all SIX log-family tools return a gate error ───
#
# All six tools call _gate_source directly and are NOT wired to _route_log_source
# in Task 4.  A configured file source either:
#   - triggers a routing error (routable:False) for Log-only tools, or
#   - triggers a capability error (capable_sources list) for multi-cap tools
#     (semantic_log_search needs Log+Event; manage needs Log+Event+Inventory).
# Either way, the result must be an error dict (never a successful analysis).

_UNWIRED_LOG_TOOL_KWARGS = {
    # analyze_logs and detect_log_anomalies are NOT in this table: they hold a
    # provenance-only gate that ACCEPTS any registered source (nothing to route),
    # so a configured file source would NOT produce a gate error — wrong contract.
    # Their unknown-source rejection is covered by test_text_only_tools_provenance_gate
    # in test_source_gating.py and test_text_only_unknown_source_rejected below.
    "analyze_pod_logs_hybrid":         {"namespace": "team-a", "pod_name": "api-1"},
    "semantic_log_search":             {"query": "database error"},
    "predictive_log_analyzer":         {},
    "manage_prediction_training_data": {"action": "collect",
                                        "collect_from_namespaces": ["team-a"]},
}


@pytest.mark.asyncio
async def test_unwired_log_tools_return_gate_error_for_file_source(
        server, monkeypatch, tmp_path):
    """Every unwired log-family tool returns a gate error for a configured file source.

    Task 4 wires NOTHING: these tools still call _gate_source directly.
    The test fails (AttributeError on _adapter_instances) until the module-level
    cache dict is added, confirming the RED-GREEN cycle.
    """
    cfg = _file_src_cfg(tmp_path)
    _registry_with(monkeypatch, server, {"myfile": cfg})
    # _adapter_instances is the production dict added in this task; its existence
    # here confirms the Task-4 plumbing landed.
    monkeypatch.setattr(server, "_adapter_instances", {})

    for tool_name, kwargs in _UNWIRED_LOG_TOOL_KWARGS.items():
        fn = getattr(server, tool_name)
        result = fn(**kwargs, source="myfile")
        if inspect.isawaitable(result):
            result = await result
        assert isinstance(result, dict) and "error" in result, (
            f"{tool_name}: expected gate error dict, got {result!r}"
        )
        # Pure-Log-only tools get the routing error (routable:False).
        # Multi-cap tools (semantic_log_search, manage_prediction_training_data)
        # get a capability error instead — both shapes have "error" +
        # "capable_sources" and must NOT have "routable".
        if tool_name in ("semantic_log_search", "manage_prediction_training_data"):
            assert "capable_sources" in result, (
                f"{tool_name}: expected capable_sources in capability error, "
                f"got {result!r}"
            )
            assert "routable" not in result, (
                f"{tool_name}: capability error must not contain 'routable', "
                f"got {result!r}"
            )
        else:
            assert result.get("routable") is False, (
                f"{tool_name}: expected routable:False, got {result!r}"
            )


# ─── Text-only tool unknown-source rejection ────────────────────────────────

_LOG_SAMPLE = "ERROR: crash\nINFO: ok\n"

_TEXT_ONLY_TOOL_KWARGS = {
    "analyze_logs":        {"log_text": _LOG_SAMPLE},
    "detect_log_anomalies": {"logs": _LOG_SAMPLE},
}


@pytest.mark.asyncio
async def test_text_only_unknown_source_rejected(server, monkeypatch, tmp_path):
    """analyze_logs and detect_log_anomalies reject an unknown source= via provenance gate.

    A configured file source IS accepted (provenance-only: any registered source passes).
    An unregistered 'nope' source returns the canonical unknown-source error dict.
    """
    cfg = _file_src_cfg(tmp_path)
    _registry_with(monkeypatch, server, {"myfile": cfg})

    for tool_name, kwargs in _TEXT_ONLY_TOOL_KWARGS.items():
        fn = getattr(server, tool_name)

        # Unknown source → canonical unknown-source error.
        bad = fn(**kwargs, source="nope")
        if inspect.isawaitable(bad):
            bad = await bad
        assert bad.get("requested_source") == "nope", (
            f"{tool_name}: expected unknown-source error for 'nope', got {bad!r}"
        )

        # Registered source → accepted as provenance, body runs normally.
        good = fn(**kwargs, source="myfile")
        if inspect.isawaitable(good):
            good = await good
        assert "error" not in good, (
            f"{tool_name}: registered source 'myfile' should be accepted, got {good!r}"
        )


# ─── Task 5 wiring tests: smart_summarize + stream_analyze ───────────────────

# shared marker embedded in the fixture log file
_TASK5_MARKER = "UNIQUE_MARKER_TASK5_DISPATCH_XY7Z"


def _setup_file_source(monkeypatch, server, tmp_path, source_name="file-test"):
    """Configure a file source backed by tmp_path and return it."""
    cfg = _file_src_cfg(tmp_path)
    _registry_with(monkeypatch, server, {source_name: cfg})
    _config_with(monkeypatch, server, {source_name: cfg})
    monkeypatch.setattr(server, "_adapter_instances", {})
    return cfg


@pytest.mark.asyncio
async def test_smart_summarize_file_source_end_to_end(server, monkeypatch, tmp_path):
    """smart_summarize_pod_logs: file source reads from filesystem, not k8s.

    The marker string embedded in the fixture log file must appear in the
    result, proving the file adapter was used.  k8s_core_api must NOT be
    called — if _quick_volume_estimate reaches the MagicMock it means the
    adapter branch was not hoisted above the volume-estimate block.
    """
    from unittest.mock import MagicMock

    log_file = tmp_path / "app.log"
    log_file.write_text(
        f"ERROR: {_TASK5_MARKER} fixture log line one\n"
        f"ERROR: {_TASK5_MARKER} fixture log line two\n"
    )

    _setup_file_source(monkeypatch, server, tmp_path)

    fake_k8s = MagicMock()
    monkeypatch.setattr(server, "k8s_core_api", fake_k8s)

    result = await server.smart_summarize_pod_logs(
        namespace="ignored",
        pod_name="*.log",
        source="file-test",
    )

    assert isinstance(result, dict) and "error" not in result, (
        f"expected success dict, got {result!r}"
    )
    assert _TASK5_MARKER in str(result), (
        f"marker not found in result — k8s path was taken instead of file "
        f"adapter; result keys: {list(result)}"
    )
    # k8s must NOT be touched: _quick_volume_estimate reaching a MagicMock k8s
    # client is the failure signature of a mis-placed adapter branch.
    # read_namespaced_pod (singular, no _log) is the REAL first k8s contact
    # point: _quick_volume_estimate → get_pod_logs → get_all_pod_logs (utils.py:499).
    assert not fake_k8s.read_namespaced_pod.called, (
        "read_namespaced_pod was called — adapter branch not hoisted "
        "above _quick_volume_estimate (real first k8s contact point)"
    )
    assert not fake_k8s.read_namespaced_pod_log.called, (
        "read_namespaced_pod_log was called — adapter branch not hoisted "
        "above _quick_volume_estimate"
    )
    assert not fake_k8s.list_namespaced_pod.called, (
        "list_namespaced_pod was called — k8s path not bypassed by file adapter"
    )


@pytest.mark.asyncio
async def test_stream_analyze_file_source_end_to_end(server, monkeypatch, tmp_path):
    """stream_analyze_pod_logs: file source reads from filesystem, not k8s."""
    from unittest.mock import MagicMock

    log_file = tmp_path / "svc.log"
    log_file.write_text(
        f"ERROR: {_TASK5_MARKER} stream fixture line one\n"
        f"WARNING: {_TASK5_MARKER} stream fixture line two\n"
    )

    _setup_file_source(monkeypatch, server, tmp_path)

    fake_k8s = MagicMock()
    monkeypatch.setattr(server, "k8s_core_api", fake_k8s)

    result = await server.stream_analyze_pod_logs(
        namespace="ignored",
        pod_name="*.log",
        source="file-test",
    )

    assert isinstance(result, dict) and "error" not in result, (
        f"expected success dict, got {result!r}"
    )
    assert _TASK5_MARKER in str(result), (
        f"marker not found in result — k8s path was taken instead of file adapter; "
        f"result keys: {list(result)}"
    )
    # read_namespaced_pod (singular, no _log) is the REAL first k8s contact
    # point: _quick_volume_estimate → get_pod_logs → get_all_pod_logs (utils.py:499).
    assert not fake_k8s.read_namespaced_pod.called, (
        "read_namespaced_pod was called — file adapter branch not wired "
        "(real first k8s contact point)"
    )
    assert not fake_k8s.read_namespaced_pod_log.called, (
        "read_namespaced_pod_log was called — file adapter branch not wired"
    )
    assert not fake_k8s.list_namespaced_pod.called, (
        "list_namespaced_pod was called — k8s path not bypassed"
    )


@pytest.mark.asyncio
async def test_smart_summarize_escape_glob_returns_error(server, monkeypatch, tmp_path):
    """smart_summarize_pod_logs: absolute pod_name (escape glob) → {"error": ...} with PathOutsideRoots message."""
    _setup_file_source(monkeypatch, server, tmp_path)

    result = await server.smart_summarize_pod_logs(
        namespace="ignored",
        pod_name="/absolute/escape/path.log",
        source="file-test",
    )

    assert isinstance(result, dict) and "error" in result, (
        f"expected error dict for absolute pod_name, got {result!r}"
    )
    assert "absolute paths are not allowed" in result["error"], (
        f"expected PathOutsideRoots message 'absolute paths are not allowed' "
        f"in error, got {result['error']!r}"
    )


@pytest.mark.asyncio
async def test_stream_analyze_escape_glob_returns_error(server, monkeypatch, tmp_path):
    """stream_analyze_pod_logs: absolute pod_name (escape glob) → {"error": ...} with PathOutsideRoots message."""
    _setup_file_source(monkeypatch, server, tmp_path)

    result = await server.stream_analyze_pod_logs(
        namespace="ignored",
        pod_name="/absolute/escape/path.log",
        source="file-test",
    )

    assert isinstance(result, dict) and "error" in result, (
        f"expected error dict for absolute pod_name, got {result!r}"
    )
    assert "absolute paths are not allowed" in result["error"], (
        f"expected PathOutsideRoots message 'absolute paths are not allowed' "
        f"in error, got {result['error']!r}"
    )


@pytest.mark.asyncio
async def test_smart_summarize_empty_source_calls_get_pod_logs(server, monkeypatch):
    """smart_summarize_pod_logs source="" uses the legacy k8s path (get_pod_logs called)."""
    from unittest.mock import AsyncMock

    mock_gpl = AsyncMock(return_value={"logs": {"main": "INFO: hello world\n"}})
    monkeypatch.setattr(server, "get_pod_logs", mock_gpl)

    await server.smart_summarize_pod_logs(
        namespace="team-a",
        pod_name="api-pod",
        source="",
    )

    assert mock_gpl.called, (
        "get_pod_logs was NOT called with source='' — legacy k8s path broken"
    )


@pytest.mark.asyncio
async def test_stream_analyze_empty_source_calls_get_pod_logs(server, monkeypatch):
    """stream_analyze_pod_logs source="" uses the legacy k8s path (get_pod_logs called)."""
    from unittest.mock import AsyncMock

    mock_gpl = AsyncMock(return_value={"logs": {"main": "INFO: hello world\n"}})
    monkeypatch.setattr(server, "get_pod_logs", mock_gpl)

    await server.stream_analyze_pod_logs(
        namespace="team-a",
        pod_name="api-pod",
        source="",
    )

    assert mock_gpl.called, (
        "get_pod_logs was NOT called with source='' — legacy k8s path broken"
    )


# ─── Phase 3.5 Task 1: window-reaches-adapter tests ─────────────────────────
#
# Mutation target: the derived TimeWindow actually REACHES the adapter.
#
# PRE-SEED mechanic: monkeypatch.setitem(server._adapter_instances, "file-test",
# stub) puts the mock adapter in the factory cache so _get_file_source returns
# it without touching the filesystem.  Registry + config are still rewired so
# _gate_source classifies "file-test" as a routable file Log-source.
# The stub returns an empty LogBatch, causing the tool to short-circuit at the
# "No logs found" branch AFTER fetch_logs has been called — that call captures
# the derived window.


def _empty_log_batch():
    return LogBatch(
        records=[],
        provenance=Provenance(adapter="file", query={}),
    )


def _file_src_cfg_for_preseed():
    """A SourceConfig for a file source (roots value irrelevant — cache pre-seeded)."""
    return SourceConfig(adapter="file", options={"roots": ("/irrelevant",)})


def _setup_preseed(monkeypatch, server, stub, source_name="file-test"):
    """Wire registry + config + pre-seed the adapter cache with *stub*."""
    cfg = _file_src_cfg_for_preseed()
    _registry_with(monkeypatch, server, {source_name: cfg})
    _config_with(monkeypatch, server, {source_name: cfg})
    monkeypatch.setitem(server._adapter_instances, source_name, stub)


@pytest.mark.asyncio
async def test_smart_summarize_window_reaches_adapter_with_start_time(
        server, monkeypatch):
    """smart_summarize_pod_logs: derived window.start reaches fetch_logs.

    When start_time='2026-01-01T00:00:02Z' is passed, make_time_window
    derives start=datetime(2026,1,1,0,0,2, tzinfo=utc).  The stub captures
    the window argument; this test asserts it is NOT None and equals the
    expected UTC datetime — proving the window is live, not hardcoded (None,None).
    """
    from datetime import timezone
    from unittest.mock import MagicMock

    captured = {}

    async def _fake_fetch(entity, window, limit):
        captured["window"] = window
        return _empty_log_batch()

    stub = MagicMock()
    stub.fetch_logs = _fake_fetch

    _setup_preseed(monkeypatch, server, stub)

    await server.smart_summarize_pod_logs(
        namespace="ignored",
        pod_name="*.log",
        source="file-test",
        start_time="2026-01-01T00:00:02Z",
    )

    # Tool short-circuits at "No logs found"; error key is expected here.
    assert "window" in captured, "fetch_logs was never called"
    assert captured["window"].start == datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc), (
        f"window.start is {captured['window'].start!r}, expected 2026-01-01T00:00:02Z"
    )


@pytest.mark.asyncio
async def test_smart_summarize_no_time_params_window_is_null(
        server, monkeypatch):
    """smart_summarize_pod_logs: no time params → window == TimeWindow(None, None).

    Confirms bit-compatibility with phase 3: the file adapter receives the same
    null window when no time constraints are provided.
    """
    from unittest.mock import MagicMock

    captured = {}

    async def _fake_fetch(entity, window, limit):
        captured["window"] = window
        return _empty_log_batch()

    stub = MagicMock()
    stub.fetch_logs = _fake_fetch

    _setup_preseed(monkeypatch, server, stub)

    await server.smart_summarize_pod_logs(
        namespace="ignored",
        pod_name="*.log",
        source="file-test",
    )

    assert "window" in captured, "fetch_logs was never called"
    assert captured["window"] == TimeWindow(start=None, end=None), (
        f"expected null window, got {captured['window']!r}"
    )


@pytest.mark.asyncio
async def test_stream_analyze_window_reaches_adapter_with_start_time(
        server, monkeypatch):
    """stream_analyze_pod_logs: derived window.start reaches fetch_logs.

    Same assertion as smart_summarize: start_time='2026-01-01T00:00:02Z' must
    produce window.start == datetime(2026,1,1,0,0,2, tzinfo=utc).
    """
    from datetime import timezone
    from unittest.mock import MagicMock

    captured = {}

    async def _fake_fetch(entity, window, limit):
        captured["window"] = window
        return _empty_log_batch()

    stub = MagicMock()
    stub.fetch_logs = _fake_fetch

    _setup_preseed(monkeypatch, server, stub)

    await server.stream_analyze_pod_logs(
        namespace="ignored",
        pod_name="*.log",
        source="file-test",
        start_time="2026-01-01T00:00:02Z",
    )

    assert "window" in captured, "fetch_logs was never called"
    assert captured["window"].start == datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc), (
        f"window.start is {captured['window'].start!r}, expected 2026-01-01T00:00:02Z"
    )


@pytest.mark.asyncio
async def test_stream_analyze_no_time_params_window_is_null(
        server, monkeypatch):
    """stream_analyze_pod_logs: no time params → window == TimeWindow(None, None).

    Confirms bit-compatibility with phase 3 for the stream tool.
    """
    from unittest.mock import MagicMock

    captured = {}

    async def _fake_fetch(entity, window, limit):
        captured["window"] = window
        return _empty_log_batch()

    stub = MagicMock()
    stub.fetch_logs = _fake_fetch

    _setup_preseed(monkeypatch, server, stub)

    await server.stream_analyze_pod_logs(
        namespace="ignored",
        pod_name="*.log",
        source="file-test",
    )

    assert "window" in captured, "fetch_logs was never called"
    assert captured["window"] == TimeWindow(start=None, end=None), (
        f"expected null window, got {captured['window']!r}"
    )


# ─── Phase 4 Task 1: dispatch-spine generalization tests ─────────────────────


def test_unregistered_adapter_type_raises_adapter_error(server, monkeypatch, tmp_path):
    """_get_adapter_instance raises AdapterError for a missing required option.

    'loki' IS registered in ADAPTER_FACTORIES (added in phase 4 Task 3).
    This test uses a SourceConfig with adapter="loki" but NO url option —
    the loki factory requires url and raises AdapterError when it is absent.
    The guard is: missing/invalid config → AdapterError, not a generic
    KeyError, AttributeError, or bare RuntimeError.
    """
    from core.errors import AdapterError
    from core.config_types import SourceConfig

    loki_cfg = SourceConfig(adapter="loki")
    _registry_with(monkeypatch, server, {"loki-src": loki_cfg})
    _config_with(monkeypatch, server, {"loki-src": loki_cfg})
    monkeypatch.setattr(server, "_adapter_instances", {})

    with pytest.raises(AdapterError, match="loki"):
        server._get_adapter_instance("loki-src")


def test_file_still_routes_via_get_adapter_instance(server, monkeypatch, tmp_path):
    """Regression: _get_adapter_instance returns a FileLogSource for file sources.

    After renaming _get_file_source -> _build_file_source and routing through
    ADAPTER_FACTORIES, file sources must still resolve to FileLogSource.
    """
    from adapters.file.logs import FileLogSource

    cfg = _file_src_cfg(tmp_path)
    _registry_with(monkeypatch, server, {"reg-file": cfg})
    _config_with(monkeypatch, server, {"reg-file": cfg})
    monkeypatch.setattr(server, "_adapter_instances", {})

    instance = server._get_adapter_instance("reg-file")
    assert isinstance(instance, FileLogSource), (
        f"expected FileLogSource, got {type(instance)!r}"
    )


def test_path_outside_roots_is_instance_of_adapter_error():
    """PathOutsideRoots must subclass AdapterError (verified via isinstance).

    This is the catch-widening prerequisite: catching AdapterError in tool
    bodies must also catch PathOutsideRoots raised by the file adapter.
    """
    from core.errors import AdapterError
    from adapters.file.roots import PathOutsideRoots

    exc = PathOutsideRoots("absolute paths are not allowed: '/escape'")
    assert isinstance(exc, AdapterError), (
        f"PathOutsideRoots does not subclass AdapterError — "
        f"MRO: {[c.__name__ for c in type(exc).__mro__]}"
    )


def test_converter_groups_by_provenance_grouping_attr_stream(server):
    """_logbatch_to_legacy_envelope reads batch.provenance.grouping_attr.

    A synthetic batch with grouping_attr='stream' and records carrying a
    'stream' attribute must group by that attribute, not by 'file'.
    A batch with two distinct stream values must produce two output keys.
    """
    from core.signals import LogBatch, LogRecord, Provenance

    records = [
        LogRecord(timestamp=None, body="line-a1",
                  attributes={"stream": "pod=api-1,ns=team-a"}),
        LogRecord(timestamp=None, body="line-a2",
                  attributes={"stream": "pod=api-1,ns=team-a"}),
        LogRecord(timestamp=None, body="line-b1",
                  attributes={"stream": "pod=api-2,ns=team-a"}),
    ]
    prov = Provenance(adapter="loki", query={}, grouping_attr="stream")
    batch = LogBatch(records=records, provenance=prov)

    result = server._logbatch_to_legacy_envelope(batch)

    assert result == {
        "logs": {
            "pod=api-1,ns=team-a": "line-a1\nline-a2",
            "pod=api-2,ns=team-a": "line-b1",
        }
    }, f"unexpected grouping result: {result!r}"
