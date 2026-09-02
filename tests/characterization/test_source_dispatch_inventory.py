"""Source-dispatch inventory tripwire: pins every parity tool's dispatch category.

Classifier recipe (round-4-reviewed; follow exactly):
  - Resolve _CANONICAL_ALIASES (OLD→canonical); the function def lives under the
    OLD name — e.g. live_system_topology_mapper is the def, topology_mapper the
    additive registration.  Both names inherit the OLD body's category.
  - The 2 konflux tools' bodies live in src/extensions/konflux/tools.py (closures
    inside make_ci_cd_performance_baselining_tool / make_pipeline_tracer).
  - The 10 undecorated server defs registered by the tekton/openshift extensions
    have their bodies in server-mcp.py.  Names come from the TOOLS tuples in
    src/extensions/tekton/__init__.py and src/extensions/openshift/__init__.py.
  - DISPATCHING: body contains _resolve_k8s(, _route_log_source(, or for_instance(.
  - GATE_ONLY: _gate_source( in body but no dispatch marker.
  - NO_SOURCE: no dispatch marker and no _gate_source(.
  - N/A: connect_cluster, disconnect_cluster, list_sources, refresh_capabilities (registry tools; no body check).

Update contract: any conversion from GATE_ONLY/NO_SOURCE → DISPATCHING must update
the frozenset pins below in the SAME commit as the source change.  Moving a name
from one pinned set to another is the documented approval step.

Completion criterion (end-state):
    GATE_ONLY == TEXT_ONLY_ALLOWLIST  and  NO_SOURCE == empty
Text-only tools (analyze_logs, detect_log_anomalies) stay GATE_ONLY FOREVER with a
provenance-only gate: unknown source → canonical unknown-source error; any registered
source accepted as declared provenance (no capability or adapter check); "" → legacy
pass-through as always.  They are pinned via TEXT_ONLY_ALLOWLIST asserted separately
from DISPATCHING.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_SERVER_SRC = REPO_ROOT / "src" / "server-mcp.py"
_KONFLUX_TOOLS_SRC = REPO_ROOT / "src" / "extensions" / "konflux" / "tools.py"
_TEKTON_INIT = REPO_ROOT / "src" / "extensions" / "tekton" / "__init__.py"
_OPENSHIFT_INIT = REPO_ROOT / "src" / "extensions" / "openshift" / "__init__.py"
_PARITY_REF = REPO_ROOT / "tests" / "characterization" / "parity_reference.json"

# ---------------------------------------------------------------------------
# Classifier constants (kept in sync with server-mcp.py; pinned by this test)
# ---------------------------------------------------------------------------

_NA_TOOLS: frozenset[str] = frozenset({"connect_cluster", "disconnect_cluster", "list_sources", "refresh_capabilities"})

# Permanently GATE_ONLY: pure text processors whose source= param is provenance,
# not a cluster selector.  Asserted separately below (test_text_only_allowlist).
TEXT_ONLY_ALLOWLIST: frozenset[str] = frozenset({"analyze_logs", "detect_log_anomalies"})

# Aliases: OLD def name → canonical parity name (both inherit OLD body's category).
_CANONICAL_ALIASES: dict[str, str] = {
    "analyze_pod_logs_hybrid": "analyze_logs_hybrid",
    "live_system_topology_mapper": "topology_mapper",
    "prometheus_query": "query_metrics",
    "smart_get_namespace_events": "get_events_smart",
    "smart_summarize_pod_logs": "smart_summarize_logs",
    "stream_analyze_pod_logs": "stream_analyze_logs",
}

# Konflux tool closure names (bodies in _KONFLUX_TOOLS_SRC)
_KONFLUX_TOOL_NAMES: frozenset[str] = frozenset({
    "ci_cd_performance_baselining_tool",
    "pipeline_tracer",
})

# ---------------------------------------------------------------------------
# Pinned expected sets — update when conversions land (same commit as code)
# ---------------------------------------------------------------------------

_PINNED_DISPATCHING: frozenset[str] = frozenset({
    "adaptive_namespace_investigation",
    "advanced_event_analytics",
    "analyze_failed_pipeline",
    "analyze_logs_hybrid",
    "analyze_pod_logs_hybrid",
    "automated_triage_rca_report_generator",
    "check_cluster_certificate_health",
    "check_resource_constraints",
    "ci_cd_performance_baselining_tool",
    "conservative_namespace_overview",
    "detect_anomalies",
    "find_pipeline",
    "get_etcd_logs",
    "get_events_smart",
    "get_kubernetes_resource",
    "get_machine_config_pool_status",
    "get_openshift_cluster_operator_status",
    "get_pipelinerun_logs",
    "get_tekton_pipeline_runs_status",
    "investigate_tls_certificate_issues",
    "list_namespaces",
    "list_pipelineruns",
    "list_pods_in_namespace",
    "list_recent_pipeline_runs",
    "list_taskruns",
    "live_system_topology_mapper",
    "manage_prediction_training_data",
    "pipeline_tracer",
    "predictive_log_analyzer",
    "progressive_event_analysis",
    "prometheus_query",
    "query_kubearchive",
    "query_metrics",
    "resource_bottleneck_forecaster",
    "search_resources_by_labels",
    "semantic_log_search",
    "smart_get_namespace_events",
    "smart_summarize_logs",
    "smart_summarize_pod_logs",
    "stream_analyze_logs",
    "stream_analyze_pod_logs",
    "topology_mapper",
    "what_if_scenario_simulator",
})

# Independent pin: same members as TEXT_ONLY_ALLOWLIST but spelled literally so
# the two pins can diverge if one is edited — an independent tripwire.
_PINNED_GATE_ONLY: frozenset[str] = frozenset({"analyze_logs", "detect_log_anomalies"})

_PINNED_NO_SOURCE: frozenset[str] = frozenset()

_PINNED_NA: frozenset[str] = frozenset({
    "connect_cluster",
    "disconnect_cluster",
    "list_sources",
    "refresh_capabilities",
})

_PINNED_COUNTS = {
    "DISPATCHING": 43,
    "GATE_ONLY": 2,
    "NO_SOURCE": 0,
    "N/A": 4,
}

# ---------------------------------------------------------------------------
# Classifier implementation
# ---------------------------------------------------------------------------


def _extract_function_bodies(src_path: pathlib.Path) -> dict[str, str]:
    """Return {name: full_source_text} for every top-level FunctionDef/AsyncFunctionDef."""
    src_text = src_path.read_text(encoding="utf-8")
    src_lines = src_text.splitlines()
    tree = ast.parse(src_text)
    bodies: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies[node.name] = "\n".join(src_lines[node.lineno - 1 : node.end_lineno])
    return bodies


def _extract_closure_bodies(src_path: pathlib.Path, closure_names: frozenset[str]) -> dict[str, str]:
    """Return bodies for inner functions (closures) whose names are in closure_names."""
    src_text = src_path.read_text(encoding="utf-8")
    src_lines = src_text.splitlines()
    tree = ast.parse(src_text)
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in closure_names
        ):
            bodies[node.name] = "\n".join(src_lines[node.lineno - 1 : node.end_lineno])
    return bodies


def _body_category(name: str, body: str) -> str:
    """Classify a single tool by body inspection.

    Dispatch markers (any one → DISPATCHING):
      _resolve_k8s(, _route_log_source(, for_instance(
    Gate marker (GATE_ONLY when no dispatch marker present):
      _gate_source(
    Everything else → NO_SOURCE.
    N/A tools are handled before this function is called.
    """
    if "_resolve_k8s(" in body or "_route_log_source(" in body or "for_instance(" in body:
        return "DISPATCHING"
    if "_gate_source(" in body:
        return "GATE_ONLY"
    return "NO_SOURCE"


def _classify_all() -> dict[str, frozenset[str]]:
    """Run the classifier and return {category: frozenset[parity_name]}."""
    import json

    parity_names: frozenset[str] = frozenset(
        json.loads(_PARITY_REF.read_text(encoding="utf-8"))["tools"].keys()
    )

    # Collect all function bodies
    server_bodies = _extract_function_bodies(_SERVER_SRC)
    konflux_bodies = _extract_closure_bodies(_KONFLUX_TOOLS_SRC, _KONFLUX_TOOL_NAMES)
    all_bodies: dict[str, str] = {**server_bodies, **konflux_bodies}

    # Reverse alias map: canonical → OLD (to look up body by OLD name)
    canonical_to_old: dict[str, str] = {v: k for k, v in _CANONICAL_ALIASES.items()}

    result: dict[str, list[str]] = {"DISPATCHING": [], "GATE_ONLY": [], "NO_SOURCE": [], "N/A": []}

    for parity_name in parity_names:
        if parity_name in _NA_TOOLS:
            result["N/A"].append(parity_name)
            continue

        # Find the body: canonical aliases use the OLD def name
        def_name = canonical_to_old.get(parity_name, parity_name)
        body = all_bodies.get(def_name, "")
        cat = _body_category(parity_name, body)
        result[cat].append(parity_name)

    return {cat: frozenset(names) for cat, names in result.items()}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSourceDispatchInventory:
    """Tripwire: pin the dispatch category of every parity tool (43/2/0/3)."""

    def test_dispatching_matches_pin(self):
        """DISPATCHING set must equal the 43-name pin (Task 6: +query_kubearchive/what_if_scenario_simulator; text-only tools are GATE_ONLY, not here).

        Fails when a body gains _resolve_k8s/for_instance without updating this pin,
        or when a name is accidentally removed from the pin.
        """
        actual = _classify_all()["DISPATCHING"]
        added = actual - _PINNED_DISPATCHING
        removed = _PINNED_DISPATCHING - actual
        assert actual == _PINNED_DISPATCHING, (
            f"DISPATCHING set drifted from pin.\n"
            f"  Added   (in src, not in pin): {sorted(added)}\n"
            f"  Removed (in pin, not in src): {sorted(removed)}"
        )

    def test_gate_only_matches_pin(self):
        """GATE_ONLY set must equal TEXT_ONLY_ALLOWLIST — the two permanently-gate-only text processors."""
        actual = _classify_all()["GATE_ONLY"]
        added = actual - _PINNED_GATE_ONLY
        removed = _PINNED_GATE_ONLY - actual
        assert actual == _PINNED_GATE_ONLY, (
            f"GATE_ONLY set drifted from pin.\n"
            f"  Added   (in src, not in pin): {sorted(added)}\n"
            f"  Removed (in pin, not in src): {sorted(removed)}"
        )

    def test_no_source_matches_pin(self):
        """NO_SOURCE set must equal the empty pin (Task 6: all tools converted)."""
        actual = _classify_all()["NO_SOURCE"]
        added = actual - _PINNED_NO_SOURCE
        removed = _PINNED_NO_SOURCE - actual
        assert actual == _PINNED_NO_SOURCE, (
            f"NO_SOURCE set drifted from pin.\n"
            f"  Added   (in src, not in pin): {sorted(added)}\n"
            f"  Removed (in pin, not in src): {sorted(removed)}"
        )

    def test_na_matches_pin(self):
        """N/A set must equal the 3-name pin."""
        actual = _classify_all()["N/A"]
        added = actual - _PINNED_NA
        removed = _PINNED_NA - actual
        assert actual == _PINNED_NA, (
            f"N/A set drifted from pin.\n"
            f"  Added   (in src, not in pin): {sorted(added)}\n"
            f"  Removed (in pin, not in src): {sorted(removed)}"
        )

    def test_counts_match_expected(self):
        """Category counts must equal 43/2/0/3 = 48 (feature-complete; text tools pin as GATE_ONLY)."""
        actual = _classify_all()
        for cat, expected in _PINNED_COUNTS.items():
            assert len(actual[cat]) == expected, (
                f"{cat}: expected {expected} tools, got {len(actual[cat])}: "
                f"{sorted(actual[cat])}"
            )

    def test_all_parity_names_covered(self):
        """Every parity name appears in exactly one category (no gaps, no overlaps)."""
        import json

        parity_names = frozenset(
            json.loads(_PARITY_REF.read_text(encoding="utf-8"))["tools"].keys()
        )
        actual = _classify_all()
        covered = frozenset().union(*actual.values())
        missing = parity_names - covered
        extra = covered - parity_names
        assert not missing, f"Parity names not classified: {sorted(missing)}"
        assert not extra, f"Classified names not in parity reference: {sorted(extra)}"
        # Verify no overlaps
        all_names = list(covered)
        total = sum(len(v) for v in actual.values())
        assert total == len(covered), "Some names appear in multiple categories"

    def test_text_only_allowlist_is_gate_only_set(self):
        """TEXT_ONLY_ALLOWLIST IS the GATE_ONLY set — an assertion separate from
        _PINNED_DISPATCHING so any accidental conversion of either text tool is
        caught by two pins simultaneously.

        These tools (analyze_logs, detect_log_anomalies) operate on caller-supplied
        text.  Their source= param is declared provenance, nothing to dispatch.
        They hold a provenance-only gate: unknown source → canonical unknown-source
        error; any registered source accepted silently; "" → legacy pass-through.
        """
        actual = _classify_all()
        assert actual["GATE_ONLY"] == TEXT_ONLY_ALLOWLIST, (
            f"GATE_ONLY must equal TEXT_ONLY_ALLOWLIST exactly.\n"
            f"  GATE_ONLY:          {sorted(actual['GATE_ONLY'])}\n"
            f"  TEXT_ONLY_ALLOWLIST: {sorted(TEXT_ONLY_ALLOWLIST)}"
        )

    def test_end_state_completion_criterion(self):
        """End-state criterion: GATE_ONLY == TEXT_ONLY_ALLOWLIST and NO_SOURCE == empty.

        Text-only tools are permanently GATE_ONLY (provenance-only gate, never
        dispatched).  All other tools must be wired (NO_SOURCE empty).
        """
        actual = _classify_all()
        assert actual["GATE_ONLY"] == TEXT_ONLY_ALLOWLIST, (
            f"GATE_ONLY must equal TEXT_ONLY_ALLOWLIST (permanently gate-only).\n"
            f"  Unexpected in GATE_ONLY: {sorted(actual['GATE_ONLY'] - TEXT_ONLY_ALLOWLIST)}\n"
            f"  Missing from GATE_ONLY: {sorted(TEXT_ONLY_ALLOWLIST - actual['GATE_ONLY'])}"
        )
        assert actual["NO_SOURCE"] == frozenset(), (
            f"NO_SOURCE should be empty once all conversions are done.\n"
            f"Still pending: {sorted(actual['NO_SOURCE'])}"
        )

    def test_non_vacuity_mispin_detected(self):
        """Prove the classifier is not vacuous: moving list_namespaces to the wrong
        category must cause a mismatch between the real classification and the pin.

        This verifies the guard would catch an incorrect pin update.
        """
        actual = _classify_all()

        # list_namespaces must be DISPATCHING (has _resolve_k8s in body)
        assert "list_namespaces" in actual["DISPATCHING"], (
            "list_namespaces should be DISPATCHING — test setup assumption violated"
        )

        # Simulate a wrong pin: move list_namespaces from DISPATCHING to GATE_ONLY
        wrong_dispatching = _PINNED_DISPATCHING - {"list_namespaces"}
        wrong_gate_only = _PINNED_GATE_ONLY | {"list_namespaces"}

        # The wrong pin must NOT match the actual classification
        assert actual["DISPATCHING"] != wrong_dispatching, (
            "Mis-pin was not detected — classifier is vacuous"
        )
        assert actual["GATE_ONLY"] != wrong_gate_only, (
            "Mis-pin was not detected — classifier is vacuous"
        )
