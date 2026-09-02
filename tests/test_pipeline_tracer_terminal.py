"""F-38 regression tests: cancelled/timed-out pipeline runs must NOT report in_progress.

The parametrized RED tests cover all 7 terminal-failure reasons that pre-fix fell
through to "in_progress". The agreement test asserts that derive_overall_status
and summarize_stages always agree, even for "Running" (which should be in_progress
in both).
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))

from extensions.konflux.lineage import (  # noqa: E402
    _TERMINAL_OK,
    derive_overall_status,
    summarize_stages,
)

TERMINAL_FAILURE_REASONS = [
    "PipelineRunCancelled",
    "Cancelled",
    "PipelineRunTimeout",
    "CouldntGetPipeline",
    "CreateRunFailed",
    "Unknown",
    "",
]


@pytest.mark.parametrize("reason", TERMINAL_FAILURE_REASONS)
def test_derive_overall_status_reports_failed(reason):
    """All terminal-failure reasons must yield 'failed', not 'in_progress'."""
    result = derive_overall_status(
        build_flow=[{"status": reason}],
        release_plr_flow=[],
        releases=[],
    )
    assert result == "failed", (
        f"derive_overall_status returned {result!r} for {reason!r} — "
        "expected 'failed' (pre-fix: this test should be RED)"
    )


@pytest.mark.parametrize("reason", TERMINAL_FAILURE_REASONS)
def test_summarize_stages_reports_failed(reason):
    """summarize_stages build status must agree with derive_overall_status."""
    stage = summarize_stages(
        build_flow=[{"status": reason}],
        release_plr_flow=[],
        lifecycle={},
    )
    assert stage["build"]["status"] == "failed", (
        f"summarize_stages returned {stage['build']['status']!r} for {reason!r} — "
        "expected 'failed'"
    )


@pytest.mark.parametrize("reason", [
    "PipelineRunCancelled", "Cancelled", "PipelineRunTimeout",
    "CouldntGetPipeline", "CreateRunFailed", "Unknown", "", "Running",
    "Started", "PipelineRunPending",
])
def test_derive_and_summarize_agree(reason):
    """derive_overall_status and summarize_stages['build']['status'] must agree."""
    overall = derive_overall_status([{"status": reason}], [], [])
    stage = summarize_stages([{"status": reason}], [], {})
    assert stage["build"]["status"] == overall, (
        f"Agreement broken for {reason!r}: derive={overall!r}, stage={stage['build']['status']!r}"
    )


@pytest.mark.parametrize("status", ["Running", "Started", "PipelineRunPending"])
def test_in_flight_status_is_in_progress(status):
    """Every member of _IN_FLIGHT must yield 'in_progress' from both functions.

    An agreement-only check would pass if both functions returned 'failed' — this
    test pins the value so that removing a status from _IN_FLIGHT goes RED.
    """
    overall = derive_overall_status([{"status": status}], [], [])
    stage = summarize_stages([{"status": status}], [], {})
    assert overall == "in_progress", (
        f"derive_overall_status returned {overall!r} for in-flight {status!r} — "
        "expected 'in_progress'"
    )
    assert stage["build"]["status"] == "in_progress", (
        f"summarize_stages returned {stage['build']['status']!r} for in-flight {status!r} — "
        "expected 'in_progress'"
    )
