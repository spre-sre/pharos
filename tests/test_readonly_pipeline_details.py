"""Behavioral proof: get_pipeline_details / get_task_details route their
CustomObjects reads through ReadOnlyK8sClient via param-local reassignment."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Insert order is load-bearing: src/ MUST end up at sys.path[0].  This file
# imports helpers.utils (guarded `from core.readonly_client import`) at
# collection time; if tests/ preceded src/, the tests/core/ package would
# shadow src/core and poison `import core` for _readonly_spy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import helpers.utils as hu
from _readonly_spy import make_spy


@pytest.mark.asyncio
async def test_get_pipeline_details_routes_readonly(monkeypatch):
    record = []
    monkeypatch.setattr(hu, "ReadOnlyK8sClient", make_spy(record))

    fake = MagicMock()
    fake.get_namespaced_custom_object.return_value = {
        "metadata": {}, "spec": {}, "status": {}}

    await hu.get_pipeline_details(
        "team-a", "plr-x", fake,
        AsyncMock(return_value=[]),   # list_taskruns_func
        MagicMock(return_value=None), # calculate_duration_func
        MagicMock(),                  # log
    )

    assert "get_namespaced_custom_object" in record, (
        f"get_pipeline_details read not routed through ReadOnlyK8sClient; "
        f"record={record}")


@pytest.mark.asyncio
async def test_get_task_details_routes_readonly(monkeypatch):
    record = []
    monkeypatch.setattr(hu, "ReadOnlyK8sClient", make_spy(record))

    fake = MagicMock()
    fake.get_namespaced_custom_object.return_value = {
        "metadata": {}, "spec": {}, "status": {}}

    await hu.get_task_details(
        "team-a", "tr-x", fake,
        MagicMock(return_value=None), # calculate_duration_func
        MagicMock(),                  # log
    )

    assert "get_namespaced_custom_object" in record, (
        f"get_task_details read not routed through ReadOnlyK8sClient; "
        f"record={record}")
