"""Behavioral proof: prometheus-discovery and OpenShift-status CustomObjects
reads route through ReadOnlyK8sClient (spec SS4.7)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy

import helpers.prometheus as _helpers_prometheus


def _empty_custom_api():
    fake = MagicMock()
    fake.list_namespaced_custom_object.return_value = {"items": []}
    fake.list_cluster_custom_object.return_value = {"items": []}
    return fake


@pytest.mark.asyncio
async def test_prometheus_route_discovery_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(_helpers_prometheus, "ReadOnlyK8sClient", make_spy(record))

    result = await _helpers_prometheus._discover_prometheus_via_routes(custom_api=_empty_custom_api())

    assert "list_namespaced_custom_object" in record, (
        f"route discovery not routed through ReadOnlyK8sClient; record={record}")
    assert result is None  # empty items -> no route found


@pytest.mark.asyncio
async def test_prometheus_crd_discovery_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(_helpers_prometheus, "ReadOnlyK8sClient", make_spy(record))

    result = await _helpers_prometheus._discover_prometheus_via_operator_crd(
        custom_api=_empty_custom_api(),
        core_api=MagicMock(),
    )

    assert "list_cluster_custom_object" in record, (
        f"CRD discovery not routed through ReadOnlyK8sClient; record={record}")
    assert result is None


@pytest.mark.asyncio
async def test_machine_config_pool_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    monkeypatch.setattr(server, "k8s_custom_api", _empty_custom_api())

    await server.get_machine_config_pool_status()

    assert record.count("list_cluster_custom_object") == 2, (
        f"Expected BOTH MachineConfigPool reads (pools + machineconfigs) through "
        f"ReadOnlyK8sClient; got {record.count('list_cluster_custom_object')}; "
        f"record={record}")


@pytest.mark.asyncio
async def test_cluster_operator_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    monkeypatch.setattr(server, "k8s_custom_api", _empty_custom_api())

    await server.get_openshift_cluster_operator_status()

    assert record.count("list_cluster_custom_object") == 2, (
        f"Expected BOTH ClusterOperator reads (clusteroperators + clusterversions) "
        f"through ReadOnlyK8sClient; got {record.count('list_cluster_custom_object')}; "
        f"record={record}")


@pytest.mark.asyncio
async def test_execute_prometheus_query_internal_forwards_clients(server, monkeypatch):
    """Regression (group-5 fix): _execute_prometheus_query_internal must forward
    custom_api/core_api into _discover_prometheus_endpoint.  Pre-fix, the function
    called _discover_prometheus_endpoint() with no clients, so K8s-based discovery
    was always skipped when called as a passed-in callable (what_if_scenario_simulator,
    extension query_prometheus) without PROMETHEUS_URL set."""
    fake_custom = object()
    fake_core = object()
    captured = {}

    async def _capture_discover(cluster_override=None, *, custom_api=None, core_api=None, source: str = ""):
        captured["custom_api"] = custom_api
        captured["core_api"] = core_api
        return (None, None)  # returns early; no aiohttp needed

    monkeypatch.setattr(_helpers_prometheus, "_discover_prometheus_endpoint", _capture_discover)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("THANOS_URL", raising=False)

    await _helpers_prometheus._execute_prometheus_query_internal(
        "up", custom_api=fake_custom, core_api=fake_core
    )

    assert captured.get("custom_api") is fake_custom, (
        f"custom_api not forwarded; captured={captured}")
    assert captured.get("core_api") is fake_core, (
        f"core_api not forwarded; captured={captured}")


@pytest.mark.asyncio
async def test_what_if_simulator_partial_binds_k8s_clients(server, monkeypatch):
    """what_if_scenario_simulator must bind custom_api and core_api into the
    functools.partial it passes as prometheus_query_fn to
    load_historical_performance_data.  Fails if either kwarg is absent from
    the partial's .keywords — Prometheus-based calibration would silently use
    the wrong (or no) clients for endpoint discovery."""
    import functools
    from unittest.mock import AsyncMock, MagicMock

    fake_custom = object()
    fake_core = object()
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom, raising=False)
    monkeypatch.setattr(server, "k8s_core_api", fake_core, raising=False)

    captured_fn = {}

    async def _capture_load_historical(scope, simulation_duration, *,
                                       prometheus_query_fn=None):
        captured_fn["fn"] = prometheus_query_fn
        return {}

    monkeypatch.setattr(server, "collect_baseline_system_data",
                        AsyncMock(return_value={}))
    monkeypatch.setattr(server, "build_system_behavior_models",
                        AsyncMock(return_value={}))
    monkeypatch.setattr(server, "load_historical_performance_data",
                        _capture_load_historical)
    monkeypatch.setattr(server, "calibrate_simulation_models",
                        MagicMock(return_value={}))
    monkeypatch.setattr(server, "run_monte_carlo_simulation",
                        AsyncMock(return_value={}))
    monkeypatch.setattr(server, "analyze_system_impact",
                        MagicMock(return_value={}))
    monkeypatch.setattr(server, "identify_affected_components",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(server, "perform_risk_assessment",
                        MagicMock(return_value={}))
    monkeypatch.setattr(server, "calculate_simulation_quality",
                        MagicMock(return_value={}))
    monkeypatch.setattr(server, "generate_simulation_recommendations",
                        MagicMock(return_value=[]))

    await server.what_if_scenario_simulator(
        scenario_type="scaling",
        changes={"replicas": {"before": 1, "after": 2}},
    )

    fn = captured_fn.get("fn")
    assert fn is not None, (
        "prometheus_query_fn was not passed to load_historical_performance_data"
    )
    assert isinstance(fn, functools.partial), (
        f"prometheus_query_fn should be a functools.partial; got {type(fn)}"
    )
    assert fn.keywords.get("custom_api") is fake_custom, (
        "what_if_scenario_simulator dropped custom_api from prometheus_query_fn "
        "partial — _execute_prometheus_query_internal will not receive the client"
    )
    assert fn.keywords.get("core_api") is fake_core, (
        "what_if_scenario_simulator dropped core_api from prometheus_query_fn "
        "partial — _execute_prometheus_query_internal will not receive the client"
    )
