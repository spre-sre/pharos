"""RED test for F-23(ii): generate_streaming_recommendations zero-lines-processed gate.

Step 2 of D2.

Pre-fix:  FAILS — "No critical patterns detected. System appears stable." fires
          even when total_lines_analyzed == 0.
Post-fix: PASSES — sentinel suppressed when 0 lines processed; sentinel DOES fire
          for the honest case (total_lines_analyzed > 0, total_issues == 0).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Ensure helpers are importable without loading the full server module.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.log_analysis import generate_streaming_recommendations  # noqa: E402


# ---------------------------------------------------------------------------
# RED test — F-23(ii)
# ---------------------------------------------------------------------------


def test_stable_sentinel_suppressed_when_zero_lines_processed():
    """'appears stable' must NOT fire when total_lines_analyzed == 0.

    Pre-fix: sentinel fires unconditionally when total_issues == 0.
    Post-fix: sentinel gated on total_lines > 0.
    """
    recs = generate_streaming_recommendations(
        {"total_issues_found": 0, "total_lines_analyzed": 0, "most_common_errors": {}},
        {"trending_up": {}},
    )
    assert not any("appears stable" in r for r in recs), (
        f"Sentinel 'appears stable' must not fire when 0 lines processed, got {recs!r}"
    )


def test_stable_sentinel_fires_for_zero_issues_with_lines_processed():
    """'appears stable' MUST fire when total_lines_analyzed > 0 and total_issues == 0.

    This is the honest case: we actually read the logs and found nothing wrong.
    Regression guard — this must stay green after the guard is added.
    """
    recs_with_lines = generate_streaming_recommendations(
        {"total_issues_found": 0, "total_lines_analyzed": 50, "most_common_errors": {}},
        {"trending_up": {}},
    )
    assert any("stable" in r.lower() for r in recs_with_lines), (
        "Sentinel must fire for 0 issues in 50 processed lines (the honest case); "
        f"got {recs_with_lines!r}"
    )
