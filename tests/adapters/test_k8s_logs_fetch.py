import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.kubernetes import logs as k8s_logs
from core.readonly_client import ReadOnlyCoreV1


def test_fetch_builds_per_container_batch():
    # get_all_pod_logs is async, so patch with AsyncMock
    with patch.object(k8s_logs, "get_all_pod_logs",
                      new=AsyncMock(return_value={"main": "m1\nm2", "sidecar": "s1"})) as gapl:
        batch = asyncio.run(k8s_logs.fetch_pod_logs(MagicMock(), "ns", "pod"))
    bodies = [(r.attributes["container"], r.body) for r in batch.records]
    assert ("main", "m1") in bodies and ("main", "m2") in bodies
    assert ("sidecar", "s1") in bodies
    assert batch.provenance.adapter == "kubernetes"
    # the client handed to get_all_pod_logs must be the read-only wrapper
    assert isinstance(gapl.call_args.kwargs.get("k8s_core_api")
                      or gapl.call_args.args[2], ReadOnlyCoreV1)


def test_sentinel_dict_yields_empty_batch_with_note():
    with patch.object(k8s_logs, "get_all_pod_logs",
                      new=AsyncMock(return_value={"pod_error": "pod not found"})):
        batch = asyncio.run(k8s_logs.fetch_pod_logs(MagicMock(), "ns", "gone"))
    assert batch.records == []
    assert any("pod_error" in n for n in batch.provenance.notes)


def test_get_all_pod_logs_wraps_client_internally(monkeypatch):
    # Ensure tests/ root is on path for _readonly_spy (after src/ so adapters is unambiguous)
    _tests_root = str(Path(__file__).resolve().parents[1])
    if _tests_root not in sys.path:
        sys.path.append(_tests_root)
    from helpers import utils
    from _readonly_spy import make_spy

    record = []
    monkeypatch.setattr(utils, "ReadOnlyCoreV1", make_spy(record))
    raw = MagicMock()
    pod = MagicMock()
    pod.spec.containers = [MagicMock()]
    pod.spec.containers[0].name = "main"
    raw.read_namespaced_pod.return_value = pod
    raw.read_namespaced_pod_log.return_value = "line"
    asyncio.run(utils.get_all_pod_logs("pod", "ns", k8s_core_api=raw))
    assert "read_namespaced_pod" in record, record
    assert "read_namespaced_pod_log" in record, record
