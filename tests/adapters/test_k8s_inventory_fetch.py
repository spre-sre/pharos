"""Contract tests for the kubernetes InventorySource implementation (spec SS4.2).
The adapter wraps the client internally (read-only by construction) and
returns canonical InventoryBatch values."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.kubernetes.inventory import fetch_inventory
from core.signals import InventoryBatch, InventoryItem


def _fake_pod(name="api-1", namespace="team-a", labels=None):
    p = MagicMock()
    p.metadata.name = name
    p.metadata.namespace = namespace
    p.metadata.labels = labels if labels is not None else {"app": "api"}
    return p


@pytest.mark.asyncio
async def test_fetch_inventory_returns_canonical_batch():
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[_fake_pod()])

    batch = await fetch_inventory(fake, "team-a")

    assert isinstance(batch, InventoryBatch)
    assert len(batch.items) == 1
    item = batch.items[0]
    assert isinstance(item, InventoryItem)
    assert (item.kind, item.name, item.namespace) == ("pod", "api-1", "team-a")
    assert item.labels == {"app": "api"}
    assert batch.provenance.adapter == "kubernetes"


@pytest.mark.asyncio
async def test_fetch_inventory_wraps_client_internally(monkeypatch):
    """Read-only by construction: the adapter must wrap before reading."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from _readonly_spy import make_spy
    import adapters.kubernetes.inventory as inv_mod

    record = []
    monkeypatch.setattr(inv_mod, "ReadOnlyK8sClient", make_spy(record))
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[])
    await fetch_inventory(fake, "team-a")

    assert "list_namespaced_pod" in record


@pytest.mark.asyncio
async def test_fetch_inventory_empty_namespace_yields_empty_batch():
    fake = MagicMock()
    fake.list_namespaced_pod.return_value = MagicMock(items=[])
    batch = await fetch_inventory(fake, "team-a")
    assert batch.items == ()
