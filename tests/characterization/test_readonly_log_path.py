"""Behavioral proof: k8s reads in ML/predictive tools route through ReadOnlyCoreV1.

test_get_etcd_logs_routes_readonly:  get_etcd_logs Strategy 1 (OpenShift path).
test_predictive_routes_readonly:      all three reads in predictive_log_analyzer.
test_predictive_persistence_block_read_routes_readonly: READ 3 (persistence block,
    :10848) routes through ReadOnlyCoreV1 — proven by spy count >= 2.
test_manage_collect_routes_readonly:  collect path in manage_prediction_training_data.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy


@pytest.mark.asyncio
async def test_get_etcd_logs_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyCoreV1", make_spy(record))

    seen = {}
    real_helper = server._get_logs_with_k8s_client

    def capturing_helper(client, *a, **k):
        seen["client_type"] = type(client).__name__
        return real_helper(client, *a, **k)

    monkeypatch.setattr(server, "_get_logs_with_k8s_client", capturing_helper)

    # Build a fake that satisfies the OpenShift strategy (Strategy 1):
    # list_namespaced_pod returns one etcd pod; read_namespaced_pod_log
    # returns log text so _get_logs_with_k8s_client returns True and the
    # function exits after Strategy 1 (logs_successfully_fetched = True).
    fake = MagicMock()
    pod = MagicMock()
    pod.metadata.name = "etcd-node-1"
    fake.list_namespaced_pod.return_value = MagicMock(items=[pod])
    fake.read_namespaced_pod_log.return_value = (
        "2024-01-01T00:00:00Z INFO starting server\n"
    )
    monkeypatch.setattr(server, "k8s_core_api", fake)

    result = await server.get_etcd_logs()  # now async — requires await

    # Proof 1: list_namespaced_pod went THROUGH the ReadOnlyCoreV1 spy
    assert "list_namespaced_pod" in record, (
        f"list_namespaced_pod not recorded — raw client used instead of "
        f"ReadOnlyCoreV1 wrapper; record={record}"
    )
    # Proof 2: _get_logs_with_k8s_client received a ReadOnly-wrapped client
    assert seen.get("client_type", "").endswith("ReadOnly"), (
        f"_get_logs_with_k8s_client got raw client "
        f"(type={seen.get('client_type')}); expected ReadOnlyCoreV1 subclass"
    )
    # Sanity: the function returned a non-empty result
    assert result, f"get_etcd_logs returned empty: {result}"


@pytest.mark.asyncio
async def test_predictive_routes_readonly(server, monkeypatch):
    """READ 1 (pods block list), READ 2 (pods block log), and READ 3 (persistence
    block list) in predictive_log_analyzer all route through ReadOnlyCoreV1."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyCoreV1", make_spy(record))

    # One Running pod with enough log lines to pass the >=10 filter.
    pod = MagicMock()
    pod.metadata.name = "api-1"
    pod.status.phase = "Running"
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[pod])
    fake.read_namespaced_pod_log.return_value = "\n".join(
        ["2026-01-01T00:00:00Z ERROR crash"] * 20
    )
    # list_namespaced_event is used by _get_namespace_events_as_dicts inside
    # the persistence block.  Empty items avoids any event-processing branches.
    fake.list_namespaced_event.return_value = MagicMock(
        items=[], metadata=MagicMock(_continue=None)
    )
    monkeypatch.setattr(server, "k8s_core_api", fake)

    await server.predictive_log_analyzer(namespaces=["team-a"])

    assert "list_namespaced_pod" in record, (
        f"list_namespaced_pod (READ 1 or READ 3) not routed through "
        f"ReadOnlyCoreV1; record={record}"
    )
    assert "read_namespaced_pod_log" in record, (
        f"read_namespaced_pod_log (READ 2) not routed through "
        f"ReadOnlyCoreV1; record={record}"
    )


