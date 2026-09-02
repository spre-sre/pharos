"""Behavioral pins for get_machine_config_pool_status node details.

Complements the AST pins in test_mcp_pool_status_nodes.py (review round 1):
the hoisted single node fetch must still yield correct per-pool selector
filtering, and a node-fetch failure must be SURFACED per pool — not an empty
list indistinguishable from "no nodes match" (the fleet-report agents that
motivated the fix would read that as fact).
"""
from types import SimpleNamespace

import pytest


def _mcp_pool(name, match_labels, flat=False):
    # Real MachineConfigPools nest labels under nodeSelector.matchLabels.
    # Bug 4 (memory: pharos-tool-bugs-live-testing): the filter iterated the
    # selector flat and every real pool matched zero nodes. The filter must
    # unwrap matchLabels; the legacy flat form stays supported.
    selector = match_labels if flat else {"matchLabels": match_labels}
    return {
        "metadata": {"name": name},
        "spec": {"nodeSelector": selector,
                 "machineConfigSelector": {"matchLabels": {}},
                 "paused": False},
        "status": {"machineCount": 1, "readyMachineCount": 1,
                   "updatedMachineCount": 1, "degradedMachineCount": 0,
                   "conditions": []},
    }


def _node(name, labels):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels, annotations={}),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")]),
    )


def _fake_clients(pools):
    custom = SimpleNamespace(
        list_cluster_custom_object=lambda **kw: {"items": pools})
    return SimpleNamespace(custom_api=custom, core_api=object())


@pytest.mark.asyncio
async def test_pool_node_details_filtered_per_pool(server, monkeypatch):
    pools = [_mcp_pool("workers-a", {"role": "a"}),
             _mcp_pool("workers-b", {"role": "b"})]
    monkeypatch.setattr(server, "_resolve_k8s",
                        lambda source: (_fake_clients(pools), None))
    monkeypatch.setattr(server, "_gate_extension", lambda *a, **k: None)
    monkeypatch.setattr(server.ReadOnlyK8sClient, "wrap",
                        staticmethod(lambda c: c))

    async def fake_nodes(core_api, request_timeout=30.0):
        return SimpleNamespace(items=[_node("node-a", {"role": "a"}),
                                      _node("node-b", {"role": "b"})])
    monkeypatch.setattr(server, "list_nodes_bounded", fake_nodes)

    result = await server.get_machine_config_pool_status(
        include_update_history=False)

    by_name = {p["name"]: p for p in result["machine_config_pools"]}
    assert [n["name"] for n in by_name["workers-a"]["node_status"]] == ["node-a"]
    assert [n["name"] for n in by_name["workers-b"]["node_status"]] == ["node-b"]


@pytest.mark.asyncio
async def test_pool_node_details_legacy_flat_selector_still_works(server, monkeypatch):
    pools = [_mcp_pool("workers-flat", {"role": "a"}, flat=True)]
    monkeypatch.setattr(server, "_resolve_k8s",
                        lambda source: (_fake_clients(pools), None))
    monkeypatch.setattr(server, "_gate_extension", lambda *a, **k: None)
    monkeypatch.setattr(server.ReadOnlyK8sClient, "wrap",
                        staticmethod(lambda c: c))

    async def fake_nodes(core_api, request_timeout=30.0):
        return SimpleNamespace(items=[_node("node-a", {"role": "a"}),
                                      _node("node-b", {"role": "b"})])
    monkeypatch.setattr(server, "list_nodes_bounded", fake_nodes)

    result = await server.get_machine_config_pool_status(
        include_update_history=False)
    by_name = {p["name"]: p for p in result["machine_config_pools"]}
    assert [n["name"] for n in by_name["workers-flat"]["node_status"]] == ["node-a"]


@pytest.mark.asyncio
async def test_match_expressions_only_pool_claims_no_nodes(server, monkeypatch):
    """Review MAJOR-2: a matchExpressions-only pool must NOT silently claim
    every node in the cluster (empty unwrapped selector -> match-all loop).
    Unsupported selector -> empty node_status plus an explicit marker."""
    pool = _mcp_pool("workers-expr", {})
    pool["spec"]["nodeSelector"] = {
        "matchExpressions": [{"key": "role", "operator": "In", "values": ["a"]}]}
    monkeypatch.setattr(server, "_resolve_k8s",
                        lambda source: (_fake_clients([pool]), None))
    monkeypatch.setattr(server, "_gate_extension", lambda *a, **k: None)
    monkeypatch.setattr(server.ReadOnlyK8sClient, "wrap",
                        staticmethod(lambda c: c))

    async def fake_nodes(core_api, request_timeout=30.0):
        return SimpleNamespace(items=[_node("node-a", {"role": "a"}),
                                      _node("node-b", {"role": "b"})])
    monkeypatch.setattr(server, "list_nodes_bounded", fake_nodes)

    result = await server.get_machine_config_pool_status(
        include_update_history=False)
    pool_out = result["machine_config_pools"][0]
    assert pool_out["node_status"] == [], (
        f"matchExpressions-only pool claimed nodes: {pool_out['node_status']}"
    )
    assert pool_out.get("node_selector_unsupported") == "matchExpressions"


@pytest.mark.asyncio
async def test_mixed_selector_pool_marked_partial(server, monkeypatch):
    """Re-review MINOR-4: matchLabels + matchExpressions filters on labels
    only — that superset must carry an explicit partial-filter marker."""
    pool = _mcp_pool("workers-mixed", {"role": "a"})
    pool["spec"]["nodeSelector"]["matchExpressions"] = [
        {"key": "zone", "operator": "In", "values": ["us-east"]}]
    monkeypatch.setattr(server, "_resolve_k8s",
                        lambda source: (_fake_clients([pool]), None))
    monkeypatch.setattr(server, "_gate_extension", lambda *a, **k: None)
    monkeypatch.setattr(server.ReadOnlyK8sClient, "wrap",
                        staticmethod(lambda c: c))

    async def fake_nodes(core_api, request_timeout=30.0):
        return SimpleNamespace(items=[_node("node-a", {"role": "a"})])
    monkeypatch.setattr(server, "list_nodes_bounded", fake_nodes)

    result = await server.get_machine_config_pool_status(
        include_update_history=False)
    pool_out = result["machine_config_pools"][0]
    assert [n["name"] for n in pool_out["node_status"]] == ["node-a"]
    assert pool_out.get("node_selector_partial") == "matchExpressions"


@pytest.mark.asyncio
async def test_pool_node_fetch_failure_is_surfaced(server, monkeypatch):
    pools = [_mcp_pool("workers-a", {"role": "a"}),
             _mcp_pool("workers-b", {"role": "b"})]
    monkeypatch.setattr(server, "_resolve_k8s",
                        lambda source: (_fake_clients(pools), None))
    monkeypatch.setattr(server, "_gate_extension", lambda *a, **k: None)
    monkeypatch.setattr(server.ReadOnlyK8sClient, "wrap",
                        staticmethod(lambda c: c))

    async def failing_nodes(core_api, request_timeout=30.0):
        raise TimeoutError("node list timed out (test-injected)")
    monkeypatch.setattr(server, "list_nodes_bounded", failing_nodes)

    result = await server.get_machine_config_pool_status(
        include_update_history=False)

    for pool in result["machine_config_pools"]:
        assert pool["node_status"] == []
        assert pool.get("node_status_error"), (
            f"pool {pool['name']}: empty node_status with no error marker is "
            f"indistinguishable from 'no nodes match this pool'"
        )
