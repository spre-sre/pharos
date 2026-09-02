"""Behavioral proof: param-receiving helpers wrap their k8s clients internally
(spec SS4.7, phase 1e)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Insert order is load-bearing: src/ MUST end up at sys.path[0].  This file
# imports helpers.utils (guarded `from core.readonly_client import`) at
# collection time; if tests/ preceded src/, the tests/core/ package would
# shadow src/core and poison `import core` for _readonly_spy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import extensions.konflux.lineage as lineage_mod
import helpers.resource_topology as rt
import helpers.utils as hu
from _readonly_spy import make_spy


@pytest.mark.asyncio
async def test_utils_list_pods_routes_readonly(monkeypatch):
    record = []
    monkeypatch.setattr(hu, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[])

    await hu.list_pods("team-a", fake, MagicMock())

    assert "list_namespaced_pod" in record, (
        f"utils.list_pods read not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_multi_cluster_clients_factory_returns_readonly(monkeypatch):
    record = []
    monkeypatch.setattr(rt, "ReadOnlyK8sClient", make_spy(record))

    clients = await rt.get_multi_cluster_clients(MagicMock(), MagicMock(), MagicMock())
    current = clients["current"]
    current["core_api"].list_namespace
    current["custom_api"].list_namespaced_custom_object
    current["apps_api"].list_namespaced_deployment

    for verb in ("list_namespace", "list_namespaced_custom_object",
                 "list_namespaced_deployment"):
        assert verb in record, f"{verb} not routed; record={record}"


@pytest.mark.asyncio
async def test_follow_lifecycle_chain_wraps_custom_api(monkeypatch):
    """The param-shadow wrap routes resolver reads.  pipeline_flow fake data
    may be adjusted (Step 1) to whatever minimal shape triggers one resolver;
    the routing assertion may not be weakened."""
    record = []
    monkeypatch.setattr(lineage_mod, "ReadOnlyK8sClient", make_spy(record))
    fake_custom = MagicMock()
    fake_custom.get_namespaced_custom_object.return_value = {"metadata": {}, "status": {}}
    fake_custom.list_namespaced_custom_object.return_value = {"items": []}

    # The snapshot annotation is what triggers a resolver (lineage.py
    # follow_lifecycle_chain reads it; a bare kind/name entry hits `continue`
    # and resolves nothing).
    await lineage_mod.follow_lifecycle_chain(
        [{"namespace": "team-a",
          "annotations": {"appstudio.openshift.io/snapshot": "snap-1"}}],
        fake_custom, MagicMock())

    assert any(v in record for v in
               ("get_namespaced_custom_object", "list_namespaced_custom_object")), (
        f"no resolver read routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_collect_baseline_routes_readonly(monkeypatch):
    """Signature is (scope: dict, k8s_core_api, list_namespaces_fn, list_pods_fn)
    — utils.py:2343.  list_pods_fn MUST be an AsyncMock returning a list: if it
    raises, the quota read (:2385, same try) is skipped and the assertion fails
    for the wrong reason."""
    from unittest.mock import AsyncMock
    record = []
    monkeypatch.setattr(hu, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespaced_resource_quota.return_value = MagicMock(items=[])
    fake.list_node.return_value = MagicMock(items=[])

    await hu.collect_baseline_system_data(
        {"namespaces": ["team-a"]}, fake,
        AsyncMock(return_value=["team-a"]), AsyncMock(return_value=[]))

    assert "list_namespaced_resource_quota" in record and "list_node" in record, (
        f"baseline reads not routed; record={record}")


@pytest.mark.asyncio
async def test_identify_affected_components_routes_readonly(monkeypatch):
    """Found by the phase-1e final review: two raw reads reachable from the
    live what_if_scenario_simulator tool.  Both must route."""
    from unittest.mock import AsyncMock
    record = []
    monkeypatch.setattr(rt, "ReadOnlyK8sClient", make_spy(record))
    fake_core = MagicMock()
    fake_core.list_namespaced_service.return_value = MagicMock(items=[])
    fake_apps = MagicMock()
    fake_apps.list_namespaced_deployment.return_value = MagicMock(items=[])

    # scaling path → exercises k8s_apps_api.list_namespaced_deployment
    await rt.identify_affected_components(
        {"app": "foo"}, {"namespaces": ["test-ns"]}, "scaling",
        fake_core, fake_apps, AsyncMock(return_value=[]), AsyncMock(return_value=[]),
    )
    # configuration path → exercises k8s_core_api.list_namespaced_service
    await rt.identify_affected_components(
        {}, {"namespaces": ["test-ns"]}, "configuration",
        fake_core, fake_apps, AsyncMock(return_value=[]), AsyncMock(return_value=[]),
    )

    assert "list_namespaced_deployment" in record, (
        f"apps read not routed; record={record}")
    assert "list_namespaced_service" in record, (
        f"core service read not routed; record={record}")
