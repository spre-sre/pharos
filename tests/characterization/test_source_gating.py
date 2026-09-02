"""Behavioral tests for the shared source gate (_gate_source) and, from
Tasks 3-5, per-family tool gating.  The gate is the ONLY place 2b source
semantics live: "" -> legacy path, unknown -> known-names error, incapable
-> canonical capability error, capable-but-not-default -> phase-3 routing
error."""
import sys
from pathlib import Path

import pytest

# Insert order is load-bearing: src/ MUST end up at sys.path[0].  This file
# imports core.config_types/core.registry at collection time; if tests/
# preceded src/, the tests/core/ package would shadow src/core
# (ModuleNotFoundError).  NOTE: sibling characterization files use the
# opposite order safely -- they have no module-level core.* imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.config_types import ResolvedConfig, SourceConfig
from core.registry import build_registry


def _registry_with(monkeypatch, server, sources):
    reg = build_registry(ResolvedConfig(profile="test", sources=sources))
    monkeypatch.setattr(server, "_source_registry", reg)
    return reg


def test_gate_empty_source_is_none(server):
    assert server._gate_source("analyze_logs", "", ("Log",)) is None


def test_gate_unknown_source_names_known(server, monkeypatch):
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    err = server._gate_source("analyze_logs", "nope", ("Log",))
    assert err["requested_source"] == "nope"
    assert "list_sources" in err["error"]


def test_gate_incapable_source_returns_canonical_error(server, monkeypatch):
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus")})
    err = server._gate_source("smart_summarize_pod_logs", "prometheus", ("Log",))
    assert err == {
        "error": "source 'prometheus' does not support tool 'smart_summarize_pod_logs'",
        "tool": "smart_summarize_pod_logs",
        "requested_source": "prometheus",
        "capable_sources": ["kubernetes"],
    }


def test_gate_multi_capability_requires_all(server, monkeypatch):
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus")})
    # kubernetes lacks Metric -> gated even though it has Log+Event.
    err = server._gate_source("advanced_event_analytics", "kubernetes",
                              ("Event", "Log", "Metric"))
    assert err is not None and err["capable_sources"] == []


def test_gate_default_instance_passes(server, monkeypatch):
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    assert server._gate_source("smart_summarize_pod_logs", "kubernetes", ("Log",)) is None


def test_gate_capable_nondefault_gets_phase3_error(server, monkeypatch):
    _registry_with(monkeypatch, server, {
        "k8s-a": SourceConfig(adapter="kubernetes"),
        "k8s-b": SourceConfig(adapter="kubernetes")})
    # k8s-a is default (first by name); k8s-b is capable but not routable yet.
    assert server._gate_source("t", "k8s-a", ("Log",)) is None
    err = server._gate_source("t", "k8s-b", ("Log",))
    assert err["routable"] is False and "phase 3" in err["error"]


def test_gate_cross_adapter_capable_source_gets_phase3_error(server, monkeypatch):
    """kubearchive is Log-capable but is NOT the tool's legacy backend --
    it must get the routing error, never silent kubernetes data."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "kubearchive": SourceConfig(adapter="kubearchive")})
    err = server._gate_source("smart_summarize_pod_logs", "kubearchive", ("Log",))
    assert err["routable"] is False and "phase 3" in err["error"]


# ── Task 3: log-family tool gating (8 tools) ─────────────────────────────────

import inspect  # noqa: E402  (after sys.path setup above)

from .k8s_fakes import FakeApi, POD, items_list  # noqa: E402

_LOG_SAMPLE = "ERROR: crash\nINFO: ok\n"

# Minimal k8s API surface for smart_summarize_pod_logs (same as cases.py).
_LOG_POD_API = FakeApi(
    list_namespaced_pod=items_list([POD("api-1", "team-a")]),
    read_namespaced_pod=POD("api-1", "team-a"),
    read_namespaced_pod_log=_LOG_SAMPLE,
)

# Minimal kwargs for each of the 8 log-family tools when called with source="nope".
# manage_prediction_training_data uses action="collect"; after Task 3 the gate is
# unconditional so any action would exercise it, but collect is canonical.
_LOG_FAMILY_LOOP_KWARGS = {
    "analyze_logs":                    {"log_text": _LOG_SAMPLE},
    "detect_log_anomalies":            {"logs": _LOG_SAMPLE},
    "smart_summarize_pod_logs":        {"namespace": "team-a", "pod_name": "api-1"},
    "stream_analyze_pod_logs":         {"namespace": "team-a", "pod_name": "api-1"},
    "analyze_pod_logs_hybrid":         {"namespace": "team-a", "pod_name": "api-1"},
    "semantic_log_search":             {"query": "database error"},
    "predictive_log_analyzer":         {},
    "manage_prediction_training_data": {"action": "collect",
                                        "collect_from_namespaces": ["team-a"]},
}


@pytest.mark.asyncio
async def test_log_family_smart_summarize_unknown_source(server, monkeypatch):
    """source='nope' returns unknown-source dict for smart_summarize_pod_logs."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    err = await server.smart_summarize_pod_logs(
        namespace="team-a", pod_name="api-1", source="nope")
    assert err["requested_source"] == "nope"
    assert "list_sources" in err["error"]


