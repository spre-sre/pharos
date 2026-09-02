"""Step 2 RED — F-16b: assess_failure_severity must accept and use related_incidents.

Pre-fix: function signature has no `related_incidents` parameter → TypeError on call.
Post-fix: `related_incidents: Optional[List] = None` added; blast-radius factor:
  severity_score += min(2, len(related_incidents)) when related_incidents is truthy.

The `with` call must yield a higher severity_score than the `without` call.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.failure_analysis import assess_failure_severity


def test_severity_blast_radius_increases_score():
    """F-16b RED: related_incidents must increase severity_score (blast-radius factor).

    Pre-fix: `assess_failure_severity` does not accept `related_incidents` → TypeError.
    Post-fix: blast-radius factor adds min(2, blast_count) to severity_score.
    """
    primary = {"basic_analysis": {"failed_tasks": []}}
    root_cause = {"root_cause_analysis": {"primary_cause": {}}}
    resource = {}
    config = []
    related = [{"id": "inc-1"}, {"id": "inc-2"}]

    result_with = assess_failure_severity(
        primary, root_cause, resource, config, related_incidents=related
    )
    result_without = assess_failure_severity(primary, root_cause, resource, config)

    assert result_with["severity_score"] > result_without["severity_score"], (
        f"related_incidents should increase severity_score: "
        f"with={result_with['severity_score']}, without={result_without['severity_score']}"
    )


def test_blast_radius_capped_at_two():
    """F-16b: blast-radius factor is capped at 2 (min(2, blast_count))."""
    primary = {"basic_analysis": {"failed_tasks": []}}
    root_cause = {"root_cause_analysis": {"primary_cause": {}}}
    resource = {}
    config = []

    score_1 = assess_failure_severity(
        primary, root_cause, resource, config,
        related_incidents=[{"id": "inc-1"}]
    )["severity_score"]
    score_5 = assess_failure_severity(
        primary, root_cause, resource, config,
        related_incidents=[{"id": f"inc-{i}"} for i in range(5)]
    )["severity_score"]
    base = assess_failure_severity(primary, root_cause, resource, config)["severity_score"]

    assert score_1 == base + 1, (
        f"1 related incident should add exactly 1 to score: "
        f"base={base}, score_1={score_1}"
    )
    assert score_5 == base + 2, (
        f"5 related incidents should add exactly 2 (cap) to score: "
        f"base={base}, score_5={score_5}"
    )
