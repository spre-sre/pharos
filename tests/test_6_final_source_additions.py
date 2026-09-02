"""Task 6 seam tests — what_if_scenario_simulator + query_kubearchive source dispatch.

Invariants tested:
  1. what_if_scenario_simulator: named instance's custom_api + core_api reach the
     _execute_prometheus_query_internal partial (not the module globals).
  2. what_if_scenario_simulator: stored bearer token reaches the partial for a named
     instance; not bound for the default instance.
  3. what_if_scenario_simulator: source= reaches the partial (cache isolation).
  4. what_if_scenario_simulator: default path (source='') binds no bearer_token
     in the partial (sentinel is used → full default chain).
  5. query_kubearchive: source reaches get_discovery factory.
  6. query_kubearchive: default path (source='') preserved (source='' forwarded).

RED discipline: these tests are written before the implementation.  Before
conversion both tools have no source= param; calling them with source='fake-k8s-*'
raises TypeError (unexpected keyword argument).  After conversion the seam is
invoked and the assertions pass.

Mutation check (documented in task-6-report.md):
  Mutant A: bind partial with source="" always (omit the `source` kwarg binding).
    → test_what_if_source_reaches_prometheus_partial FAILS.
  Mutant B: omit bearer_token from the partial for named instances.
    → test_what_if_bearer_token_reaches_prometheus_partial FAILS.
  Mutant C: pass source="" to get_discovery always (not the tool's source param).
    → test_query_kubearchive_source_reaches_factory FAILS.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

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


# ── Server fixture (module-scoped) ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_task6") / "config"
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
        "server_mcp_task6", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_task6"] = mod
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


# ── Helper: register a fake named kubernetes instance ─────────────────────────

def _register_fake_instance(server, name: str, token: Optional[str] = None):
    """Register a named kubernetes instance and inject a sentinel K8sClientSet.

    Returns the injected K8sClientSet so callers can identity-check its fields.
    Optionally stores token in _instance_tokens for bearer-token seam tests.
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
    sentinel_apps = object()
    fake_cs = server.K8sClientSet(
        core_api=sentinel_core,
        apps_api=sentinel_apps,
        custom_api=sentinel_custom,
        batch_api=object(),
        storage_api=object(),
        networking_api=object(),
        autoscaling_api=object(),
        apis_api=object(),
    )
    server._k8s_instances[name] = fake_cs
    if token is not None:
        server._instance_tokens[name] = token
    return fake_cs


# ── Helpers: stub out what_if internals so only the seam matters ───────────────

def _stub_what_if_internals(monkeypatch, server, captured: dict):
    """Monkeypatch all what_if_scenario_simulator helpers to avoid real I/O.

    captured['prometheus_query_fn'] is set from load_historical_performance_data.
    captured['baseline_list_ns_fn'] is the list_ns_fn arg to collect_baseline_system_data.
    captured['affected_list_ns_fn'] is the list_ns_fn arg to identify_affected_components.
    """
    async def fake_collect_baseline(scope, core_api, list_ns_fn, list_pods_fn, progress_cb=None):
        captured["baseline_core_api"] = core_api
        captured["baseline_list_ns_fn"] = list_ns_fn
        return {"nodes": [], "namespaces": [], "pods": []}

    async def fake_build_models(baseline, scenario_type):
        return {"type": scenario_type}

    async def fake_load_historical(scope, duration, prometheus_query_fn=None):
        captured["prometheus_query_fn"] = prometheus_query_fn
        return {}

    def fake_calibrate(models, hist, load_profile):
        return models

    async def fake_monte_carlo(models, changes, stype, duration, risk_tol):
        return {"mean": {}, "confidence": {}, "p95": {}, "worst": {}, "scenarios": [], "convergence": {}}

    def fake_impact(sim_results, baseline, stype):
        return {}

    async def fake_affected(changes, scope, stype, core_api, apps_api, list_pods_fn, list_ns_fn):
        captured["affected_core_api"] = core_api
        captured["affected_apps_api"] = apps_api
        captured["affected_list_ns_fn"] = list_ns_fn
        return []

    def fake_risk(sim_results, impact, affected, risk_tol):
        return {}

    def fake_quality(baseline, hist, models, logger):
        return {}

    def fake_recommendations(impact, risk, quality, stype, logger):
        return []

    monkeypatch.setattr(server, "collect_baseline_system_data", fake_collect_baseline)
    monkeypatch.setattr(server, "build_system_behavior_models", fake_build_models)
    monkeypatch.setattr(server, "load_historical_performance_data", fake_load_historical)
    monkeypatch.setattr(server, "calibrate_simulation_models", fake_calibrate)
    monkeypatch.setattr(server, "run_monte_carlo_simulation", fake_monte_carlo)
    monkeypatch.setattr(server, "analyze_system_impact", fake_impact)
    monkeypatch.setattr(server, "identify_affected_components", fake_affected)
    monkeypatch.setattr(server, "perform_risk_assessment", fake_risk)
    monkeypatch.setattr(server, "calculate_simulation_quality", fake_quality)
    monkeypatch.setattr(server, "generate_simulation_recommendations", fake_recommendations)


