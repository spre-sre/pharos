"""Behavioral proof: high-traffic CoreV1 tool reads route through
ReadOnlyK8sClient (spec SS4.7, phase 1e)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy


@pytest.mark.asyncio
async def test_list_namespaces_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    ns = MagicMock()
    ns.metadata.name = "team-a"
    fake = MagicMock()
    fake.list_namespace.return_value = MagicMock(items=[ns])
    monkeypatch.setattr(server, "k8s_core_api", fake)
    # Bust the namespace cache so the fetch path actually runs.
    # The cache is now an instance-keyed dict (empty = all misses).
    monkeypatch.setattr(server, "_namespace_cache", {})

    result = await server.list_namespaces()

    assert "list_namespace" in record, (
        f"list_namespaces read not routed through ReadOnlyK8sClient; record={record}")
    assert result == ["team-a"]


@pytest.mark.asyncio
async def test_list_pods_in_namespace_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[])
    monkeypatch.setattr(server, "k8s_core_api", fake)

    result = await server.list_pods_in_namespace(namespace="team-a")

    assert "list_namespaced_pod" in record, (
        f"list_pods_in_namespace read not routed through ReadOnlyK8sClient; record={record}")
    assert result == []


@pytest.mark.asyncio
async def test_check_resource_constraints_routes_readonly(server, monkeypatch):
    """Direct reads (quota + per-pod read) route through server's wrapper; the
    list_pods call is covered separately by the utils-internal wrap test."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    pod = MagicMock()
    pod.metadata.name = "api-1"
    pod.status.phase = "Running"
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[pod])
    fake.list_namespaced_resource_quota.return_value = MagicMock(items=[])
    fake.read_namespaced_pod.return_value = pod
    monkeypatch.setattr(server, "k8s_core_api", fake)

    await server.check_resource_constraints(namespace="team-a")

    assert "list_namespaced_resource_quota" in record, (
        f"quota read not routed; record={record}")
    assert "read_namespaced_pod" in record, (
        f"per-pod read not routed; record={record}")


@pytest.mark.asyncio
async def test_tekton_status_core_read_routes_readonly(server, monkeypatch):
    """The 1e CoreV1 list_namespace read (tenant-label) routes through the
    wrapper; the custom reads were proven in phase 1d."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake_core = MagicMock()
    fake_core.list_namespace.return_value = MagicMock(items=[])
    monkeypatch.setattr(server, "k8s_core_api", fake_core)
    fake_custom = MagicMock()
    fake_custom.list_cluster_custom_object.return_value = {"items": []}
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    await server.get_tekton_pipeline_runs_status()

    assert "list_namespace" in record, (
        f"tenant list_namespace not routed through ReadOnlyK8sClient; record={record}")
