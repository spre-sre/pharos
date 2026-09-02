"""Behavioral proof: get_multi_cluster_topology_clients returns read-only
wrapped clients — the single choke point for all topology-family reads."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Insert order is load-bearing: src/ MUST end up at sys.path[0] (tests/core/
# would otherwise shadow src/core for _readonly_spy's `from core...` import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import helpers.resource_topology as rt
from _readonly_spy import make_spy
from core.readonly_client import WriteOperationError


@pytest.mark.asyncio
async def test_topology_factory_returns_readonly_clients(monkeypatch):
    record = []
    monkeypatch.setattr(rt, "ReadOnlyK8sClient", make_spy(record))

    clients = await rt.get_multi_cluster_topology_clients(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    current = clients["current"]

    # One read verb per returned client goes through the spy...
    current["core_api"].list_namespaced_pod
    current["custom_api"].list_namespaced_custom_object
    current["apps_api"].list_namespaced_deployment
    current["storage_api"].list_storage_class
    current["batch_api"].list_namespaced_job
    for verb in ("list_namespaced_pod", "list_namespaced_custom_object",
                 "list_namespaced_deployment", "list_storage_class",
                 "list_namespaced_job"):
        assert verb in record, f"{verb} not routed through ReadOnlyK8sClient; record={record}"

    # ...and write verbs are structurally absent.
    with pytest.raises(WriteOperationError):
        current["core_api"].delete_namespaced_pod