# ── Tests: what_if_scenario_simulator ────────────────────────────────────────

class TestWhatIfSourceDispatch:
    """Seam tests for what_if_scenario_simulator source= parameter threading."""

    def test_what_if_clients_reach_prometheus_partial(self, server, monkeypatch):
        """Named instance's custom_api + core_api must reach the prometheus partial.

        RED: before conversion what_if_scenario_simulator has no source= param;
        calling with source= raises TypeError.

        GREEN: after conversion _clients.custom_api / core_api from the fake
        instance (sentinels) appear in the partial's keywords.
        """
        fake_cs = _register_fake_instance(server, "fake-k8s-what1")
        captured: dict = {}
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="resource_limits",
                changes={"cpu_limit": "500m"},
                source="fake-k8s-what1",
            )
        )

        fn = captured.get("prometheus_query_fn")
        assert fn is not None, "load_historical_performance_data was not called"
        assert hasattr(fn, "keywords"), "prometheus_query_fn is not a functools.partial"

        assert fn.keywords.get("custom_api") is fake_cs.custom_api, (
            f"custom_api in partial must be fake instance's sentinel "
            f"(id={id(fake_cs.custom_api):#x}), got: {fn.keywords.get('custom_api')!r}. "
            "Tool must bind _clients.custom_api, not the module global k8s_custom_api."
        )
        assert fn.keywords.get("core_api") is fake_cs.core_api, (
            f"core_api in partial must be fake instance's sentinel "
            f"(id={id(fake_cs.core_api):#x}), got: {fn.keywords.get('core_api')!r}. "
            "Tool must bind _clients.core_api, not the module global k8s_core_api."
        )

    def test_what_if_bearer_token_reaches_prometheus_partial(self, server, monkeypatch):
        """Named instance's stored bearer token must be bound in the prometheus partial.

        RED: before conversion source= param is absent; TypeError on call.

        GREEN: after conversion, bearer_token=stored-token is a keyword in the partial.
        This ensures the named instance's token is used, never the default chain.
        """
        _register_fake_instance(server, "fake-k8s-what2", token="tok-sentinel-abc")
        captured: dict = {}
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="scaling",
                changes={"replicas": 3},
                source="fake-k8s-what2",
            )
        )

        fn = captured.get("prometheus_query_fn")
        assert fn is not None, "load_historical_performance_data was not called"
        assert hasattr(fn, "keywords"), "prometheus_query_fn is not a functools.partial"

        assert "bearer_token" in fn.keywords, (
            "bearer_token must be bound in the partial for a named instance. "
            "The named instance's stored token must be passed, not the default chain."
        )
        assert fn.keywords["bearer_token"] == "tok-sentinel-abc", (
            f"Expected bearer_token='tok-sentinel-abc', "
            f"got: {fn.keywords['bearer_token']!r}. "
            "Tool must read _instance_tokens.get(source) for named instances."
        )

    def test_what_if_source_reaches_prometheus_partial(self, server, monkeypatch):
        """source= must be bound in the prometheus partial for cache isolation.

        RED: before conversion source= param is absent; TypeError on call.

        GREEN: after conversion, source='fake-k8s-what3' appears in fn.keywords.
        Without this, the endpoint cache would be keyed under 'default' and
        a named instance's prometheus endpoint would be shared with the default.
        """
        _register_fake_instance(server, "fake-k8s-what3")
        captured: dict = {}
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="configuration",
                changes={"timeout": "30s"},
                source="fake-k8s-what3",
            )
        )

        fn = captured.get("prometheus_query_fn")
        assert fn is not None, "load_historical_performance_data was not called"
        assert hasattr(fn, "keywords"), "prometheus_query_fn is not a functools.partial"

        assert fn.keywords.get("source") == "fake-k8s-what3", (
            f"Expected source='fake-k8s-what3' in partial keywords, "
            f"got: {fn.keywords.get('source')!r}. "
            "Omitting source= silently keys the endpoint cache under 'default', "
            "creating cross-cluster bleed."
        )

    def test_what_if_default_path_no_bearer_token_bound(self, server, monkeypatch):
        """Default path (source='') must NOT bind bearer_token in the partial.

        The sentinel is used via the parameter default, which triggers the full
        default token fallback chain (_get_k8s_bearer_token: oc → kubeconfig → SA → env).
        Binding bearer_token=None would produce token_unavailable errors on the
        default instance.
        """
        captured: dict = {}
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="deployment",
                changes={"image": "new-image"},
                # source="" (default — omitted)
            )
        )

        fn = captured.get("prometheus_query_fn")
        assert fn is not None, "load_historical_performance_data was not called"
        assert hasattr(fn, "keywords"), "prometheus_query_fn is not a functools.partial"

        # For the default path, bearer_token must NOT be explicitly bound.
        # If bound, it must be the sentinel (not None), otherwise the default chain breaks.
        from helpers.prometheus import _BEARER_SENTINEL
        if "bearer_token" in fn.keywords:
            assert fn.keywords["bearer_token"] is _BEARER_SENTINEL, (
                f"Default path partial has bearer_token={fn.keywords['bearer_token']!r}. "
                "Binding bearer_token=None would produce token_unavailable on the default instance. "
                "Either omit bearer_token (sentinel default) or bind _BEARER_SENTINEL explicitly."
            )

    def test_what_if_clients_also_reach_identify_affected_components(self, server, monkeypatch):
        """Named instance's core_api + apps_api must reach identify_affected_components.

        The plan requires ALL global API usages to be replaced with _clients.* fields.
        """
        fake_cs = _register_fake_instance(server, "fake-k8s-what4")
        captured: dict = {}
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="resource_limits",
                changes={"mem": "1Gi"},
                source="fake-k8s-what4",
            )
        )

        assert captured.get("affected_core_api") is fake_cs.core_api, (
            f"core_api passed to identify_affected_components must be the fake instance's "
            f"sentinel (id={id(fake_cs.core_api):#x}), "
            f"got: {captured.get('affected_core_api')!r}."
        )
        assert captured.get("affected_apps_api") is fake_cs.apps_api, (
            f"apps_api passed to identify_affected_components must be the fake instance's "
            f"sentinel (id={id(fake_cs.apps_api):#x}), "
            f"got: {captured.get('affected_apps_api')!r}."
        )

    def test_what_if_list_namespaces_partial_in_collect_baseline(self, server, monkeypatch):
        """list_namespaces passed to collect_baseline_system_data must be source-bound.

        RED: before fix, the bare list_namespaces is passed — it is NOT a partial,
        so it calls the default cluster's namespace list regardless of source=.

        GREEN: after fix, functools.partial(list_namespaces, source=X) is passed,
        and invoking it routes to the named cluster.

        Mutation check: reverting the partial binding so the bare function is passed
        causes this test to fail because hasattr(fn, 'keywords') is False.
        """
        _register_fake_instance(server, "fake-k8s-lns-b")
        captured: dict = {}

        spy_calls: list = []

        async def _spy_ns(**kwargs):
            spy_calls.append(kwargs)
            return []

        monkeypatch.setattr(server, "list_namespaces", _spy_ns)
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="resource_limits",
                changes={"cpu": "500m"},
                source="fake-k8s-lns-b",
            )
        )

        fn = captured.get("baseline_list_ns_fn")
        assert fn is not None, "collect_baseline_system_data stub did not capture list_ns_fn"
        assert hasattr(fn, "keywords"), (
            "list_ns_fn passed to collect_baseline_system_data must be a functools.partial; "
            "the bare list_namespaces was passed instead, causing default-cluster bleed."
        )
        assert fn.keywords.get("source") == "fake-k8s-lns-b", (
            f"partial must bind source='fake-k8s-lns-b', "
            f"got: {fn.keywords.get('source')!r}"
        )
        assert fn.func is _spy_ns, (
            "partial must wrap server.list_namespaces, not some other callable"
        )
        # Invoke the partial — spy must receive source=fake-k8s-lns-b.
        asyncio.run(fn())
        assert spy_calls, "invoking the partial did not call the underlying list_namespaces spy"
        assert spy_calls[-1].get("source") == "fake-k8s-lns-b", (
            f"Calling the partial must route source='fake-k8s-lns-b' to the underlying "
            f"callable; got kwargs: {spy_calls[-1]!r}"
        )

    def test_what_if_list_namespaces_partial_in_identify_affected(self, server, monkeypatch):
        """list_namespaces passed to identify_affected_components must be source-bound.

        RED: before fix, the bare list_namespaces is passed — calls the default cluster.
        GREEN: after fix, functools.partial(list_namespaces, source=X) is passed.

        Mutation check: reverting the partial binding so the bare function is passed
        causes this test to fail because hasattr(fn, 'keywords') is False.
        """
        _register_fake_instance(server, "fake-k8s-lns-a")
        captured: dict = {}

        spy_calls: list = []

        async def _spy_ns(**kwargs):
            spy_calls.append(kwargs)
            return []

        monkeypatch.setattr(server, "list_namespaces", _spy_ns)
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="scaling",
                changes={"replicas": 2},
                source="fake-k8s-lns-a",
            )
        )

        fn = captured.get("affected_list_ns_fn")
        assert fn is not None, "identify_affected_components stub did not capture list_ns_fn"
        assert hasattr(fn, "keywords"), (
            "list_ns_fn passed to identify_affected_components must be a functools.partial; "
            "the bare list_namespaces was passed instead, causing default-cluster bleed."
        )
        assert fn.keywords.get("source") == "fake-k8s-lns-a", (
            f"partial must bind source='fake-k8s-lns-a', "
            f"got: {fn.keywords.get('source')!r}"
        )
        assert fn.func is _spy_ns, (
            "partial must wrap server.list_namespaces, not some other callable"
        )
        # Invoke the partial — spy must receive source=fake-k8s-lns-a.
        asyncio.run(fn())
        assert spy_calls, "invoking the partial did not call the underlying list_namespaces spy"
        assert spy_calls[-1].get("source") == "fake-k8s-lns-a", (
            f"Calling the partial must route source='fake-k8s-lns-a' to the underlying "
            f"callable; got kwargs: {spy_calls[-1]!r}"
        )

    def test_what_if_list_namespaces_default_path_unbound(self, server, monkeypatch):
        """Default path (source='') must pass the bare list_namespaces, not a source-bound partial.

        The sibling-tool pattern (semantic_log_search:9491) passes the bare callable
        for source=='' to preserve the default cluster chain without wrapping.
        """
        captured: dict = {}

        async def _spy_ns(**kwargs):
            return []

        monkeypatch.setattr(server, "list_namespaces", _spy_ns)
        _stub_what_if_internals(monkeypatch, server, captured)

        asyncio.run(
            server.what_if_scenario_simulator(
                scenario_type="deployment",
                changes={"image": "latest"},
                # source="" default — omitted
            )
        )

        for label, key in [
            ("collect_baseline_system_data", "baseline_list_ns_fn"),
            ("identify_affected_components", "affected_list_ns_fn"),
        ]:
            fn = captured.get(key)
            assert fn is not None, f"{label} stub did not capture list_ns_fn"
            # Default path: bare callable, no source kwarg bound.
            if hasattr(fn, "keywords"):
                assert "source" not in fn.keywords or not fn.keywords.get("source"), (
                    f"Default path {label}: list_ns_fn must not bind a non-empty source; "
                    f"got keywords={fn.keywords!r}"
                )