@pytest.mark.asyncio
async def test_log_family_smart_summarize_incapable_source(server, monkeypatch):
    """prometheus (Metric-only) cannot serve Log tools -> canonical capability error."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus"),
    })
    err = await server.smart_summarize_pod_logs(
        namespace="team-a", pod_name="api-1", source="prometheus")
    assert err == {
        "error": "source 'prometheus' does not support tool 'smart_summarize_pod_logs'",
        "tool": "smart_summarize_pod_logs",
        "requested_source": "prometheus",
        "capable_sources": ["kubernetes"],
    }


@pytest.mark.asyncio
async def test_log_family_smart_summarize_empty_source_runs_legacy(
        server, monkeypatch, tmp_path):
    """source='' passes through the gate and runs the legacy body."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    monkeypatch.setattr(server, "k8s_core_api", _LOG_POD_API)
    monkeypatch.setattr(server, "_namespace_cache", {}, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = await server.smart_summarize_pod_logs(
        namespace="team-a", pod_name="api-1", source="")
    assert "summary" in result


@pytest.mark.asyncio
async def test_log_family_manage_collect_action_gated(server, monkeypatch):
    """manage_prediction_training_data action='collect' IS gated; nope -> error."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    err = await server.manage_prediction_training_data(
        action="collect", collect_from_namespaces=["team-a"], source="nope")
    assert err["requested_source"] == "nope"
    assert "list_sources" in err["error"]


_TEXT_ONLY_TOOLS = {"analyze_logs", "detect_log_anomalies"}


@pytest.mark.asyncio
async def test_log_family_all_tools_unknown_source(server, monkeypatch):
    """Every log-family tool returns unknown-source error for source='nope' (including text-only tools)."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    for tool_name, kwargs in _LOG_FAMILY_LOOP_KWARGS.items():
        fn = getattr(server, tool_name)
        result = fn(**kwargs, source="nope")
        if inspect.isawaitable(result):
            result = await result
        assert result.get("requested_source") == "nope", (
            f"{tool_name}: expected unknown-source error, got {result!r}"
        )


@pytest.mark.asyncio
async def test_text_only_tools_provenance_gate(server, monkeypatch):
    """Text-only tools: unknown source → error; any registered source → accepted.

    Provenance-only mode (required_caps=()): unknown → canonical unknown-source
    error dict; registered source accepted silently regardless of adapter/capability
    (there is nothing to dispatch — the text is analyzed as-is).
    """
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    for tool_name in _TEXT_ONLY_TOOLS:
        kwargs = _LOG_FAMILY_LOOP_KWARGS[tool_name]
        fn = getattr(server, tool_name)

        # Unknown source → canonical unknown-source error.
        bad_result = fn(**kwargs, source="nope")
        if inspect.isawaitable(bad_result):
            bad_result = await bad_result
        assert bad_result.get("requested_source") == "nope", (
            f"{tool_name}: expected unknown-source error for 'nope', got {bad_result!r}"
        )
        assert "list_sources" in bad_result.get("error", ""), (
            f"{tool_name}: error must mention list_sources, got {bad_result!r}"
        )

        # Registered source → accepted as declared provenance, no error.
        good_result = fn(**kwargs, source="kubernetes")
        if inspect.isawaitable(good_result):
            good_result = await good_result
        assert "error" not in good_result, (
            f"{tool_name}: registered source 'kubernetes' should be accepted, got {good_result!r}"
        )


# ── Task 4: event/inventory-family tool gating (4 tools) ─────────────────────

# Minimal k8s API surface for smart_get_namespace_events (same as cases.py).
_EVENT_API = FakeApi(list_namespaced_event=items_list([]))

# Minimal kwargs for each of the 4 event/inventory tools when called with source="nope".
# Gate fires before any k8s call, so only the required positional args are needed.
_EVENT_FAMILY_LOOP_KWARGS = {
    "detect_anomalies":           {"namespace": "team-a"},
    "smart_get_namespace_events": {"namespace": "team-a"},
    "progressive_event_analysis": {"namespace": "team-a"},
    "advanced_event_analytics":   {"namespace": "team-a"},
}


@pytest.mark.asyncio
async def test_event_family_smart_events_unknown_source(server, monkeypatch):
    """source='nope' returns unknown-source dict for smart_get_namespace_events."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    err = await server.smart_get_namespace_events(namespace="team-a", source="nope")
    assert err["requested_source"] == "nope"
    assert "list_sources" in err["error"]


@pytest.mark.asyncio
async def test_event_family_smart_events_incapable_source(server, monkeypatch):
    """prometheus (Metric-only) cannot serve Event tools -> canonical capability error."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus"),
    })
    err = await server.smart_get_namespace_events(
        namespace="team-a", source="prometheus")
    assert err == {
        "error": "source 'prometheus' does not support tool 'smart_get_namespace_events'",
        "tool": "smart_get_namespace_events",
        "requested_source": "prometheus",
        "capable_sources": ["kubernetes"],
    }


