"""Forecaster second-character value-corruption tests (F-17, M-C2-pinned).

Three tests pin the exact values produced by _analyze_cluster_capacity_new
when prometheus_query returns the PROCESSED shape (what the function actually
receives after _format_as_json runs):

    {"status":"success","data":[{"metric":{},"value":"47.3","timestamp":"...","formatted_value":"47.3"}]}

Pre-fix:
    data[0]['value'] = "47.3"  (already a string)
    data[0]['value'][1] = "7"  (second character — the bug)
    float("7") = 7.0  → result is "7.0%"

    data[0]['value'] = "0.5"
    data[0]['value'][1] = "."
    float(".") raises ValueError, swallowed by except → 0.0 → result is "0.0%"

Post-fix (data[0]['value'] → float directly):
    float("47.3") = 47.3 → "47.3%"  ✓
    float("0.5")  = 0.5  → "0.5%"   ✓
    float("62.1") = 62.1 → "62.1%"  ✓
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


# ── Server fixture (module-scoped, mirrors test_output_bounding.py:86-140) ────

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_forecaster") / "config"
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
        "server_mcp_forecaster", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_forecaster"] = mod
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_node_list():
    """One-node list with capacity as a plain dict (not a _NS object).

    status.capacity must be a plain dict so .get('cpu', '0') succeeds.
    Using SimpleNamespace would give a _NS-like object whose .get() raises
    AttributeError — the capacity field MUST be a real dict.
    """
    node = SimpleNamespace(
        status=SimpleNamespace(
            capacity={"cpu": "4", "memory": "16Gi"}
        )
    )
    return SimpleNamespace(items=[node])


def _prom_success(value: str) -> dict:
    """Return the PROCESSED prometheus_query shape for a scalar value.

    This is what _analyze_cluster_capacity_new actually receives — the output
    of _format_as_json, NOT the raw Prometheus wire shape.
    data[0]['value'] is already a scalar string, not [timestamp, string].
    """
    return {
        "status": "success",
        "data": [
            {
                "metric": {},
                "value": value,
                "timestamp": "1753180800",
                "formatted_value": value,
            }
        ],
    }


def _make_fake_core_api():
    """Minimal fake CoreV1Api with list_node."""
    class _FakeCore:
        def list_node(self, **kwargs):
            return _make_node_list()
    return _FakeCore()


# ── Step 1: CPU value pin — "47.3"[1] = "7" → 7.0 (BUG); fix → 47.3 ─────────

class TestForecasterValuePin:
    """Pin the exact CPU/memory values returned by _analyze_cluster_capacity_new.

    Pre-fix: FAILS with AssertionError: assert '7.0%' == '47.3%'
    Post-fix: PASSES — float("47.3") = 47.3 → "47.3%"
    """

    @pytest.mark.asyncio
    async def test_cpu_value_not_second_character(self, server, monkeypatch):
        """current_cpu_usage must be '47.3%', not '7.0%' (second-character bug).

        Pre-fix: data[0]['value'][1] = '7' → float('7') = 7.0 → '7.0%'. FAILS.
        Post-fix: float('47.3') = 47.3 → '47.3%'. PASSES.
        """
        cpu_result = _prom_success("47.3")
        memory_result = _prom_success("62.1")

        call_count = {"n": 0}

        async def _fake_prom(query, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: connectivity check (up)
                return {"status": "success", "data": []}
            if "node_cpu" in query or "idle" in query:
                return cpu_result
            if "node_memory" in query or "MemAvailable" in query or "MemTotal" in query:
                return memory_result
            return {"status": "success", "data": []}

        async def _fake_node_resources(trend_period, forecast_horizon, log, *, query_fn=None, core_api=None):
            return []

        monkeypatch.setattr(server, "prometheus_query", _fake_prom)
        monkeypatch.setattr(server, "_analyze_node_resources_new", _fake_node_resources)
        monkeypatch.setattr(server, "k8s_core_api", _make_fake_core_api())

        result = await server.resource_bottleneck_forecaster()
        overview = result["cluster_overview"]
        assert overview["current_cpu_usage"] == "47.3%", (
            f"Expected '47.3%' (full value); got {overview['current_cpu_usage']!r}. "
            f"Pre-fix: data[0]['value'][1]='7' → 7.0 → '7.0%' (second-character bug)."
        )

    @pytest.mark.asyncio
    async def test_zero_point_five_not_swallowed(self, server, monkeypatch):
        """current_cpu_usage must be '0.5%', not '0.0%' (ValueError-swallowed bug).

        Pre-fix: data[0]['value'][1] = '.' → float('.') raises ValueError,
        swallowed by except → cpu_usage_percent stays 0.0 → '0.0%'. FAILS.
        Post-fix: float('0.5') = 0.5 → '0.5%'. PASSES.
        """
        cpu_result = _prom_success("0.5")
        memory_result = _prom_success("62.1")

        call_count = {"n": 0}

        async def _fake_prom(query, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"status": "success", "data": []}
            if "node_cpu" in query or "idle" in query:
                return cpu_result
            if "node_memory" in query or "MemAvailable" in query or "MemTotal" in query:
                return memory_result
            return {"status": "success", "data": []}

        async def _fake_node_resources(trend_period, forecast_horizon, log, *, query_fn=None, core_api=None):
            return []

        monkeypatch.setattr(server, "prometheus_query", _fake_prom)
        monkeypatch.setattr(server, "_analyze_node_resources_new", _fake_node_resources)
        monkeypatch.setattr(server, "k8s_core_api", _make_fake_core_api())

        result = await server.resource_bottleneck_forecaster()
        overview = result["cluster_overview"]
        assert overview["current_cpu_usage"] == "0.5%", (
            f"Expected '0.5%'; got {overview['current_cpu_usage']!r}. "
            f"Pre-fix: data[0]['value'][1]='.' → ValueError → 0.0 → '0.0%'."
        )

    @pytest.mark.asyncio
    async def test_memory_value_not_second_character(self, server, monkeypatch):
        """current_memory_usage must be '62.1%', not '2.0%' (second-character bug).

        Pre-fix: data[0]['value'][1] = '2' → float('2') = 2.0 → '2.0%'. FAILS.
        Post-fix: float('62.1') = 62.1 → '62.1%'. PASSES.
        """
        cpu_result = _prom_success("47.3")
        memory_result = _prom_success("62.1")

        call_count = {"n": 0}

        async def _fake_prom(query, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"status": "success", "data": []}
            if "node_cpu" in query or "idle" in query:
                return cpu_result
            if "node_memory" in query or "MemAvailable" in query or "MemTotal" in query:
                return memory_result
            return {"status": "success", "data": []}

        async def _fake_node_resources(trend_period, forecast_horizon, log, *, query_fn=None, core_api=None):
            return []

        monkeypatch.setattr(server, "prometheus_query", _fake_prom)
        monkeypatch.setattr(server, "_analyze_node_resources_new", _fake_node_resources)
        monkeypatch.setattr(server, "k8s_core_api", _make_fake_core_api())

        result = await server.resource_bottleneck_forecaster()
        overview = result["cluster_overview"]
        assert overview["current_memory_usage"] == "62.1%", (
            f"Expected '62.1%' (full value); got {overview['current_memory_usage']!r}. "
            f"Pre-fix: data[0]['value'][1]='2' → 2.0 → '2.0%' (second-character bug)."
        )


# ── Step 4: namespace CPU value pin (site 3) ───────────────────────────────────

class TestForecasterNamespaceCPUPin:
    """Pin the namespace-CPU value produced by resource_bottleneck_forecaster
    when namespaces=[...] is passed (site 3 of the F-17 value-corruption fix).

    Pre-fix (data[0]['value'][1] instead of data[0]['value']):
        value = "47.3" → value[1] = "7" → float("7") = 7.0  WRONG
        → forecast current_usage.value = 7.0, not 47.3.

    Post-fix (data[0]['value']):
        float("47.3") = 47.3 → forecast current_usage.value = 47.3.  CORRECT

    Mutation: revert line 12366 to data[0]['value'][1] → value pin fails.
    """

    @pytest.mark.asyncio
    async def test_namespace_cpu_value_not_second_character(self, server, monkeypatch):
        """current_usage.value in namespace_cpu forecast must be 47.3, not 7.0.

        Pre-fix: data[0]['value'][1] = '7' → float('7') = 7.0 → FAILS.
        Post-fix: float('47.3') = 47.3 → PASSES.
        """
        ns_cpu_result = _prom_success("47.3")

        call_count = {"n": 0}

        async def _fake_prom(query, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: connectivity check (up)
                return {"status": "success", "data": []}
            # Cluster CPU/memory queries (for _analyze_cluster_capacity_new)
            if "idle" in query or "node_cpu" in query:
                return _prom_success("5.0")
            if "MemAvailable" in query or "MemTotal" in query or "node_memory" in query:
                return _prom_success("10.0")
            # Namespace CPU query
            if "container_cpu_usage_seconds_total" in query:
                return ns_cpu_result
            # Namespace memory queries — return no data so only CPU entry appears
            return {"status": "success", "data": []}

        async def _fake_node_resources(trend_period, forecast_horizon, log, *, query_fn=None, core_api=None):
            return []

        monkeypatch.setattr(server, "prometheus_query", _fake_prom)
        monkeypatch.setattr(server, "_analyze_node_resources_new", _fake_node_resources)
        monkeypatch.setattr(server, "k8s_core_api", _make_fake_core_api())

        result = await server.resource_bottleneck_forecaster(namespaces=["test-ns"])

        # Find the namespace_cpu forecast entry
        ns_forecasts = [
            f for f in result["forecasts"]
            if f.get("resource_type") == "namespace_cpu"
               and f.get("resource_identifier", {}).get("namespace") == "test-ns"
        ]
        assert ns_forecasts, (
            "No namespace_cpu forecast entry found for 'test-ns'. "
            "Pre-fix with value='0.x': float('.') raises ValueError (swallowed) → no entry."
        )
        actual_value = ns_forecasts[0]["current_usage"]["value"]
        assert actual_value == 47.3, (
            f"Expected namespace CPU current_usage.value=47.3; got {actual_value!r}. "
            f"Pre-fix: data[0]['value'][1]='7' → float('7')=7.0 (second-character bug, site 3)."
        )


# ── F-R2-2: core_api identity pin for _analyze_node_resources_new ──────────────

class TestNodeResourcesReceivesCoreApi:
    """F-R2-2: resource_bottleneck_forecaster passes server.k8s_core_api to
    _analyze_node_resources_new as core_api (not None, not a stale snapshot).

    A required-kwarg change to the function would NOT catch this regression
    (None is a legal value); only the identity assertion on the captured arg proves
    the call-site wiring is intact.

    Non-vacuity: mutate the call site to pass core_api=None → test FAILS.
    Restore → test PASSES.
    """

    @pytest.mark.asyncio
    async def test_analyze_node_resources_receives_core_api(self, server, monkeypatch):
        """_analyze_node_resources_new must receive server.k8s_core_api, not None."""
        # Sentinel: a unique object whose identity we can verify at assertion time.
        sentinel_core = object()
        monkeypatch.setattr(server, "k8s_core_api", sentinel_core)

        # Capturing stub: records the core_api kwarg, then returns an empty list.
        captured = {}

        async def _capturing_stub(trend_period, forecast_horizon, log, *, query_fn=None, core_api=None):
            captured["core_api"] = core_api
            return []

        monkeypatch.setattr(server, "_analyze_node_resources_new", _capturing_stub)

        # Stub _analyze_cluster_capacity_new to avoid unrelated K8s/Prometheus calls.
        async def _fake_capacity(core_api, log, *, query_fn=None):
            return {
                "overall_health": "unknown",
                "current_cpu_usage": "0.0%",
                "current_memory_usage": "0.0%",
                "most_constrained_resources": [],
                "fastest_growing_consumers": [],
                "capacity_runway": {},
            }

        monkeypatch.setattr(server, "_analyze_cluster_capacity_new", _fake_capacity)

        # Return success for the connectivity probe (first call) and nothing thereafter.
        async def _fake_prom(query, **kwargs):
            return {"status": "success", "data": []}

        monkeypatch.setattr(server, "prometheus_query", _fake_prom)

        await server.resource_bottleneck_forecaster()

        assert "core_api" in captured, (
            "_analyze_node_resources_new stub was never called; "
            "check that a cpu/memory/disk resource_type triggered the node-analysis path"
        )
        assert captured["core_api"] is server.k8s_core_api, (
            f"_analyze_node_resources_new received core_api={captured['core_api']!r} "
            f"but server.k8s_core_api is {server.k8s_core_api!r}. "
            "The call site must pass k8s_core_api (the live server global), not None. "
            "Non-vacuity: mutate the call site to pass core_api=None → this fails; restore → passes."
        )