# ── Tests: query_kubearchive ──────────────────────────────────────────────────

class TestQueryKubearchiveSourceDispatch:
    """Seam tests for query_kubearchive source= parameter threading."""

    def test_query_kubearchive_source_reaches_factory(self, server, monkeypatch):
        """Named instance's source must be forwarded to get_discovery factory.

        RED: before conversion query_kubearchive has no source= param; calling
        with source= raises TypeError.

        GREEN: after conversion get_discovery receives source='fake-k8s-ka1'
        so the factory returns the per-instance discovery object.

        A sentinel bearer token is stored so the BUG 1 fail-closed guard is
        satisfied and the function reaches get_discovery.
        """
        _register_fake_instance(server, "fake-k8s-ka1", token="tok-ka1-seam")
        captured: dict = {}

        def fake_get_discovery(source="", *, k8s_core_api=None, k8s_custom_api=None,
                               k8s_networking_api=None):
            captured["source"] = source
            return None  # None triggers the KUBEARCHIVE_ENABLED=false path

        import helpers.kubearchive_integration as ka_mod
        monkeypatch.setattr(server, "get_discovery", fake_get_discovery)

        asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                source="fake-k8s-ka1",
            )
        )

        assert captured.get("source") == "fake-k8s-ka1", (
            f"Expected source='fake-k8s-ka1' forwarded to get_discovery, "
            f"got: {captured.get('source')!r}. "
            "Tool must pass source= to the factory for per-instance discovery."
        )

    def test_query_kubearchive_clients_reach_factory(self, server, monkeypatch):
        """Named instance's core_api + custom_api must reach get_discovery.

        After source dispatch is added, _resolve_k8s provides instance clients
        which must be forwarded to get_discovery (not the module globals).

        A sentinel bearer token is stored so the BUG 1 fail-closed guard is
        satisfied and the function reaches get_discovery.
        """
        fake_cs = _register_fake_instance(server, "fake-k8s-ka2", token="tok-ka2-seam")
        captured: dict = {}

        def fake_get_discovery(source="", *, k8s_core_api=None, k8s_custom_api=None,
                               k8s_networking_api=None):
            captured["source"] = source
            captured["core_api"] = k8s_core_api
            captured["custom_api"] = k8s_custom_api
            return None

        monkeypatch.setattr(server, "get_discovery", fake_get_discovery)

        asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                source="fake-k8s-ka2",
            )
        )

        assert captured.get("core_api") is fake_cs.core_api, (
            f"core_api passed to get_discovery must be the fake instance's sentinel "
            f"(id={id(fake_cs.core_api):#x}), got: {captured.get('core_api')!r}. "
            "Tool must pass _clients.core_api, not the module global."
        )
        assert captured.get("custom_api") is fake_cs.custom_api, (
            f"custom_api passed to get_discovery must be the fake instance's sentinel "
            f"(id={id(fake_cs.custom_api):#x}), got: {captured.get('custom_api')!r}. "
            "Tool must pass _clients.custom_api, not the module global."
        )

    def test_query_kubearchive_default_path_preserved(self, server, monkeypatch):
        """Default path (source='') must forward source='' to get_discovery.

        The factory distinguishes '' from named sources to confine port-forward.
        A default call must pass source='' (not some other value).
        """
        captured: dict = {}

        def fake_get_discovery(source="", *, k8s_core_api=None, k8s_custom_api=None,
                               k8s_networking_api=None):
            captured["source"] = source
            return None

        monkeypatch.setattr(server, "get_discovery", fake_get_discovery)

        asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                # source="" (default — omitted)
            )
        )

        assert captured.get("source") == "", (
            f"Default path must forward source='' to get_discovery, "
            f"got: {captured.get('source')!r}. "
            "The factory uses source=='' to decide whether to allow port-forward."
        )


