"""Behavioral proof of spec SS4.7 routing: get_pod_logs forwards a
ReadOnlyCoreV1, not the raw client. A regression that drops the wrap fails
here even though every golden stays green (the wrapper is transparent)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from core.readonly_client import ReadOnlyCoreV1


@pytest.mark.asyncio
async def test_get_pod_logs_forwards_readonly_client(server, monkeypatch):
    captured = {}

    async def fake_gapl(pod_name, namespace, k8s_core_api=None, **kw):
        captured["client_type"] = type(k8s_core_api)
        return {"main": "x"}

    # get_all_pod_logs is imported into server-mcp.py from helpers
    # (line ~45: from helpers import ... get_all_pod_logs ...),
    # so patching server.get_all_pod_logs intercepts the bare-name call.
    monkeypatch.setattr(server, "get_all_pod_logs", fake_gapl)
    monkeypatch.setattr(server, "k8s_core_api", MagicMock())
    result = await server.get_pod_logs("ns", "pod")
    assert captured["client_type"] is ReadOnlyCoreV1
    assert "logs" in result and result["logs"] == {"main": "x"}
