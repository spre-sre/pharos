"""Behavioral proof: prometheus/thanos service-discovery and cert-health reads
route through ReadOnlyK8sClient (spec SS4.7, phase 1e)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy

import helpers.prometheus as _helpers_prometheus


@pytest.mark.asyncio
async def test_prom_crd_core_read_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(_helpers_prometheus, "ReadOnlyK8sClient", make_spy(record))
    fake_custom = MagicMock()
    fake_custom.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"name": "k8s", "namespace": "openshift-monitoring"}}]}
    fake_core = MagicMock()
    fake_core.read_namespaced_service.side_effect = Exception("not found")

    await _helpers_prometheus._discover_prometheus_via_operator_crd(
        custom_api=fake_custom,
        core_api=fake_core,
    )

    assert "read_namespaced_service" in record, (
        f"CRD-discovery service read not routed; record={record}")


@pytest.mark.asyncio
async def test_prom_services_discovery_routes_readonly(server, monkeypatch):
    """Both reads (per-namespace + all-namespaces label fallback) route."""
    record = []
    monkeypatch.setattr(_helpers_prometheus, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespaced_service.return_value = MagicMock(items=[])
    fake.list_service_for_all_namespaces.return_value = MagicMock(items=[])

    result = await _helpers_prometheus._discover_prometheus_via_services(core_api=fake)

    assert "list_namespaced_service" in record and \
        "list_service_for_all_namespaces" in record, (
        f"services discovery reads not both routed; record={record}")
    assert result is None


@pytest.mark.asyncio
async def test_thanos_services_discovery_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(_helpers_prometheus, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespaced_service.return_value = MagicMock(items=[])
    fake.list_service_for_all_namespaces.return_value = MagicMock(items=[])

    result = await _helpers_prometheus._discover_thanos_via_services(core_api=fake)

    assert "list_namespaced_service" in record and \
        "list_service_for_all_namespaces" in record, (
        f"thanos discovery reads not both routed; record={record}")
    assert result is None


@pytest.mark.asyncio
async def test_cert_health_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespace.return_value = MagicMock(items=[])
    fake.list_namespaced_secret.return_value = MagicMock(items=[])
    monkeypatch.setattr(server, "k8s_core_api", fake)

    await server.check_cluster_certificate_health()

    assert "list_namespace" in record, f"cert ns-list not routed; record={record}"
    assert "list_namespaced_secret" in record, (
        f"cert secret reads not routed; record={record}")
