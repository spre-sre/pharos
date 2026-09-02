"""Task 2b seam tests — delegating generic tools.

Covers: detect_anomalies, manage_prediction_training_data, predictive_log_analyzer,
        analyze_pod_logs_hybrid, live_system_topology_mapper.

Each tool currently leads with _gate_source, which rejects named kubernetes sources
with a phase-3 error and never reaches the internal helper call.  After conversion
the internal call is invoked with source= propagated.

RED: before conversion _gate_source returns a phase-3 error → internal call is never
reached → captured is empty → assertion fails.

GREEN: after conversion the two-path or simple _resolve_k8s pattern is used → internal
call receives source='fake-k8s-XXX'.
"""
import asyncio
import importlib.util
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


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_2b") / "config"
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
        "server_mcp_2b", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_2b"] = mod
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


def _register_fake_instance(server, name):
    """Register a named kubernetes instance and inject a sentinel K8sClientSet.

    Returns the injected K8sClientSet so callers can identity-check its fields.
    """
    from core.registry import SourceEntry, ADAPTER_CAPABILITIES

    entry = SourceEntry(
        name=name,
        adapter="kubernetes",
        capabilities=ADAPTER_CAPABILITIES["kubernetes"],
        state="configured",
        default=False,
    )
    if name not in server._source_registry._entries:
        server._source_registry.add_instance(entry)

    sentinel_core = object()
    sentinel_custom = object()
    fake_cs = server.K8sClientSet(
        core_api=sentinel_core,
        apps_api=object(),
        custom_api=sentinel_custom,
        batch_api=object(),
        storage_api=object(),
        networking_api=object(),
        autoscaling_api=object(),
        apis_api=object(),
    )
    server._k8s_instances[name] = fake_cs
    return fake_cs


# ---------------------------------------------------------------------------
# detect_anomalies: source threads to list_pipelineruns
# ---------------------------------------------------------------------------


def test_detect_anomalies_threads_source_to_list_pipelineruns(server, monkeypatch):
    """detect_anomalies must pass source= to list_pipelineruns.

    RED: before conversion _gate_source returns a phase-3 error for named
    kubernetes sources; list_pipelineruns is never called so captured is empty
    and the assertion fails.

    GREEN: after conversion the simple _resolve_k8s pattern is used; the tool
    calls list_pipelineruns(namespace, source='fake-k8s-da').
    """
    _register_fake_instance(server, "fake-k8s-da")

    captured = {}

    async def fake_list_pipelineruns(namespace, limit=200, source=""):
        captured["source"] = source
        captured["namespace"] = namespace
        return []

    async def fake_list_taskruns(namespace, pipeline_run=None, source=""):
        return []

    monkeypatch.setattr(server, "list_pipelineruns", fake_list_pipelineruns)
    monkeypatch.setattr(server, "list_taskruns", fake_list_taskruns)

    asyncio.run(
        server.detect_anomalies("test-ns", source="fake-k8s-da")
    )

    assert captured.get("source") == "fake-k8s-da", (
        f"Expected source='fake-k8s-da' threaded to list_pipelineruns, "
        f"got captured={captured!r}. "
        "Tool must use _resolve_k8s pattern, not bare _gate_source."
    )


# ---------------------------------------------------------------------------
# manage_prediction_training_data: source threads to list_pipelineruns (collect)
# ---------------------------------------------------------------------------


def test_manage_prediction_training_data_threads_source_to_list_pipelineruns(server, monkeypatch):
    """manage_prediction_training_data must pass source= to list_pipelineruns.

    The 'collect' action with explicit collect_from_namespaces skips auto-detection
    and goes straight to the per-namespace loop where list_pipelineruns is called.

    RED: before conversion _gate_source returns a phase-3 error; list_pipelineruns
    is never called so captured is empty and the assertion fails.

    GREEN: after conversion list_pipelineruns(namespace=ns, source='fake-k8s-mpt')
    is called inside the collect loop.
    """
    _register_fake_instance(server, "fake-k8s-mpt")

    captured = {}

    async def fake_list_pipelineruns(namespace, limit=200, source=""):
        captured["source"] = source
        captured["namespace"] = namespace
        return []

    async def fake_events_as_dicts(namespace, limit=100, time_period=None, clients=None):
        return []

    monkeypatch.setattr(server, "list_pipelineruns", fake_list_pipelineruns)
    monkeypatch.setattr(server, "_get_namespace_events_as_dicts", fake_events_as_dicts)

    asyncio.run(
        server.manage_prediction_training_data(
            action="collect",
            collect_from_namespaces=["fake-ns"],
            source="fake-k8s-mpt",
        )
    )

    assert captured.get("source") == "fake-k8s-mpt", (
        f"Expected source='fake-k8s-mpt' threaded to list_pipelineruns, "
        f"got captured={captured!r}. "
        "Tool must use two-path resolve-or-gate pattern, not bare _gate_source."
    )


# ---------------------------------------------------------------------------
# predictive_log_analyzer: source threads to list_namespaces
# ---------------------------------------------------------------------------


