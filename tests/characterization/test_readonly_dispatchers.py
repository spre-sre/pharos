"""Behavioral proof: the dynamic dispatchers route every API family through
ReadOnlyK8sClient (spec SS4.7, phase 1e)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy


@pytest.mark.asyncio
async def test_get_kubernetes_resource_routes_readonly_core(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    monkeypatch.setattr(server, "k8s_core_api", fake)

    await server.get_kubernetes_resource(
        resource_type="pod", name="api-1", namespace="team-a")

    assert "read_namespaced_pod" in record, (
        f"core dispatch not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_get_kubernetes_resource_routes_readonly_apps(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    monkeypatch.setattr(server, "k8s_apps_api", fake)

    await server.get_kubernetes_resource(
        resource_type="deployment", name="api-1", namespace="team-a")

    assert "read_namespaced_deployment" in record, (
        f"apps dispatch not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_get_kubernetes_resource_routes_readonly_batch(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    monkeypatch.setattr(server, "k8s_batch_api", fake)

    await server.get_kubernetes_resource(
        resource_type="job", name="api-1", namespace="team-a")

    assert "read_namespaced_job" in record, (
        f"batch dispatch not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_get_kubernetes_resource_routes_readonly_autoscaling(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    monkeypatch.setattr(server, "k8s_autoscaling_api", fake)

    await server.get_kubernetes_resource(
        resource_type="hpa", name="api-1", namespace="team-a")

    assert "read_namespaced_horizontal_pod_autoscaler" in record, (
        f"autoscaling dispatch not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_get_kubernetes_resource_routes_readonly_storage(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    monkeypatch.setattr(server, "k8s_storage_api", fake)

    await server.get_kubernetes_resource(
        resource_type="storageclass", name="fast", namespace="team-a")

    assert "read_storage_class" in record, (
        f"storage dispatch not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_get_kubernetes_resource_routes_readonly_custom(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.get_namespaced_custom_object.return_value = {"metadata": {}}
    monkeypatch.setattr(server, "k8s_custom_api", fake)

    await server.get_kubernetes_resource(
        resource_type="pipelinerun", name="build-1", namespace="team-a")

    assert "get_namespaced_custom_object" in record, (
        f"custom dispatch not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_search_by_labels_namespaced_routes_readonly(server, monkeypatch):
    """One invocation covering all four namespaced API branches."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    empty = MagicMock()
    empty.items = []
    fake_core = MagicMock()
    fake_core.list_namespaced_pod.return_value = empty
    fake_apps = MagicMock()
    fake_apps.list_namespaced_deployment.return_value = empty
    fake_batch = MagicMock()
    fake_batch.list_namespaced_job.return_value = empty
    fake_custom = MagicMock()
    fake_custom.list_namespaced_custom_object.return_value = {"items": []}
    monkeypatch.setattr(server, "k8s_core_api", fake_core)
    monkeypatch.setattr(server, "k8s_apps_api", fake_apps)
    monkeypatch.setattr(server, "k8s_batch_api", fake_batch)
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    # label_selectors elements are DICTS — build_advanced_label_selector calls
    # .get() on each (utils.py:1533); a plain string would AttributeError.
    await server.search_resources_by_labels(
        resource_types=["pods", "deployments", "jobs", "pipelineruns"],
        label_selectors=[{"key": "app", "value": "x", "operator": "equals"}],
        namespaces=["team-a"])

    for verb in ("list_namespaced_pod", "list_namespaced_deployment",
                 "list_namespaced_job", "list_namespaced_custom_object"):
        assert verb in record, f"{verb} not routed through ReadOnlyK8sClient; record={record}"


@pytest.mark.asyncio
async def test_search_by_labels_cluster_scoped_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    empty = MagicMock()
    empty.items = []
    fake_core = MagicMock()
    fake_core.list_node.return_value = empty
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    await server.search_resources_by_labels(
        resource_types=["nodes"],
        label_selectors=[{"key": "role", "value": "worker", "operator": "equals"}],
        namespaces=["team-a"])

    assert "list_node" in record, (
        f"cluster-scoped core dispatch not routed; record={record}")


@pytest.mark.asyncio
async def test_search_by_labels_ns_autodetect_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    ns = MagicMock()
    ns.metadata.name = "team-a"
    fake_core = MagicMock()
    fake_core.list_namespace.return_value = MagicMock(items=[ns])
    fake_core.list_namespaced_pod.return_value = MagicMock(items=[])
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    await server.search_resources_by_labels(
        resource_types=["pods"],
        label_selectors=[{"key": "app", "value": "x", "operator": "equals"}])

    assert "list_namespace" in record, (
        f"namespace auto-detect not routed; record={record}")