@pytest.mark.asyncio
async def test_event_family_smart_events_empty_source_runs_legacy(server, monkeypatch):
    """source='' passes through the gate and runs the legacy body."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    monkeypatch.setattr(server, "k8s_core_api", _EVENT_API)
    result = await server.smart_get_namespace_events(namespace="team-a", source="")
    assert "events" in result


@pytest.mark.asyncio
async def test_event_family_all_tools_unknown_source(server, monkeypatch):
    """Every event/inventory-family tool returns the unknown-source error for source='nope'."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes")})
    for tool_name, kwargs in _EVENT_FAMILY_LOOP_KWARGS.items():
        fn = getattr(server, tool_name)
        result = fn(**kwargs, source="nope")
        if inspect.isawaitable(result):
            result = await result
        assert result.get("requested_source") == "nope", (
            f"{tool_name}: expected unknown-source error, got {result!r}"
        )


# ── Task 5: metric/multi-signal-family tool gating (4 tools) ─────────────────

from .cases import _fake_aiohttp, _fake_discover_endpoint  # noqa: E402

# Minimal kwargs for each of the 4 metric/multi-signal tools when called with
# source="nope".  Gate fires before any network call, so only required positional
# args are needed.
_METRIC_FAMILY_LOOP_KWARGS = {
    "prometheus_query":                      {"query": "up"},
    "resource_bottleneck_forecaster":        {},
    "automated_triage_rca_report_generator": {"failure_identifier": "test-run"},
    "live_system_topology_mapper":           {},
}


@pytest.mark.asyncio
async def test_metric_family_prometheus_unknown_source(server, monkeypatch):
    """source='nope' returns unknown-source dict for prometheus_query."""
    _registry_with(monkeypatch, server, {
        "prometheus": SourceConfig(adapter="prometheus")})
    err = await server.prometheus_query(query="up", source="nope")
    assert err["requested_source"] == "nope"
    assert "list_sources" in err["error"]


@pytest.mark.asyncio
async def test_metric_family_prometheus_incapable_source(server, monkeypatch):
    """Non-kubernetes source with no Metric capability returns canonical capability error.

    After Task 4 'kubernetes' adapter sources are dispatched (they can host Prometheus);
    a 'loki' adapter (Log-only, no Metric, not kubernetes) still gates correctly.
    """
    _registry_with(monkeypatch, server, {
        "loki": SourceConfig(adapter="loki"),
        "prometheus": SourceConfig(adapter="prometheus"),
    })
    err = await server.prometheus_query(query="up", source="loki")
    assert err == {
        "error": "source 'loki' does not support tool 'prometheus_query'",
        "tool": "prometheus_query",
        "requested_source": "loki",
        "capable_sources": ["prometheus"],
    }


@pytest.mark.asyncio
async def test_metric_family_prometheus_empty_source_runs_legacy(server, monkeypatch):
    """source='' passes through the gate and runs the legacy Prometheus body."""
    _registry_with(monkeypatch, server, {
        "prometheus": SourceConfig(adapter="prometheus")})
    monkeypatch.setattr(server, "_discover_prometheus_endpoint",
                        _fake_discover_endpoint, raising=False)
    monkeypatch.setattr(server, "aiohttp", _fake_aiohttp, raising=False)
    result = await server.prometheus_query(query="up", source="")
    assert "status" in result


