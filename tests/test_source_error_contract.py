"""Source-error contract tests (F-01, F-08, M-pinned).

Step 0: _gate_source path — assert ':6443/' not in str(result) for analyze_logs.
Step 1: _resolve_k8s path — assert ':6443/' not in result["error"] for list_namespaces.
Step 2: string render path — assert ':6443/' not in result for get_kubernetes_resource.
Step 3: annotation shape — assert isinstance(result, dict) for list_namespaces.
Step 4: parametrized regression over all 41 source-accepting tools.

Pre-fix: Steps 0-4 FAIL (registry join / resolve error enumerates endpoint names).
Post-fix: all PASS.
"""
import importlib.util
import inspect
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

_FAKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1"}
  name: fake
contexts:
- context: {cluster: fake, user: fake}
  name: fake
current-context: fake
users:
- name: fake
  user: {token: "fake-token"}
"""

# ── Server fixture (module-scoped, mirrors test_output_bounding.py:86-140) ───

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_src_error") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_src_error", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_src_error"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    if _orig_kube_loc is not None:
        try:
            from kubernetes.config import kube_config as _k8s_kube_config
            _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = _orig_kube_loc
        except Exception:
            pass
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


def _make_fake_entry(server):
    """Build a SourceEntry for an endpoint-shaped kubernetes-adapter source."""
    from core.registry import SourceEntry
    k8s_caps = server._source_registry.get("kubernetes").capabilities
    return SourceEntry(
        name="api-fake:6443/testuser",
        adapter="kubernetes",
        capabilities=k8s_caps,
        state="configured",
    )


def _rendered(r) -> str:
    """Render a tool result to a string covering the entire result.

    Handles str (returned by get_kubernetes_resource), dict (most error returns),
    and list (returned by list_pipelineruns / list_taskruns / list_pods_in_namespace).
    The full result is rendered so that any endpoint-shaped name in any key is caught,
    including known_kubernetes_instances (which must now be an empty list, not populated).
    """
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        return str(r)
    if isinstance(r, dict):
        return str(r)
    return str(r)


# ── Step 0: _gate_source path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_source_path_no_endpoint_name_in_error(server, monkeypatch):
    """_gate_source('wrong-source-xyz') must NOT embed endpoint-shaped names in result.

    Pre-fix: registry.get() KeyError enumerates all known source names including
    'api-fake:6443/testuser', so ':6443/' appears in str(result). FAILS pre-fix.
    Post-fix: message is terse ('list_sources'); ':6443/' is absent. PASSES.
    """
    monkeypatch.setitem(
        server._source_registry._entries,
        "api-fake:6443/testuser",
        _make_fake_entry(server),
    )
    result = await server.analyze_logs(log_text="INFO ok", source="wrong-source-xyz")
    assert ":6443/" not in _rendered(result), (
        f"error must not embed endpoint-shaped source names; got: {result!r}"
    )


# ── Step 1: _resolve_k8s path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_k8s_path_no_endpoint_name_in_error(server, monkeypatch):
    """_resolve_k8s('wrong-source-xyz') error string must NOT embed endpoint names.

    IMPORTANT: adapter='kubernetes' is required so _resolve_k8s includes the entry
    in known_k8s (it filters by adapter). Without it the test is vacuous.

    Pre-fix failure MODE: list_namespaces returns [_err] (a list), so
    result['error'] raises TypeError — that is still a valid RED (the annotation
    is too narrow; list_namespaces should return a dict for error cases).
    Post-fix (after F-08 + F-01): result is a dict; ':6443/' not in result['error'].
    """
    monkeypatch.setitem(
        server._source_registry._entries,
        "api-fake:6443/testuser",
        _make_fake_entry(server),
    )
    result = await server.list_namespaces(source="wrong-source-xyz")
    assert isinstance(result, dict), (
        f"list_namespaces must return a dict for error cases, got {type(result).__name__}"
    )
    assert ":6443/" not in result["error"], (
        f"error string must not embed endpoint-shaped names; got: {result['error']!r}"
    )


# ── Step 2: string render path ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_string_render_path_no_endpoint_name_in_error(server, monkeypatch):
    """get_kubernetes_resource string render must NOT embed endpoint-shaped names.

    Pre-fix: known = _err['known_kubernetes_instances'] includes 'api-fake:6443/testuser',
    so ':6443/' appears in the returned string. FAILS pre-fix.
    Post-fix: message is terse ('list_sources'); ':6443/' is absent. PASSES.
    """
    monkeypatch.setitem(
        server._source_registry._entries,
        "api-fake:6443/testuser",
        _make_fake_entry(server),
    )
    result = await server.get_kubernetes_resource(
        resource_type="pod", name="p", source="wrong-source-xyz"
    )
    assert ":6443/" not in result, (
        f"string render must not embed endpoint-shaped source names; got: {result!r}"
    )


# ── Step 3: F-08 annotation shape ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_namespaces_error_returns_dict(server, monkeypatch):
    """list_namespaces(source='wrong-source-xyz') → dict (not list) for error case.

    Pre-fix: list_namespaces returns [_err] (a list). isinstance(result, dict) FAILS.
    Post-fix (F-08 fix — return _err directly): isinstance(result, dict) PASSES.
    """
    monkeypatch.setitem(
        server._source_registry._entries,
        "api-fake:6443/testuser",
        _make_fake_entry(server),
    )
    result = await server.list_namespaces(source="wrong-source-xyz")
    assert isinstance(result, dict), (
        f"list_namespaces error must be a dict (F-08); got {type(result).__name__}: {result!r}"
    )
    assert "error" in result, f"error key missing from result: {result!r}"
    assert "requested_source" in result, f"requested_source key missing: {result!r}"


# ── Step 4: parametrized regression — all 41 source-accepting tools ───────────

# Canonical names registered by _CANONICAL_ALIASES map to these module symbols.
_ALIAS_TO_MODULE: dict[str, str] = {
    "analyze_logs_hybrid": "analyze_pod_logs_hybrid",
    "get_events_smart": "smart_get_namespace_events",
    "query_metrics": "prometheus_query",
    "smart_summarize_logs": "smart_summarize_pod_logs",
    "stream_analyze_logs": "stream_analyze_pod_logs",
    "topology_mapper": "live_system_topology_mapper",
}

# Minimal required kwargs for each of the 41 source-accepting tools.
# source="bad-source-xyz" is injected by the test.
_TOOL_KWARGS: dict[str, dict] = {
    "adaptive_namespace_investigation": {"namespace": "test-ns"},
    "advanced_event_analytics": {"namespace": "test-ns"},
    "analyze_failed_pipeline": {"namespace": "test-ns", "pipeline_run": "pr-1"},
    "analyze_logs": {"log_text": "x"},
    "analyze_logs_hybrid": {"namespace": "test-ns", "pod_name": "p"},
    "analyze_pod_logs_hybrid": {"namespace": "test-ns", "pod_name": "p"},
    "automated_triage_rca_report_generator": {"failure_identifier": "test"},
    "check_cluster_certificate_health": {},
    "check_resource_constraints": {"namespace": "test-ns"},
    "conservative_namespace_overview": {"namespace": "test-ns"},
    "detect_anomalies": {"namespace": "test-ns"},
    "detect_log_anomalies": {"logs": "x"},
    "find_pipeline": {"pipeline_id_pattern": "build"},
    "get_etcd_logs": {},
    "get_events_smart": {"namespace": "test-ns"},
    "get_kubernetes_resource": {"resource_type": "pod", "name": "p"},
    "get_machine_config_pool_status": {},
    "get_openshift_cluster_operator_status": {},
    "get_pipelinerun_logs": {"pipelinerun_name": "pr-1", "namespace": "test-ns"},
    "get_tekton_pipeline_runs_status": {},
    "investigate_tls_certificate_issues": {},
    "list_namespaces": {},
    "list_pipelineruns": {"namespace": "test-ns"},
    "list_pods_in_namespace": {"namespace": "test-ns"},
    "list_recent_pipeline_runs": {},
    "list_taskruns": {"namespace": "test-ns"},
    "live_system_topology_mapper": {},
    "manage_prediction_training_data": {},
    "predictive_log_analyzer": {},
    "progressive_event_analysis": {"namespace": "test-ns"},
    "prometheus_query": {"query": "up"},
    "query_metrics": {"query": "up"},
    "resource_bottleneck_forecaster": {},
    "search_resources_by_labels": {
        "resource_types": ["pods"],
        "label_selectors": [{"key": "app", "value": "x", "operator": "equals"}],
    },
    "semantic_log_search": {"query": "test"},
    "smart_get_namespace_events": {"namespace": "test-ns"},
    "smart_summarize_logs": {"namespace": "test-ns", "pod_name": "p"},
    "smart_summarize_pod_logs": {"namespace": "test-ns", "pod_name": "p"},
    "stream_analyze_logs": {"namespace": "test-ns", "pod_name": "p"},
    "stream_analyze_pod_logs": {"namespace": "test-ns", "pod_name": "p"},
    "topology_mapper": {},
}

assert len(_TOOL_KWARGS) == 41, (
    f"_TOOL_KWARGS must cover exactly 41 source-accepting tools; got {len(_TOOL_KWARGS)}"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(_TOOL_KWARGS))
async def test_source_error_no_endpoint_names(server, monkeypatch, tool_name):
    """Every source-accepting tool must NOT embed endpoint-shaped names in its error.

    Registers 'api-fake:6443/testuser' (adapter=kubernetes) so both _gate_source
    and _resolve_k8s would include it in error enumeration if the fix were absent.
    Asserts: (a) no exception raised; (b) ':6443/' not in _rendered(result)
    (whole result, including known_kubernetes_instances which must be [] post-fix).
    Pre-fix: FAILS. Post-fix: PASSES.
    """
    monkeypatch.setitem(
        server._source_registry._entries,
        "api-fake:6443/testuser",
        _make_fake_entry(server),
    )
    module_name = _ALIAS_TO_MODULE.get(tool_name, tool_name)
    fn = getattr(server, module_name)
    kwargs = dict(_TOOL_KWARGS[tool_name])
    kwargs["source"] = "bad-source-xyz"

    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result

    rendered = _rendered(result)
    assert ":6443/" not in rendered, (
        f"{tool_name}: error must not embed endpoint-shaped source names; "
        f"rendered={rendered!r}"
    )
