"""Output bounding for the two unbounded-output offenders found in the
2026-07-24 p02 41-tool live sweep:

  - live_system_topology_mapper returned 1.32MB at depth_limit=1 (12-pod ns)
  - list_pods_in_namespace fetched an ~18MB API response on a 279-pod ns
    (43s + urllib3 connection-break retries)

Phase-3.5 style: additive params with behavior-preserving defaults for small
results (goldens byte-identical), staged truncation + explicit _truncation
reporting when the bound engages.

Mutation targets:
  - truncation boundary: comfortably-under-budget input -> output UNCHANGED,
    no _truncation key (both tools)
  - list_pods: the API call must receive the limit (server-side bound — the
    18MB problem is at fetch time, not serialization time)
"""
import importlib.util
import json
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


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_bounding_sweep") / "config"
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

    # F9 harness-bleed guard: pin KUBE_CONFIG_DEFAULT_LOCATION to the fake
    # kubeconfig so _discover_kube_contexts reads only the harness contexts
    # (not ~/.kube/config on a dev machine with real contexts).
    # Mirrors tests/characterization/conftest.py:78-83.
    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_bounding_sweep", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_bounding_sweep"] = mod
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


# ── Fakes for list_pods_in_namespace ─────────────────────────────────────────

def _pod(name):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, creation_timestamp=None),
        spec=SimpleNamespace(node_name="node-1"),
        status=SimpleNamespace(
            phase="Running", pod_ip="10.0.0.1", container_statuses=None,
            init_container_statuses=None,
        ),
    )


class _FakePodApi:
    """Captures list_namespaced_pod kwargs; optionally reports a continue token."""

    def __init__(self, n_pods, continue_token=None):
        self.calls = []
        self._pods = [_pod(f"pod-{i}") for i in range(n_pods)]
        self._continue = continue_token

    def list_namespaced_pod(self, namespace, **kwargs):
        self.calls.append({"namespace": namespace, **kwargs})
        return SimpleNamespace(
            items=self._pods,
            metadata=SimpleNamespace(_continue=self._continue,
                                     remaining_item_count=None),
        )


class TestListPodsBounding:
    @pytest.mark.asyncio
    async def test_default_limit_forwarded_to_api(self, server, monkeypatch):
        fake = _FakePodApi(3)
        monkeypatch.setattr(server, "k8s_core_api", fake)
        await server.list_pods_in_namespace(namespace="ns1")
        assert fake.calls and fake.calls[0].get("limit") == 200

    @pytest.mark.asyncio
    async def test_explicit_limit_forwarded(self, server, monkeypatch):
        fake = _FakePodApi(3)
        monkeypatch.setattr(server, "k8s_core_api", fake)
        await server.list_pods_in_namespace(namespace="ns1", limit=7)
        assert fake.calls[0].get("limit") == 7

    @pytest.mark.asyncio
    async def test_truncated_response_appends_truncation_entry(self, server, monkeypatch):
        fake = _FakePodApi(3, continue_token="more-pages")
        monkeypatch.setattr(server, "k8s_core_api", fake)
        result = await server.list_pods_in_namespace(namespace="ns1", limit=3)
        assert "_truncation" in result[-1]
        info = result[-1]["_truncation"]
        assert info["truncated"] is True
        assert info["returned"] == 3
        assert "limit" in info["note"]

    @pytest.mark.asyncio
    async def test_complete_response_has_no_truncation_entry(self, server, monkeypatch):
        # Mutation target: boundary — no continue token -> list shape UNCHANGED.
        fake = _FakePodApi(3, continue_token=None)
        monkeypatch.setattr(server, "k8s_core_api", fake)
        result = await server.list_pods_in_namespace(namespace="ns1")
        assert len(result) == 3
        assert all("_truncation" not in entry for entry in result)


# ── Topology bounding (pure helper + wiring) ─────────────────────────────────

def _fat_topology(n_nodes=200, n_edges=400):
    pad = "x" * 200
    return {
        "topology": {
            "nodes": [{"id": f"n{i}", "detail": pad} for i in range(n_nodes)],
            "edges": [{"src": f"n{i % n_nodes}", "dst": f"n{(i + 1) % n_nodes}",
                       "detail": pad} for i in range(n_edges)],
        },
        "summary": {"total_nodes": n_nodes, "total_relationships": n_edges},
        "permissions": {"accessible": [], "denied": [], "errors": []},
        "last_updated": "2026-07-24T00:00:00",
    }


class TestTopologyBounding:
    def test_over_budget_result_is_bounded_and_reported(self, server):
        result = _fat_topology()
        budget = 2000
        bounded = server._bound_topology_result(result, budget)
        text = json.dumps(bounded, default=str)
        assert server.calculate_context_tokens(text) <= budget
        info = bounded["_truncation"]
        assert info["truncated"] is True
        assert info["nodes_total"] == 200 and info["edges_total"] == 400
        assert info["nodes_kept"] <= 200 and info["edges_kept"] < 400
        assert "max_context_tokens" in info["note"]

    def test_edges_dropped_before_nodes(self, server):
        bounded = server._bound_topology_result(_fat_topology(), 20000)
        info = bounded["_truncation"]
        # generous budget: edge-halving alone should suffice, nodes untouched
        assert info["edges_kept"] < info["edges_total"]
        assert info["nodes_kept"] == info["nodes_total"]

    def test_under_budget_result_unchanged(self, server):
        # Mutation target: boundary — small result passes through IDENTICALLY.
        result = _fat_topology(n_nodes=3, n_edges=3)
        bounded = server._bound_topology_result(result, 50000)
        assert bounded is result
        assert "_truncation" not in bounded

    @pytest.mark.asyncio
    async def test_tool_wires_budget_into_bounder(self, server, monkeypatch):
        class _EmptyListApi:
            """Any method call returns an empty k8s-style list response."""
            def __getattr__(self, name):
                def _fn(*a, **k):
                    return SimpleNamespace(items=[])
                return _fn

        async def _one_cluster(*a, **k):
            api = _EmptyListApi()
            return {"fake-cluster": {"core_api": api, "apps_api": api,
                                     "custom_api": api, "storage_api": api,
                                     "batch_api": api}}
        monkeypatch.setattr(server, "get_multi_cluster_topology_clients", _one_cluster)
        seen = {}
        real = server._bound_topology_result

        def spy(result, max_tokens):
            seen["max_tokens"] = max_tokens
            return real(result, max_tokens)

        monkeypatch.setattr(server, "_bound_topology_result", spy)
        out = await server.live_system_topology_mapper(max_context_tokens=12345)
        assert seen.get("max_tokens") == 12345
        assert "error" not in out