@pytest.mark.asyncio
async def test_metric_family_resource_forecaster_any_source_incapable(
        server, monkeypatch):
    """resource_bottleneck_forecaster needs Metric+Inventory; no single source
    holds both in the konflux profile -> capable_sources: [] for any non-empty source."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus"),
    })
    err = await server.resource_bottleneck_forecaster(source="prometheus")
    assert err["capable_sources"] == []
    assert err["requested_source"] == "prometheus"


@pytest.mark.asyncio
async def test_metric_family_all_tools_unknown_source(server, monkeypatch):
    """Every metric/multi-signal-family tool returns the unknown-source error for source='nope'."""
    _registry_with(monkeypatch, server, {
        "prometheus": SourceConfig(adapter="prometheus")})
    for tool_name, kwargs in _METRIC_FAMILY_LOOP_KWARGS.items():
        fn = getattr(server, tool_name)
        result = fn(**kwargs, source="nope")
        if inspect.isawaitable(result):
            result = await result
        assert result.get("requested_source") == "nope", (
            f"{tool_name}: expected unknown-source error, got {result!r}"
        )


def test_gate_prometheus_legacy_adapter_anchor(server, monkeypatch):
    """prometheus_query is the ONLY tool gated with legacy_adapter="prometheus".
    source="prometheus" must PASS its gate (None); with the default kubernetes
    anchor it would get the phase-3 routing error -- this pins the argument
    (Task-5 review: mutation survived without it)."""
    _registry_with(monkeypatch, server, {
        "kubernetes": SourceConfig(adapter="kubernetes"),
        "prometheus": SourceConfig(adapter="prometheus")})
    assert server._gate_source("prometheus_query", "prometheus", ("Metric",),
                               legacy_adapter="prometheus") is None
    # The same call WITHOUT the anchor gets the routing error -- proving the
    # argument is what makes source="prometheus" servable.
    err = server._gate_source("prometheus_query", "prometheus", ("Metric",))
    assert err is not None and err["routable"] is False
    src = (Path(__file__).resolve().parents[2] / "src" / "server-mcp.py").read_text()
    assert 'legacy_adapter="prometheus"' in src, (
        "prometheus_query's gate call lost its legacy_adapter anchor")


# ── D10: OTLP source gating matrix (Tasks 3+5) ───────────────────────────────
#
# Full 20-row / 16-tool matrix for a configured OTLP source (Log-only capabilities).
# Three outcome classes:
#   capability_error: tool needs cap(s) OTLP doesn't have → "capable_sources" in result
#   routing_error:    Log-capable but calls _gate_source directly (not _route_log_source)
#                     → result["routable"] is False
#   served:           wired through _route_log_source → adapter ran, no error key
#
# Breakdown: 4 Event + 2 Metric + 8 Inventory capability errors;
#            4 routable:False; 2 served.


def _registry_with_otlp(monkeypatch, server):
    """Wire a single OTLP source into the server module for D10 tests."""
    cfg = ResolvedConfig(
        profile="test",
        sources={"otlp-src": SourceConfig(
            adapter="otlp",
            options={"ring_capacity": 5, "max_body_bytes": 65536},
        )},
    )
    reg = build_registry(cfg)
    monkeypatch.setattr(server, "_source_registry", reg)
    monkeypatch.setattr(server, "_lumino_config", cfg)
    return reg, cfg


@pytest.mark.parametrize("tool_name,kwargs,expected_class", [
    # ── Capability errors — Event tools (OTLP has no Event cap) ──────────────
    ("smart_get_namespace_events",   {"namespace": "ns"},                  "capability_error"),
    ("progressive_event_analysis",   {"namespace": "ns"},                  "capability_error"),
    ("advanced_event_analytics",     {"namespace": "ns"},                  "capability_error"),
    # semantic_log_search needs Log+Event; OTLP has Log but not Event → capability_error
    ("semantic_log_search",          {"query": "database error"},          "capability_error"),

    # ── Capability errors — Metric tools (OTLP has no Metric cap) ────────────
    ("prometheus_query",             {"query": "up"},                      "capability_error"),
    ("resource_bottleneck_forecaster", {},                                 "capability_error"),

    # ── Capability errors — Inventory tools (OTLP has no Inventory cap) ──────
    # detect_anomalies gates ("Inventory",) — NOT Event (reviewer finding: mislabeled)
    ("detect_anomalies",             {"namespace": "ns"},                  "capability_error"),
    ("live_system_topology_mapper",  {},                                   "capability_error"),
    # automated_triage needs Log+Event+Inventory; OTLP has Log only → capability_error
    ("automated_triage_rca_report_generator", {"failure_identifier": "t"}, "capability_error"),
    # ── Capability errors — manage_prediction_training_data (gate unconditional, Task 3) ─
    ("manage_prediction_training_data",
     {"action": "collect", "collect_from_namespaces": ["ns"]},            "capability_error"),
    ("manage_prediction_training_data", {"action": "stats"},               "capability_error"),
    ("manage_prediction_training_data", {"action": "list_failures"},       "capability_error"),
    ("manage_prediction_training_data", {"action": "add_failure"},         "capability_error"),
    ("manage_prediction_training_data", {"action": "cleanup"},             "capability_error"),

    # ── Text-only tools — provenance-only gate; OTLP IS registered → accepted ──
    # source="otlp-src" is a known/registered source → provenance gate passes;
    # body runs on the supplied text, no cluster contact.
    ("analyze_logs",        {"log_text": "ERROR ok\n"},                    "served"),
    ("detect_log_anomalies", {"logs": "ERROR ok\n"},                       "served"),

    # ── Routing errors — Log-gate-only (call _gate_source directly, not wired) ─
    ("analyze_pod_logs_hybrid", {"namespace": "ns", "pod_name": "p"},     "routing_error"),
    ("predictive_log_analyzer", {},                                        "routing_error"),

    # ── Served — wired log tools (routed to OTLP adapter via _route_log_source) ─
    ("smart_summarize_pod_logs",  {"namespace": "ns", "pod_name": "p"},   "served"),
    ("stream_analyze_pod_logs",   {"namespace": "ns", "pod_name": "p"},   "served"),
])
@pytest.mark.asyncio
async def test_d10_otlp_gating_matrix(
        tool_name, kwargs, expected_class, server, monkeypatch, tmp_path):
    """D10: full 20-row / 16-tool gating-matrix for a configured OTLP source.

    Outcome classes:
    - capability_error: result has 'capable_sources' key (incapable tool)
    - routing_error: result has 'routable': False (log-capable but unwired)
    - served: result has neither 'capable_sources' nor 'routable' (adapter ran)
    """
    _registry_with_otlp(monkeypatch, server)
    # Cold adapter-instance cache; empty ring for the OTLP source.
    monkeypatch.setattr(server, "_adapter_instances", {})
    monkeypatch.setattr(server, "_otlp_rings", {}, raising=False)
    monkeypatch.setattr(server, "_otlp_listening", False, raising=False)
    # HOME redirect needed for tools that touch the filesystem.
    monkeypatch.setenv("HOME", str(tmp_path))

    fn = getattr(server, tool_name)
    result = fn(**kwargs, source="otlp-src")
    if inspect.isawaitable(result):
        result = await result

    if expected_class == "capability_error":
        assert "capable_sources" in result, (
            f"{tool_name}: expected capability error, got {result!r}"
        )
        assert result.get("routable") is None, (
            f"{tool_name}: capability error must not have 'routable' key"
        )
    elif expected_class == "routing_error":
        assert result.get("routable") is False, (
            f"{tool_name}: expected routable:False, got {result!r}"
        )
        assert "capable_sources" not in result, (
            f"{tool_name}: routing error must not have 'capable_sources'"
        )
    else:  # "served"
        assert "capable_sources" not in result, (
            f"{tool_name}: served path must not return capability error, got {result!r}"
        )
        assert result.get("routable") is not False, (
            f"{tool_name}: served path must not return routing error"
        )


# ── Call-site client-injection pins (Task 5.8 regression guard) ──────────────

@pytest.mark.asyncio
async def test_prometheus_query_forwards_k8s_clients_to_endpoint_discovery(
        server, monkeypatch):
    """prometheus_query must pass custom_api and core_api into
    _discover_prometheus_endpoint.  Fails if either kwarg is dropped —
    K8s-based discovery (routes, CRDs, services) would be silently skipped."""
    _registry_with(monkeypatch, server, {
        "prometheus": SourceConfig(adapter="prometheus")})
    captured = {}

    async def _capture_discover(cluster_override=None, **kwargs):
        captured.update(kwargs)
        return (None, None)  # returns early; no aiohttp needed

    monkeypatch.setattr(server, "_discover_prometheus_endpoint",
                        _capture_discover, raising=False)
    await server.prometheus_query(query="up", source="")

    assert "custom_api" in captured, (
        "prometheus_query dropped custom_api before _discover_prometheus_endpoint; "
        "K8s-based route/CRD discovery will be silently skipped"
    )
    assert "core_api" in captured, (
        "prometheus_query dropped core_api before _discover_prometheus_endpoint; "
        "K8s-based service discovery will be silently skipped"
    )
