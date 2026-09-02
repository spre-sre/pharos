"""
Tests for F-11a: HIGH-volume risk factor in assess_overall_risk.

RED block: fail pre-fix (only CRITICAL>5 factor exists), pass post-fix.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from helpers.event_analysis import assess_overall_risk


def _make_analytics(high_count=0, critical_count=0):
    """Build minimal analytics_result for assess_overall_risk."""
    return {
        "base_analysis": {
            "detailed_analysis": {
                "severity_analysis": {
                    "HIGH": {"count": high_count},
                    "CRITICAL": {"count": critical_count},
                }
            }
        }
    }


# ============================================================================
# RED TESTS (pre-fix: FAIL; post-fix: PASS)
# ============================================================================


def test_high_events_10_raises_risk():
    """10 HIGH events → risk_score >= 0.25 (pre-fix: 0.0 — only CRITICAL>5 factor fires)."""
    result = assess_overall_risk(_make_analytics(high_count=10))
    assert result["risk_score"] >= 0.25, (
        f"Expected risk_score >= 0.25 for 10 HIGH events, got {result['risk_score']}"
    )


def test_high_events_25_raises_risk():
    """25 HIGH events → risk_score >= 0.4 (pre-fix: 0.0)."""
    result = assess_overall_risk(_make_analytics(high_count=25))
    assert result["risk_score"] >= 0.4, (
        f"Expected risk_score >= 0.4 for 25 HIGH events, got {result['risk_score']}"
    )
