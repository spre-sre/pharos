"""Step 4 RED — F-28: get_resource_metrics must NOT return hardcoded placeholder values.

Pre-fix: `get_resource_metrics` returns literal `"cpu_usage": "0.1"` / `"memory_usage": "64Mi"`
under a comment saying "This would integrate with Prometheus in a real implementation".
`status` was also "running" — also fabricated (we queried nothing).

Post-fix: returns `cpu_usage=None`, `memory_usage=None`, `status=None`, `data_source="unavailable"`.
All three presence-asserted (must be present and None, not merely absent).
"""
import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import logging
from helpers.resource_topology import get_resource_metrics


def test_get_resource_metrics_no_placeholder_cpu():
    """F-28 RED: cpu_usage must be present and None (not a hardcoded '0.1' string).

    Pre-fix: returns cpu_usage='0.1' (placeholder).
    Post-fix: returns cpu_usage=None (honest: Prometheus not integrated).
    Presence-asserted so the test catches both wrong-value AND absent-key regressions.
    """
    result = asyncio.run(
        get_resource_metrics("cluster", "deployment", "ns", "name", logging.getLogger())
    )
    assert "cpu_usage" in result, (
        f"cpu_usage key must be present in result — keys: {sorted(result)}"
    )
    assert result["cpu_usage"] is None, (
        f"cpu_usage must be None (not a hardcoded string), got {result['cpu_usage']!r} — "
        "pre-fix: returns '0.1' placeholder"
    )


def test_get_resource_metrics_no_placeholder_memory():
    """F-28 RED: memory_usage must be present and None (not a hardcoded '64Mi' string).

    Presence-asserted so the test catches both wrong-value AND absent-key regressions.
    """
    result = asyncio.run(
        get_resource_metrics("cluster", "deployment", "ns", "name", logging.getLogger())
    )
    assert "memory_usage" in result, (
        f"memory_usage key must be present in result — keys: {sorted(result)}"
    )
    assert result["memory_usage"] is None, (
        f"memory_usage must be None (not a hardcoded string), got {result['memory_usage']!r} — "
        "pre-fix: returns '64Mi' placeholder"
    )


def test_get_resource_metrics_no_placeholder_status():
    """F-28 extended: status must be None — we queried nothing, cannot know the resource is running.

    Pre-fix: returns status='running' alongside data_source='unavailable' — contradictory.
    Post-fix: status=None, consistent with the honest-unavailability declaration.
    """
    result = asyncio.run(
        get_resource_metrics("cluster", "deployment", "ns", "name", logging.getLogger())
    )
    assert "status" in result, "status key must be present"
    assert result["status"] is None, (
        f"status must be None (not a fabricated sentinel), got {result['status']!r} — "
        "pre-fix: returns 'running' without having queried anything"
    )


def test_get_resource_metrics_data_source_unavailable():
    """F-28 RED: data_source must be 'unavailable' to signal Prometheus not integrated."""
    result = asyncio.run(
        get_resource_metrics("cluster", "deployment", "ns", "name", logging.getLogger())
    )
    assert result.get("data_source") == "unavailable", (
        f"data_source must be 'unavailable', got {result.get('data_source')!r} — "
        "pre-fix: no data_source key returned"
    )
