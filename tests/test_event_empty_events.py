"""RED test for F-23(iii): ProgressiveEventAnalyzer._generate_detailed_recommendations
empty-events defensive fix.

Step 3 of D2.

# TOOL-UNREACHABLE: ProgressiveEventAnalyzer is constructed only at
# server-mcp.py:8856 inside _progressive_event_analysis_core, which
# early-returns on empty classified_events before reaching the constructor.
# This test targets the defensive fix directly at the class method level.
# It is a unit test, not an integration test.

Pre-fix:  FAILS — "_generate_detailed_recommendations([])" returns
          ["No events to analyze - system appears stable"], which contains
          the forbidden 'appears stable' sentinel.
Post-fix: PASSES — sentinel replaced with a coverage-aware message that does
          not claim system stability when no events were inspected.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Ensure helpers are importable without loading the full server module.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.event_analysis import ProgressiveEventAnalyzer  # noqa: E402


# ---------------------------------------------------------------------------
# RED test — F-23(iii)  (TOOL-UNREACHABLE: defensive unit test)
# ---------------------------------------------------------------------------


def test_empty_events_sentinel_is_not_appears_stable():
    """_generate_detailed_recommendations([]) must not claim system stability.

    Pre-fix: returns ["No events to analyze - system appears stable"].
    Post-fix: returns a message that omits 'appears stable'.

    NOTE: this path is TOOL-UNREACHABLE through any registered MCP tool —
    the early-return guard at event_analysis.py:120 fires before this method
    is ever called with an empty list in production. The fix is defensive.
    """
    result = ProgressiveEventAnalyzer([])._generate_detailed_recommendations([])
    assert not any("appears stable" in r for r in result), (
        f"'appears stable' must not appear in empty-events return: {result!r}"
    )
    assert any("investigation coverage: none" in r for r in result), (
        f"Expected coverage-aware message in empty-events return: {result!r}"
    )
