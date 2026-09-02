"""Behavioral proof: pipeline-family CustomObjects reads route through
ReadOnlyK8sClient (spec SS4.7).  All fakes return empty items so every tool
exits on its empty-result path — no downstream mocking needed."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy


def _empty_custom_api():
    fake = MagicMock()
    fake.list_namespaced_custom_object.return_value = {"items": []}
    fake.list_cluster_custom_object.return_value = {"items": []}
    return fake


@pytest.mark.asyncio
async def test_list_pipelineruns_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    monkeypatch.setattr(server, "k8s_custom_api", _empty_custom_api())

    result = await server.list_pipelineruns(namespace="team-a")

    assert "list_namespaced_custom_object" in record, (
        f"list_pipelineruns read not routed through ReadOnlyK8sClient; record={record}")
    assert result == []


@pytest.mark.asyncio
async def test_list_taskruns_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    monkeypatch.setattr(server, "k8s_custom_api", _empty_custom_api())

    result = await server.list_taskruns(namespace="team-a")

    assert "list_namespaced_custom_object" in record, (
        f"list_taskruns read not routed through ReadOnlyK8sClient; record={record}")
    assert result == []


@pytest.mark.asyncio
async def test_list_recent_pipeline_runs_routes_readonly(server, monkeypatch):
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    monkeypatch.setattr(server, "k8s_custom_api", _empty_custom_api())

    await server.list_recent_pipeline_runs()

    assert "list_cluster_custom_object" in record, (
        f"list_recent_pipeline_runs read not routed through ReadOnlyK8sClient; record={record}")


@pytest.mark.asyncio
async def test_find_pipeline_routes_readonly(server, monkeypatch):
    """All 5 fetch closures route through the wrapper.  Invocation A
    (namespaces=None, include_taskruns=True) exercises PLR-cluster, TR-cluster,
    repositories.  Invocation B (namespaces=['team-a'], include_taskruns=True)
    exercises PLR-namespaced, TR-namespaced, repositories."""
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    monkeypatch.setattr(server, "k8s_custom_api", _empty_custom_api())

    await server.find_pipeline("nomatch-pattern", include_taskruns=True)
    cluster_reads = record.count("list_cluster_custom_object")
    assert cluster_reads >= 3, (
        f"Expected >=3 cluster reads (PLR + TR + repositories) through "
        f"ReadOnlyK8sClient; got {cluster_reads}; record={record}")

    record.clear()
    await server.find_pipeline("nomatch-pattern", include_taskruns=True,
                               namespaces=["team-a"])
    assert record.count("list_namespaced_custom_object") >= 2, (
        f"Expected >=2 namespaced reads (PLR + TR) through ReadOnlyK8sClient; "
        f"record={record}")


@pytest.mark.asyncio
async def test_tekton_status_routes_readonly(server, monkeypatch):
    """get_tekton_pipeline_runs_status custom reads route through the wrapper.

    F-02 fix: discovery is now ONE cluster-wide PLR call; active_namespaces is
    derived from PR metadata; the tenant-label list_namespace call narrows the
    set (option-a).

    Invocation A: cluster-wide PLR call fires + per-ns TaskRun read fires for
    the active namespace; list_cluster_custom_object and
    list_namespaced_custom_object are both routed through ReadOnlyK8sClient.
    Invocation B: list_namespace fails (exception) → tenant filter skipped →
    cluster-wide PLR still fires (unchanged primary path)."""
    # Invocation A: cluster-wide discovery returns one PLR in team-a → active
    # ns = team-a (after tenant-label filter matches); TaskRun fetch fires.
    record = []
    monkeypatch.setattr(server, "ReadOnlyK8sClient", make_spy(record))
    custom_a = MagicMock()
    custom_a.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"namespace": "team-a"}, "status": {}}]}
    custom_a.list_namespaced_custom_object.return_value = {"items": []}
    monkeypatch.setattr(server, "k8s_custom_api", custom_a)
    ns = MagicMock()
    ns.metadata.name = "team-a"
    core_a = MagicMock()
    core_a.list_namespace.return_value = MagicMock(items=[ns])
    monkeypatch.setattr(server, "k8s_core_api", core_a)

    await server.get_tekton_pipeline_runs_status()

    assert "list_cluster_custom_object" in record, (
        f"cluster-wide PLR discovery not routed through "
        f"ReadOnlyK8sClient; record={record}")
    assert record.count("list_namespaced_custom_object") >= 1, (
        f"Expected per-ns TaskRun read through ReadOnlyK8sClient; record={record}")

    # Invocation B: list_namespace raises → tenant filter skipped gracefully →
    # cluster-wide PLR call still fires (it is the primary path, not a fallback).
    record.clear()
    custom_b = MagicMock()
    custom_b.list_cluster_custom_object.return_value = {"items": []}
    monkeypatch.setattr(server, "k8s_custom_api", custom_b)
    core_b = MagicMock()
    core_b.list_namespace.side_effect = Exception("no perms")
    monkeypatch.setattr(server, "k8s_core_api", core_b)

    await server.get_tekton_pipeline_runs_status()

    assert "list_cluster_custom_object" in record, (
        f"cluster-wide PLR read not routed through "
        f"ReadOnlyK8sClient; record={record}")
