"""Step 1 RED — F-16a: calculate_confidence_score must NOT grant +0.2 for unknown category.

Pre-fix: `if root_cause_data["root_cause_analysis"]["primary_cause"]:` is truthy for any
non-empty dict (including one with category="unknown") → grants +0.2 → score=0.7.

Post-fix: gate checks `_pc.get("category") not in (None, "", "unknown")` → no +0.2 for
unknown/blank/None category → score stays at 0.5 (base only).

Confidence MUST be < 0.7 for category in (None, "", "unknown").
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.failure_analysis import calculate_confidence_score


def test_unknown_category_no_confidence_boost():
    """F-16a RED: category='unknown' must not grant +0.2 confidence boost.

    Pre-fix: truthy primary_cause dict grants +0.2 → 0.7.
    Post-fix: category='unknown' excluded → 0.5 base only.
    """
    root_cause_unknown = {
        "root_cause_analysis": {
            "primary_cause": {"category": "unknown", "description": "unclassified error"}
        }
    }
    primary = {"logs_analyzed": False}
    timeline = []
    score = calculate_confidence_score(primary, root_cause_unknown, timeline)
    assert score < 0.7, (
        f"Confidence must be < 0.7 for unknown category, got {score} — "
        "pre-fix: returns 0.7 (base 0.5 + 0.2 for truthy primary_cause)"
    )


def test_bad_categories_no_confidence_boost():
    """F-16a RED: categories None, '', and 'unknown' must all be rejected.

    Pre-fix: all three return 0.7 (truthy dict grants +0.2).
    Post-fix: all three return 0.5 (gate excludes them).
    """
    for bad_cat in ("", None, "unknown"):
        rc = {"root_cause_analysis": {"primary_cause": {"category": bad_cat}}}
        s = calculate_confidence_score({"logs_analyzed": False}, rc, [])
        assert s < 0.7, (
            f"Confidence must be < 0.7 for category={bad_cat!r}, got {s} — "
            "pre-fix: truthy dict grants +0.2 regardless of category value"
        )
