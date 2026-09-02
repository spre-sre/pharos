"""
Tests for F-11b: UNCHARACTERISED signal in the three recommendation generators.

RED block: fail pre-fix (generators assert normality when no rule fires),
           pass post-fix.
Regression block: always GREEN — zero HIGH/CRITICAL → old normality message.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from helpers.event_analysis import (
    ProgressiveEventAnalyzer,
    generate_string_events_recommendations,
    generate_strategic_recommendations,
)


def _make_high_events(n=2, category="FAILURE"):
    """Build n HIGH-severity events that won't trip category thresholds (count < 3)."""
    return [
        {
            "severity": "HIGH",
            "category": category,
            "event_string": f"[t] Warning: Event{i} (Object: Pod/p{i})",
        }
        for i in range(n)
    ]


def _make_analytics_with_high(high_count=2):
    """Build an analytics_result dict with high_count HIGH events and no other signals."""
    return {
        "base_analysis": {
            "detailed_analysis": {
                "severity_analysis": {
                    "HIGH": {"count": high_count},
                    "CRITICAL": {"count": 0},
                }
            }
        },
        "risk_assessment": {"overall_risk_level": "LOW"},
        "ml_patterns": {},
        "runbook_suggestions": [],
        "log_correlation": {},
        "metrics_correlation": {},
    }


# ============================================================================
# RED TESTS (pre-fix: FAIL; post-fix: PASS)
# ============================================================================


def test_detailed_recommendations_uncharacterised():
    """Generator 1: 2 HIGH events, no rule fires → UNCHARACTERISED in recommendations[0].

    Pre-fix: recommendations == ['MONITORING: Events are within normal parameters ...']
    """
    events = _make_high_events(2)
    analyzer = ProgressiveEventAnalyzer(events)
    recs = analyzer._generate_detailed_recommendations(events)
    assert recs, "Expected non-empty recommendations"
    assert "UNCHARACTERISED" in recs[0], (
        f"Expected UNCHARACTERISED in first recommendation, got: {recs[0]!r}"
    )


def test_string_events_uncharacterised():
    """Generator 2: 2 HIGH events (FAILURE, count < threshold 3) → UNCHARACTERISED in result[0].

    Pre-fix: result == ['Continue monitoring - event patterns appear normal']
    """
    events = _make_high_events(2)
    result = generate_string_events_recommendations(events)
    assert result, "Expected non-empty result"
    assert "UNCHARACTERISED" in result[0], (
        f"Expected UNCHARACTERISED in first recommendation, got: {result[0]!r}"
    )


def test_strategic_recommendations_uncharacterised():
    """Generator 3: analytics_result with 2 HIGH events, no other signals → UNCHARACTERISED tail.

    Pre-fix: result[-1] == 'Continue monitoring - no immediate action required ...'
    """
    analytics_result = _make_analytics_with_high(high_count=2)
    result = generate_strategic_recommendations(analytics_result)
    assert result, "Expected non-empty result"
    assert "UNCHARACTERISED" in result[-1], (
        f"Expected UNCHARACTERISED in tail recommendation, got: {result[-1]!r}"
    )


# ============================================================================
# REGRESSION TESTS (must stay GREEN pre- and post-fix)
# ============================================================================


def test_detailed_recommendations_normality_when_no_hc():
    """Generator 1: zero HIGH/CRITICAL events → old normality message (no UNCHARACTERISED).

    Two LOW-severity events ensure the function reaches the modified tail (the empty-list
    early return at line 674 would bypass it, making this test vacuously true for any
    implementation).  With LOW events: critical_count=0, high_count=0, no category
    threshold fires → hc_count=0 → else branch → MONITORING string.
    """
    events = [
        {"severity": "LOW", "category": "OTHER", "event_string": "[t] Normal: info msg 1"},
        {"severity": "LOW", "category": "OTHER", "event_string": "[t] Normal: info msg 2"},
    ]
    analyzer = ProgressiveEventAnalyzer(events)
    recs = analyzer._generate_detailed_recommendations(events)
    assert recs, "Expected non-empty recommendations"
    assert "UNCHARACTERISED" not in recs[0], (
        f"Expected MONITORING normality message when no HIGH/CRITICAL events, got: {recs[0]!r}"
    )


def test_string_events_normality_when_no_hc():
    """Generator 2: zero HIGH/CRITICAL events, no category threshold fires → old normality message."""
    # One LOW-severity event — no HIGH/CRITICAL, no category count >= 3
    events = [{"severity": "LOW", "category": "OTHER", "event_string": "[t] Normal: routine"}]
    result = generate_string_events_recommendations(events)
    assert result, "Expected non-empty result"
    assert "UNCHARACTERISED" not in result[0], (
        f"Expected normality message when no HIGH/CRITICAL events, got: {result[0]!r}"
    )


def test_strategic_recommendations_normality_when_no_hc():
    """Generator 3: zero HIGH/CRITICAL events → old normality message (no UNCHARACTERISED)."""
    analytics_result = _make_analytics_with_high(high_count=0)
    result = generate_strategic_recommendations(analytics_result)
    assert result, "Expected non-empty result"
    assert "UNCHARACTERISED" not in result[-1], (
        f"Expected normality message when no HIGH/CRITICAL events, got: {result[-1]!r}"
    )
