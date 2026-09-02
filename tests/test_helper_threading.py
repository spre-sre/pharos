"""Phase 2e-b Task 1: fetch-helper clients= threading tests (RED then GREEN).

Tests (a)-(d) from the brief Step 2.

Each threaded helper gains `clients: Optional[K8sClientSet] = None` and a
`_c = clients if clients is not None else _DefaultClientView()` first line;
every module-global k8s client read in the body becomes `_c.<api>`.

Fixture loads server-mcp.py once under a unique module name.
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

# Ensure tests/ is on path so we can import characterization.k8s_fakes
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from characterization.k8s_fakes import FakeApi, items_list

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
    kubeconfig = tmp_path_factory.mktemp("kube_helper_threading") / "config"
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
        "server_mcp_helper_threading", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_helper_threading"] = mod
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


def _make_cs(server, **overrides):
    """Build a K8sClientSet with all 8 fields; overrides replace defaults (FakeApi())."""
    fields = dict(
        core_api=FakeApi(), apps_api=FakeApi(), custom_api=FakeApi(),
        batch_api=FakeApi(), storage_api=FakeApi(), networking_api=FakeApi(),
        autoscaling_api=FakeApi(), apis_api=FakeApi(),
    )
    fields.update(overrides)
    return server.K8sClientSet(**fields)


# ─── (a) get_pod_logs: explicit clients= → fake core_api called, global untouched

@pytest.mark.asyncio
async def test_get_pod_logs_explicit_clients_uses_fake(server, monkeypatch):
    """get_pod_logs(..., clients=fake_cs) → get_all_pod_logs receives fake.core_api."""
    captured = {}

    async def spy_get_all_pod_logs(**kwargs):
        captured["k8s_core_api"] = kwargs.get("k8s_core_api")
        return {}

    monkeypatch.setattr(server, "get_all_pod_logs", spy_get_all_pod_logs)
    # Poison the module global: any attribute access raises AttributeError
    monkeypatch.setattr(server, "k8s_core_api", FakeApi())  # unconfigured → raises

    fake_core = FakeApi()
    fake_cs = _make_cs(server, core_api=fake_core)

    await server.get_pod_logs(namespace="ns", pod_name="pod", clients=fake_cs)

    assert "k8s_core_api" in captured, "spy_get_all_pod_logs was never called"
    # get_pod_logs wraps _c.core_api with ReadOnlyCoreV1.wrap before passing
    received = captured["k8s_core_api"]
    # The proxy's _api must be the fake's core_api (or the proxy itself if already wrapped)
    from core.readonly_client import ReadOnlyK8sClient
    assert isinstance(received, ReadOnlyK8sClient), (
        f"Expected ReadOnlyK8sClient proxy; got {type(received).__name__}"
    )
    assert received._api is fake_core, (
        f"Expected proxy wrapping fake_core; got proxy._api={received._api!r}"
    )


@pytest.mark.asyncio
async def test_get_pod_logs_no_clients_uses_module_global(server, monkeypatch):
    """get_pod_logs(...) with no clients= → get_all_pod_logs receives module global."""
    captured = {}

    async def spy_get_all_pod_logs(**kwargs):
        captured["k8s_core_api"] = kwargs.get("k8s_core_api")
        return {}

    fake_global = FakeApi()
    monkeypatch.setattr(server, "get_all_pod_logs", spy_get_all_pod_logs)
    monkeypatch.setattr(server, "k8s_core_api", fake_global)

    await server.get_pod_logs(namespace="ns", pod_name="pod")

    assert "k8s_core_api" in captured, "spy_get_all_pod_logs was never called"
    from core.readonly_client import ReadOnlyK8sClient
    received = captured["k8s_core_api"]
    assert isinstance(received, ReadOnlyK8sClient), (
        f"Expected ReadOnlyK8sClient proxy; got {type(received).__name__}"
    )
    # The proxy must wrap the module global (fake_global)
    assert received._api is fake_global, (
        f"Default path must use module-global; got proxy._api={received._api!r}"
    )


# ─── (a) _estimate_pod_log_tokens: explicit clients= → fake core_api called ──

@pytest.mark.asyncio
async def test_estimate_pod_log_tokens_explicit_clients_uses_fake(server, monkeypatch):
    """_estimate_pod_log_tokens(..., clients=fake_cs) → get_all_pod_logs receives fake.core_api."""
    captured = {}

    async def spy_get_all_pod_logs(**kwargs):
        captured["k8s_core_api"] = kwargs.get("k8s_core_api")
        return {}

    import helpers.log_analysis as _log_analysis
    monkeypatch.setattr(_log_analysis, "get_all_pod_logs", spy_get_all_pod_logs)
    monkeypatch.setattr(server, "k8s_core_api", FakeApi())  # poison

    fake_core = FakeApi()
    fake_cs = _make_cs(server, core_api=fake_core)

    await server._estimate_pod_log_tokens(
        namespace="ns", pod_name="pod", clients=fake_cs
    )

    assert "k8s_core_api" in captured, "spy_get_all_pod_logs was never called"
    # _estimate_pod_log_tokens passes _c.core_api directly to get_all_pod_logs
    # (k8s_core_api=_c.core_api — no wrapping at this call site)
    received = captured["k8s_core_api"]
    assert received is fake_core, (
        f"Expected fake_core passed directly to get_all_pod_logs; got {received!r}"
    )


@pytest.mark.asyncio
async def test_estimate_pod_log_tokens_no_clients_uses_module_global(server, monkeypatch):
    """_estimate_pod_log_tokens() with no clients= uses module-global k8s_core_api.

    The real _DefaultClientView (wired into helpers.log_analysis at server import)
    late-binds server.k8s_core_api, so patching the module global is enough.
    """
    import helpers.log_analysis as _log_analysis
    captured = {}

    async def spy_get_all_pod_logs(**kwargs):
        captured["k8s_core_api"] = kwargs.get("k8s_core_api")
        return {}

    fake_global = FakeApi()
    monkeypatch.setattr(_log_analysis, "get_all_pod_logs", spy_get_all_pod_logs)
    monkeypatch.setattr(server, "k8s_core_api", fake_global)

    await server._estimate_pod_log_tokens(namespace="ns", pod_name="pod")

    assert "k8s_core_api" in captured, "spy_get_all_pod_logs was never called"
    assert captured["k8s_core_api"] is fake_global, (
        f"Default path must use module-global; got {captured['k8s_core_api']!r}"
    )


# ─── (c) _quick_volume_estimate: forwarding clients= into get_pod_logs ────────

@pytest.mark.asyncio
async def test_quick_volume_estimate_forwards_clients_to_get_pod_logs(server, monkeypatch):
    """_quick_volume_estimate(..., clients=fake) → get_pod_logs receives clients=fake.

    Seam test: monkeypatch.setattr(server, 'get_pod_logs', spy) is intercepted
    because the production call site passes get_logs_fn=get_pod_logs by bare name;
    at call time 'get_pod_logs' resolves to server.get_pod_logs (the spy).
    """
    captured = {}

    async def spy_get_pod_logs(**kwargs):
        captured["clients"] = kwargs.get("clients")
        return {"logs": {}}

    monkeypatch.setattr(server, "get_pod_logs", spy_get_pod_logs)
    monkeypatch.setattr(server, "k8s_core_api", FakeApi())  # poison

    fake_cs = _make_cs(server)
    await server._quick_volume_estimate(namespace="ns", pod_name="pod", clients=fake_cs, get_logs_fn=server.get_pod_logs)

    assert "clients" in captured, "spy_get_pod_logs was never called"
    assert captured["clients"] is fake_cs, (
        f"Expected clients forwarded as fake_cs; got {captured['clients']!r}"
    )


@pytest.mark.asyncio
async def test_quick_volume_estimate_no_clients_none_forwarded(server, monkeypatch):
    """_quick_volume_estimate() with no clients= forwards clients=None to get_pod_logs."""
    captured = {}

    async def spy_get_pod_logs(**kwargs):
        captured["clients"] = kwargs.get("clients", "NOT_PRESENT")
        return {"logs": {}}

    monkeypatch.setattr(server, "get_pod_logs", spy_get_pod_logs)

    await server._quick_volume_estimate(namespace="ns", pod_name="pod", get_logs_fn=server.get_pod_logs)

    assert "clients" in captured, "spy_get_pod_logs was never called"
    # With clients=None (default), get_pod_logs receives clients=None
    assert captured["clients"] is None, (
        f"Default path must pass clients=None to get_pod_logs; got {captured['clients']!r}"
    )


@pytest.mark.asyncio
async def test_quick_volume_estimate_seam_via_production_path(server, monkeypatch):
    """monkeypatch.setattr(server, 'get_pod_logs', spy) intercepts via the production path.

    smart_summarize_pod_logs (adaptive mode) → _quick_volume_estimate(..., get_logs_fn=get_pod_logs).
    The call site passes get_pod_logs by BARE NAME; at call time the name resolves to
    server.get_pod_logs (the spy). This test FAILS under a frozen-binding mutation
    (get_logs_fn=_FROZEN_GPL at import time) because the frozen alias holds the original
    function, not the spy — so since_seconds=300 never appears in calls.

    Non-vacuity: replacing the bare-name call site with an import-time alias makes
    only this test fail (the two direct-call tests above remain green).
    """
    calls = []

    async def spy_get_pod_logs(**kwargs):
        calls.append(dict(kwargs))
        return {"logs": {"main": "line1"}}

    monkeypatch.setattr(server, "get_pod_logs", spy_get_pod_logs)

    await server.smart_summarize_pod_logs(namespace="ns-seam", pod_name="pod-seam")

    assert any(c.get("since_seconds") == 300 for c in calls), (
        f"Volume estimate (since_seconds=300) must reach spy via bare-name get_pod_logs; "
        f"calls recorded: {calls}"
    )


# ─── (a) _get_namespace_events_internal: explicit clients= → fake core_api ───

@pytest.mark.asyncio
async def test_get_namespace_events_explicit_clients_uses_fake(server, monkeypatch):
    """_get_namespace_events_internal(..., clients=fake_cs) → fake.core_api used."""
    calls = []
    fake_core = FakeApi(
        list_namespaced_event=items_list([])
    )

    # Wrap the FakeApi so it also records calls
    original_list = fake_core._methods["list_namespaced_event"]

    def recording_list(**kwargs):
        calls.append("list_namespaced_event")
        return original_list

    fake_core._methods["list_namespaced_event"] = recording_list
    fake_cs = _make_cs(server, core_api=fake_core)

    # Poison the module global
    monkeypatch.setattr(server, "k8s_core_api", FakeApi())  # unconfigured → raises on any call

    result = await server._get_namespace_events_internal(
        namespace="ns", clients=fake_cs
    )

    assert isinstance(result, dict), f"Expected dict result; got {type(result).__name__}"
    assert calls, (
        "fake_core.list_namespaced_event must have been called (fake.core_api was used)"
    )


@pytest.mark.asyncio
async def test_get_namespace_events_no_clients_uses_module_global(server, monkeypatch):
    """_get_namespace_events_internal() with no clients= uses module-global k8s_core_api."""
    calls = []
    fake_global = FakeApi(list_namespaced_event=items_list([]))

    original = fake_global._methods["list_namespaced_event"]

    def recording_list(**kwargs):
        calls.append("list_namespaced_event")
        return original

    fake_global._methods["list_namespaced_event"] = recording_list
    monkeypatch.setattr(server, "k8s_core_api", fake_global)

    result = await server._get_namespace_events_internal(namespace="ns")

    assert isinstance(result, dict), f"Expected dict result; got {type(result).__name__}"
    assert calls, "Module-global fake_global.list_namespaced_event must have been called"


# ─── (b) _get_namespace_events_as_dicts: module-global k8s_core_api via _DefaultClientView ───


@pytest.mark.asyncio
async def test_get_namespace_events_as_dicts_no_clients_uses_module_global(server, monkeypatch):
    """_get_namespace_events_as_dicts() with no clients= uses module-global k8s_core_api.

    The real _DefaultClientView (wired into helpers.event_analysis at server import)
    late-binds server.k8s_core_api, so patching the module global is enough.
    This seam test proves that monkeypatch.setattr(server, 'k8s_core_api', ...)
    still reaches the moved _get_namespace_events_as_dicts fallback.
    """
    calls = []
    fake_global = FakeApi(
        list_namespaced_event=items_list([])
    )

    original = fake_global._methods["list_namespaced_event"]

    def recording_list(**kwargs):
        calls.append("list_namespaced_event")
        return original

    fake_global._methods["list_namespaced_event"] = recording_list
    monkeypatch.setattr(server, "k8s_core_api", fake_global)

    result = await server._get_namespace_events_as_dicts(namespace="ns")

    assert isinstance(result, list), f"Expected list result; got {type(result).__name__}"
    assert calls, (
        "Module-global fake_global.list_namespaced_event must have been called "
        "via _DefaultClientView fallback wired into helpers.event_analysis"
    )


# ─── (d) detect_tekton_namespaces: source= forwarded to list_namespaces ───────

@pytest.mark.asyncio
async def test_detect_tekton_namespaces_forwards_source_to_list_namespaces(
    server, monkeypatch
):
    """detect_tekton_namespaces(source='ctx-b') → list_namespaces(source='ctx-b')."""
    captured = {}

    async def spy_list_namespaces(**kwargs):
        captured["source"] = kwargs.get("source", "NOT_PRESENT")
        return []

    monkeypatch.setattr(server, "list_namespaces", spy_list_namespaces)

    await server.detect_tekton_namespaces(source="ctx-b")

    assert "source" in captured, "spy_list_namespaces was never called"
    assert captured["source"] == "ctx-b", (
        f"Expected source='ctx-b' forwarded to list_namespaces; got {captured['source']!r}"
    )


@pytest.mark.asyncio
async def test_detect_tekton_namespaces_no_arg_passes_empty_source(
    server, monkeypatch
):
    """detect_tekton_namespaces() with no arg → list_namespaces(source='') (default)."""
    captured = {}

    async def spy_list_namespaces(**kwargs):
        captured["source"] = kwargs.get("source", "NOT_PRESENT")
        return []

    monkeypatch.setattr(server, "list_namespaces", spy_list_namespaces)

    await server.detect_tekton_namespaces()

    assert "source" in captured, "spy_list_namespaces was never called"
    assert captured["source"] == "", (
        f"Default source must be '' (empty string); got {captured['source']!r}"
    )


# ─── (e) _progressive_event_analysis_core: smart_get_namespace_events intercepted ──


@pytest.mark.asyncio
async def test_progressive_event_analysis_core_smart_events_fn_intercepted(server, monkeypatch):
    """progressive_event_analysis → _progressive_event_analysis_core → spy intercepted.

    Seam test: monkeypatch.setattr(server, 'smart_get_namespace_events', spy) is
    intercepted because the call site inside progressive_event_analysis passes
    smart_events_fn=smart_get_namespace_events by bare name; at call time the name
    resolves to server.smart_get_namespace_events (the spy).
    """
    captured = {}

    async def spy_smart_events(**kwargs):
        captured["namespace"] = kwargs.get("namespace")
        return {"events": []}

    monkeypatch.setattr(server, "smart_get_namespace_events", spy_smart_events)

    await server.progressive_event_analysis(namespace="test-ns-seam")

    assert "namespace" in captured, "spy_smart_events was never called"
    assert captured["namespace"] == "test-ns-seam", (
        f"Expected namespace forwarded to smart_events_fn; got {captured['namespace']!r}"
    )


@pytest.mark.asyncio
async def test_advanced_event_analytics_core_smart_events_fn_intercepted(server, monkeypatch):
    """advanced_event_analytics → _progressive_event_analysis_core → spy intercepted.

    Seam test: monkeypatch.setattr(server, 'smart_get_namespace_events', spy) is
    intercepted because the call site inside advanced_event_analytics passes
    smart_events_fn=smart_get_namespace_events by bare name; at call time the name
    resolves to server.smart_get_namespace_events (the spy).
    """
    captured = {}

    async def spy_smart_events(**kwargs):
        captured["namespace"] = kwargs.get("namespace")
        return {"events": []}

    monkeypatch.setattr(server, "smart_get_namespace_events", spy_smart_events)

    await server.advanced_event_analytics(namespace="test-ns-adv")

    assert "namespace" in captured, "spy_smart_events was never called via advanced_event_analytics"
    assert captured["namespace"] == "test-ns-adv", (
        f"Expected namespace forwarded to smart_events_fn; got {captured['namespace']!r}"
    )