def test_predictive_log_analyzer_threads_source_to_list_namespaces(server, monkeypatch):
    """predictive_log_analyzer must pass source= to list_namespaces.

    The 'pods' log_source branch auto-detects namespaces by calling list_namespaces
    and detect_tekton_namespaces when no namespaces are provided.

    RED: before conversion _gate_source returns a phase-3 error; list_namespaces
    is never called so captured is empty and the assertion fails.

    GREEN: after conversion list_namespaces(source='fake-k8s-pla') is called in
    the pods branch, plus the 'for source in log_sources' variable-shadowing bug is
    fixed to 'for _log_source in log_sources' so the outer source param is preserved.
    """
    _register_fake_instance(server, "fake-k8s-pla")

    captured = {}

    async def fake_list_namespaces(limit=500, source=""):
        captured["source"] = source
        # Return one namespace so the loop runs but logs stay empty → early return
        return ["test-ns"]

    async def fake_detect_tekton_namespaces(source=""):
        # Return empty dict → no tekton namespaces → falls back to all_ns
        return {}

    async def fake_events_as_dicts(namespace, limit=100, time_period=None, clients=None):
        return []

    monkeypatch.setattr(server, "list_namespaces", fake_list_namespaces)
    monkeypatch.setattr(server, "detect_tekton_namespaces", fake_detect_tekton_namespaces)
    monkeypatch.setattr(server, "_get_namespace_events_as_dicts", fake_events_as_dicts)

    asyncio.run(
        server.predictive_log_analyzer(
            log_sources=["pods"],
            source="fake-k8s-pla",
        )
    )

    assert captured.get("source") == "fake-k8s-pla", (
        f"Expected source='fake-k8s-pla' threaded to list_namespaces, "
        f"got captured={captured!r}. "
        "Tool must use two-path resolve-or-gate pattern and rename the loop variable "
        "'for source in log_sources' to avoid shadowing the function parameter."
    )


# ---------------------------------------------------------------------------
# analyze_pod_logs_hybrid: source threads through _HYBRID_STRATEGIES
# ---------------------------------------------------------------------------


def test_analyze_pod_logs_hybrid_threads_source_to_strategies(server, monkeypatch):
    """analyze_pod_logs_hybrid must include source= in the strategy_params dict.

    The strategy is executed via _HYBRID_STRATEGIES['summarize'](**strategy_params)
    or _HYBRID_STRATEGIES['stream'](**strategy_params).  After conversion, source
    is added to the strategy_params.update({...}) block so the delegated tool
    receives it.

    RED: before conversion _gate_source returns a phase-3 error for named
    kubernetes sources; _HYBRID_STRATEGIES['summarize'] is never called so
    captured is empty and the assertion fails.

    GREEN: after conversion strategy_params includes 'source', so fake_summarize
    receives source='fake-k8s-alh'.
    """
    _register_fake_instance(server, "fake-k8s-alh")

    captured = {}

    async def fake_summarize(**kwargs):
        captured.update(kwargs)
        return {"analysis": "fake"}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        return {"chunks": [], "overall_summary": {}}

    monkeypatch.setitem(server._HYBRID_STRATEGIES, "summarize", fake_summarize)
    monkeypatch.setitem(server._HYBRID_STRATEGIES, "stream", fake_stream)

    asyncio.run(
        server.analyze_pod_logs_hybrid(
            "test-ns",
            "test-pod",
            strategy="smart_summary",
            source="fake-k8s-alh",
        )
    )

    assert captured.get("source") == "fake-k8s-alh", (
        f"Expected source='fake-k8s-alh' in strategy_params passed to strategy, "
        f"got captured={captured!r}. "
        "Tool must add 'source': source to strategy_params.update({...}) and use "
        "_resolve_k8s pattern, not bare _gate_source."
    )


# ---------------------------------------------------------------------------
# live_system_topology_mapper: instance clients reach get_multi_cluster_topology_clients
# ---------------------------------------------------------------------------


def test_live_system_topology_mapper_threads_clients_to_topology_helper(server, monkeypatch):
    """live_system_topology_mapper must pass resolved instance clients to the topology helper.

    get_multi_cluster_topology_clients is called with explicit k8s client params.
    After conversion those params must come from the resolved _clients (not module globals).

    RED: before conversion _gate_source returns a phase-3 error for named
    kubernetes sources; get_multi_cluster_topology_clients is never called so
    captured is empty and the assertion fails.

    GREEN: after conversion the simple _resolve_k8s pattern is used; the helper
    receives _clients.core_api (the sentinel) as its first positional argument.
    """
    fake_cs = _register_fake_instance(server, "fake-k8s-topo")

    captured = {}

    async def fake_topology_clients(core_api, custom_api, apps_api, storage_api, batch_api):
        captured["core_api"] = core_api
        captured["custom_api"] = custom_api
        # Return empty dict → tool exits early with 'no clients' error dict
        return {}

    monkeypatch.setattr(server, "get_multi_cluster_topology_clients", fake_topology_clients)

    asyncio.run(
        server.live_system_topology_mapper(source="fake-k8s-topo")
    )

    assert captured.get("core_api") is fake_cs.core_api, (
        f"core_api passed to get_multi_cluster_topology_clients must be the fake "
        f"instance's core_api sentinel (id={id(fake_cs.core_api):#x}), "
        f"got: {captured.get('core_api')!r}. "
        "Tool must use _clients.core_api, not the module global k8s_core_api."
    )
    assert captured.get("custom_api") is fake_cs.custom_api, (
        f"custom_api passed to topology helper must be the fake instance's "
        f"custom_api sentinel (id={id(fake_cs.custom_api):#x}), "
        f"got: {captured.get('custom_api')!r}. "
        "Tool must use _clients.custom_api, not the module global k8s_custom_api."
    )
