"""Behavioral proof: forecaster helpers + MCP node read route through
ReadOnlyK8sClient (spec SS4.7, phase 1e)."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy
import helpers.utils as hu
import helpers.resource_forecasting as hr


def test_active_node_names_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(hu, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_node.return_value = MagicMock(items=[])

    server._get_active_node_names(fake)

    assert "list_node" in record, f"active-node read not routed; record={record}"


@pytest.mark.asyncio
async def test_cluster_capacity_helper_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(hr, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_node.return_value = MagicMock(items=[])

    await server._analyze_cluster_capacity_new(fake, MagicMock(), query_fn=AsyncMock(return_value={}))

    assert "list_node" in record, f"capacity read not routed; record={record}"


@pytest.mark.asyncio
async def test_mcp_node_read_routes_readonly(server, monkeypatch):
    """get_machine_config_pool_status include_node_details=True node read
    (goldens don't exercise this branch — spy is the only guard)."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake_custom = MagicMock()
    # The node read is gated on include_node_details AND non-empty pools
    # (:9328/:9330) — return one minimal pool so the branch fires (Step 1
    # confirms the exact minimal pool shape; fakes only).
    fake_custom.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"name": "worker"}, "status": {}, "spec": {}}]}
    fake_core = MagicMock()
    fake_core.list_node.return_value = MagicMock(items=[])
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    await server.get_machine_config_pool_status(include_node_details=True)

    assert "list_node" in record, f"MCP node read not routed; record={record}"


@pytest.mark.asyncio
async def test_fallback_health_partial_wrap(server, monkeypatch):
    """Reads route through the wrapper while api_client access stays on the
    raw global (partial wrap — full wrap would break :9449/:9453).
    Function now lives in helpers.utils; spy is patched on hu.ReadOnlyK8sClient."""
    record = []
    monkeypatch.setattr(hu, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.api_client.configuration.host = "https://api.test:6443"
    fake.list_namespaced_pod.return_value = MagicMock(items=[])
    fake.list_node.return_value = MagicMock(items=[])

    result = await server._get_fallback_cluster_health(fake)

    assert "list_namespaced_pod" in record and "list_node" in record, (
        f"fallback-health reads not routed; record={record}")
    assert isinstance(result, dict)  # api_client path worked on the raw global


@pytest.mark.asyncio
async def test_analyze_node_resources_new_passes_core_api_to_active_node_names(
        server, monkeypatch):
    """_analyze_node_resources_new must forward its core_api parameter to
    _get_active_node_names.  After callable injection (Task 13), the function
    lives in helpers.resource_forecasting and resolves _get_active_node_names
    through that module's globals — patch hr._get_active_node_names, and pass
    core_api and query_fn explicitly (no longer read from server globals)."""
    fake_core = MagicMock()
    fake_core.list_node.return_value = MagicMock(items=[])

    captured = {}

    def _capture_get_active_nodes(core_api):
        captured["core_api"] = core_api
        return set()

    monkeypatch.setattr(hr, "_get_active_node_names", _capture_get_active_nodes)

    await server._analyze_node_resources_new(
        "1h", "24h", server.logger,
        query_fn=AsyncMock(return_value={"status": "success", "data": []}),
        core_api=fake_core,
    )

    assert "core_api" in captured, (
        "_get_active_node_names was not called with any argument from "
        "_analyze_node_resources_new — client injection dropped entirely"
    )
    assert captured["core_api"] is fake_core, (
        "_analyze_node_resources_new must forward core_api parameter to "
        "_get_active_node_names; a different object was received instead"
    )