@pytest.mark.asyncio
async def test_predictive_persistence_block_read_routes_readonly(server, monkeypatch):
    """READ 3 (persistence/failure-label block, :10848) routes through ReadOnlyCoreV1.

    Behavioral proof: list_namespaced_pod appears >= 2 times in the spy
    record — once for READ 1 (pods block, :10786) and once for READ 3
    (persistence block, :10848).  Before the _ro hoist, ReadOnlyCoreV1 is
    never invoked so record is empty (0 calls); after the hoist both reads
    are recorded (>= 2 calls).

    Scope note: this test does NOT guard a source!=pods path.  target_namespaces
    is assigned only inside 'if source == "pods":' (:10767/:10778/:10781), so any
    code path reaching the persistence block (:10839) without executing the pods
    branch would NameError on target_namespaces before _ro matters.  The _ro hoist
    is defensive (documents intent), but the persistence-without-pods scenario is
    structurally unreachable today.  What this test proves is READ 3 routing:
    the persistence block's list_namespaced_pod call goes through the spy.
    """
    record = []
    monkeypatch.setattr(server, "ReadOnlyCoreV1", make_spy(record))

    pod = MagicMock()
    pod.metadata.name = "api-1"
    pod.status.phase = "Running"
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[pod])
    fake.read_namespaced_pod_log.return_value = "\n".join(
        ["2026-01-01T00:00:00Z ERROR crash"] * 20
    )
    fake.list_namespaced_event.return_value = MagicMock(
        items=[], metadata=MagicMock(_continue=None)
    )
    monkeypatch.setattr(server, "k8s_core_api", fake)

    # Drive with default log_sources (pods + services + nodes).  The pods
    # branch populates all_logs and sets target_namespaces; services/nodes
    # are no-ops in the current implementation.  After the loop the persistence
    # block (READ 3) runs for every ns in target_namespaces.
    await server.predictive_log_analyzer(namespaces=["team-a"])

    lnp_count = record.count("list_namespaced_pod")
    assert lnp_count >= 2, (
        f"Expected >= 2 list_namespaced_pod calls — READ 1 (pods block) + "
        f"READ 3 (persistence block, disjoint from pods block); "
        f"got {lnp_count}.  "
        f"Before the _ro hoist: record is empty (0). "
        f"After hoist both calls are recorded (2). "
        f"full record={record}"
    )


@pytest.mark.asyncio
async def test_manage_collect_routes_readonly(server, monkeypatch):
    """manage_prediction_training_data action='collect' routes its
    list_namespaced_pod call through ReadOnlyCoreV1 (spec SS4.7)."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyCoreV1", make_spy(record))

    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[])
    # list_namespaced_event is called by _get_namespace_events_as_dicts inside
    # the collect loop; empty items avoids event-processing complexity.
    fake.list_namespaced_event.return_value = MagicMock(
        items=[], metadata=MagicMock(_continue=None)
    )
    monkeypatch.setattr(server, "k8s_core_api", fake)

    # list_pipelineruns (called inside collect loop) uses k8s_custom_api.
    # Patch it here to prevent the real k8s client making network attempts.
    fake_custom = MagicMock()
    fake_custom.list_namespaced_custom_object.return_value = {"items": []}
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    # collect_from_namespaces bypasses list_namespaces / detect_tekton_namespaces
    # auto-detection so only k8s_core_api calls are exercised.
    await server.manage_prediction_training_data(
        action="collect",
        collect_from_namespaces=["team-a"],
    )

    assert "list_namespaced_pod" in record, (
        f"list_namespaced_pod in manage_prediction_training_data collect path "
        f"not routed through ReadOnlyCoreV1; record={record}"
    )


@pytest.mark.asyncio
async def test_prioritize_routes_readonly(server, monkeypatch):
    """_prioritize_pipeline_pods routes its read_namespaced_pod call through ReadOnlyCoreV1."""
    import helpers.log_analysis as _log_analysis
    record = []
    monkeypatch.setattr(_log_analysis, "ReadOnlyCoreV1", make_spy(record))

    pod = MagicMock()
    pod.status.phase = "Running"
    pod.metadata.creation_timestamp = None
    pod.status.container_statuses = []

    fake = MagicMock()
    fake.read_namespaced_pod.return_value = pod

    await server._prioritize_pipeline_pods(["pod-a"], "team-a", fake)

    assert "read_namespaced_pod" in record, (
        f"read_namespaced_pod not routed through ReadOnlyCoreV1; record={record}"
    )


@pytest.mark.asyncio
async def test_get_pipelinerun_logs_routes_readonly(server, monkeypatch):
    """get_pipelinerun_logs pod-discovery reads route through ReadOnlyCoreV1 (spec SS4.7).

    Both the primary list_namespaced_pod (tekton.dev/pipelineRun label) and the
    fallback call (tekton.dev/pipeline label) must go through the read-only
    proxy, not the raw k8s_core_api.  We return empty items[] for both calls so
    the function exits at the 'No pods found' early return, avoiding the need to
    mock get_all_pod_logs or _prioritize_pipeline_pods.
    """
    record = []
    monkeypatch.setattr(server, "ReadOnlyCoreV1", make_spy(record))

    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[])
    monkeypatch.setattr(server, "k8s_core_api", fake)

    result = await server.get_pipelinerun_logs(
        namespace="team-a", pipelinerun_name="plr-x"
    )

    assert record.count("list_namespaced_pod") == 2, (
        f"Expected 2 list_namespaced_pod calls (primary + fallback) through "
        f"ReadOnlyCoreV1 in get_pipelinerun_logs pod-discovery; "
        f"got {record.count('list_namespaced_pod')}. "
        f"Empty items[] on first call triggers fallback — both must be wrapped. "
        f"full record={record}"
    )