# ── Tests: BUG 1 fix — named-source fail-closed token check ──────────────────

class TestQueryKubearchiveFailClosed:
    """BUG 1 fix (review B2): named source with no stored bearer token must fail
    closed before client construction and before any HTTP call to KubeArchive.

    A named source whose _instance_tokens entry is falsy (None = cert-auth context)
    must return kubearchive_status='error' with error='token_unavailable' without
    ever calling setup_kubearchive_client.

    Mutation check: reverting the early fail-closed guard in query_kubearchive
    (removing the `if source and not _instance_tokens.get(source): return …`
    block) causes test_named_source_no_token_fail_closed to fail because
    spy_setup records a call instead of staying empty.
    """

    def test_named_source_no_token_fail_closed(self, server, monkeypatch):
        """Named source with no stored bearer token must return token_unavailable
        without calling setup_kubearchive_client or performing any HTTP to KubeArchive.

        get_discovery and check_kubearchive_availability are stubbed so the function
        reaches the token-resolution site (past the KUBEARCHIVE_ENABLED=false guard).
        The seam under test is the early fail-closed guard, not those stubs.
        """
        _register_fake_instance(server, "fake-k8s-fc1")
        # No token stored in _instance_tokens for this source (cert-auth context)

        setup_calls: list = []

        async def spy_setup(*args, **kwargs):
            setup_calls.append(kwargs.get("k8s_auth_token", "<positional>"))
            return None

        monkeypatch.setattr(server, "setup_kubearchive_client", spy_setup)

        # Stub get_discovery → non-None so function reaches the token-check site
        fake_discovery = MagicMock()
        monkeypatch.setattr(server, "get_discovery", lambda *a, **kw: fake_discovery)

        # Stub availability check to report available (prevents HTTP to real KubeArchive)
        async def fake_availability(discovery):
            return {"available": True, "endpoint": "https://fake-ka.svc"}

        monkeypatch.setattr(server, "check_kubearchive_availability", fake_availability)

        result = asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                source="fake-k8s-fc1",
            )
        )

        assert result.get("kubearchive_status") == "error", (
            f"Expected kubearchive_status='error', got {result.get('kubearchive_status')!r}"
        )
        assert result.get("error") == "token_unavailable", (
            f"Expected error='token_unavailable', got {result.get('error')!r}. "
            "Named source with no stored bearer token must fail closed with "
            "token_unavailable before constructing a KubeArchive client."
        )
        assert setup_calls == [], (
            "setup_kubearchive_client must NOT be called when the named source has no "
            f"bearer token. Was called with: {setup_calls!r}"
        )

    def test_named_source_kubearchive_token_env_does_not_bleed(self, server, monkeypatch):
        """KUBEARCHIVE_TOKEN env must not substitute for a missing named-source bearer token.

        Even when KUBEARCHIVE_TOKEN is set in the environment (an ambient credential
        belonging to the server's own cluster), a named source with no stored bearer token
        must still fail closed with token_unavailable.

        Mutation check: the old behaviour (_instance_tokens.get(source) → None →
        KubeArchiveClient._get_auth_token falls through to env) would pass the env token
        to setup_kubearchive_client; spy_setup would record a call, failing this test.
        """
        _register_fake_instance(server, "fake-k8s-fc2")
        # No token in _instance_tokens
        monkeypatch.setenv("KUBEARCHIVE_TOKEN", "env-bleed-sentinel-MUST-NOT-APPEAR")

        setup_calls: list = []

        async def spy_setup(*args, **kwargs):
            setup_calls.append(kwargs.get("k8s_auth_token", "<positional>"))
            return None

        monkeypatch.setattr(server, "setup_kubearchive_client", spy_setup)
        monkeypatch.setattr(server, "get_discovery", lambda *a, **kw: MagicMock())

        async def fake_availability(discovery):
            return {"available": True, "endpoint": "https://fake-ka.svc"}

        monkeypatch.setattr(server, "check_kubearchive_availability", fake_availability)

        result = asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                source="fake-k8s-fc2",
            )
        )

        assert result.get("error") == "token_unavailable", (
            "Named source must fail closed with token_unavailable even when "
            f"KUBEARCHIVE_TOKEN env is set. Got: {result.get('error')!r}"
        )
        assert setup_calls == [], (
            "setup_kubearchive_client must NOT be called; KUBEARCHIVE_TOKEN env "
            "must not bleed into outbound requests for a named source. "
            f"Called with: {setup_calls!r}"
        )

    def test_default_source_not_subject_to_named_source_guard(self, server, monkeypatch):
        """Default source (source='') must NOT return token_unavailable — it uses the
        full _get_k8s_bearer_token fallback chain (behavior preserved).

        When KUBEARCHIVE_ENABLED=false (as in this test fixture) the function returns
        with a discovery error, never token_unavailable.  This ensures the new
        fail-closed guard does not affect the default source path.
        """
        # Stub get_discovery to return None → triggers the "endpoint not discovered"
        # early-out path for source=''.  This must NOT produce token_unavailable.
        monkeypatch.setattr(server, "get_discovery", lambda *a, **kw: None)

        result = asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                # source="" (default — omitted)
            )
        )

        assert result.get("kubearchive_status") == "error"
        assert result.get("error") != "token_unavailable", (
            "Default source (source='') must not be gated by the named-source "
            f"fail-closed check. Got error: {result.get('error')!r}"
        )

    def test_default_instance_alias_not_subject_to_named_source_guard(
        self, server, monkeypatch
    ):
        """source=<default-instance-name> must normalise to source='' before the guard.

        Without normalisation, source='kubernetes' (the default instance's canonical
        name) is truthy and _instance_tokens has no entry for it (the default
        cluster token is fetched on-demand, not stored) → guard fires →
        token_unavailable.

        With normalisation: source is set to '' before the guard; the guard's
        `if source and …` evaluates to False → skipped; the function proceeds to
        normal default-source discovery.

        Regression guard: remove the normalisation line in query_kubearchive and
        this test fails because token_unavailable is returned instead of a
        discovery error.
        """
        default_name = server._source_registry.default_kubernetes_instance()
        # Ensure no token is stored under the default name (the normal state —
        # the ambient token chain is used for the default cluster, not _instance_tokens)
        server._instance_tokens.pop(default_name, None)

        # Stub get_discovery → None so the function exits with a discovery error
        # rather than attempting real network I/O; the seam under test is the guard,
        # not discovery.
        monkeypatch.setattr(server, "get_discovery", lambda *a, **kw: None)

        result = asyncio.run(
            server.query_kubearchive(
                resource_type="pod",
                namespace="default",
                source=default_name,
            )
        )

        assert result.get("error") != "token_unavailable", (
            f"source='{default_name}' (the default instance alias) must not hit the "
            f"fail-closed guard. Got error={result.get('error')!r}. "
            "Add `if source == _source_registry.default_kubernetes_instance(): source = ''` "
            "before the guard in query_kubearchive."
        )
        assert result.get("kubearchive_status") == "error", (
            "Expected a discovery error (not token_unavailable), "
            f"got status={result.get('kubearchive_status')!r}"
        )
