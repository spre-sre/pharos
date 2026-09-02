"""Task 3: cost impact follows the direction AND magnitude of the change."""
import asyncio
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.utils import run_monte_carlo_simulation  # noqa: E402


def _run(changes):
    random.seed(0)
    return asyncio.run(run_monte_carlo_simulation(
        models={}, changes=changes, scenario_type="scaling",
        duration="1h", risk_tolerance="moderate"))


def test_scale_down_projects_cost_savings():
    stats = _run({"replicas": {"before": 107, "after": 80}})
    assert stats["cost_impact"]["mean"] < 0


def test_cost_scales_with_actual_ratio_not_floor():
    """107→80 is a 25.2% reduction; base cost for scaling is 0.3.
    Expected mean ≈ -0.3 * 0.252 ≈ -0.076 — NOT the old floor-driven -0.30."""
    stats = _run({"replicas": {"before": 107, "after": 80}})
    assert -0.12 < stats["cost_impact"]["mean"] < -0.04


def test_scale_up_projects_cost_increase_scaled():
    """12→18 is +50%; expected mean ≈ 0.3 * 0.5 = 0.15."""
    stats = _run({"replicas": {"before": 12, "after": 18}})
    assert 0.10 < stats["cost_impact"]["mean"] < 0.20


def test_perf_and_reliability_stay_positive_for_scale_down():
    stats = _run({"replicas": {"before": 107, "after": 80}})
    assert stats["performance_impact"]["mean"] > 0
    assert stats["reliability_impact"]["mean"] > 0


def test_no_before_after_keeps_legacy_magnitude():
    """No parseable before/after → legacy magnitude 1.0 → cost ≈ base 0.3."""
    stats = _run({"replicas": 5})
    assert 0.25 < stats["cost_impact"]["mean"] < 0.35


def test_no_change_yields_zero_impact():
    """before==after (ratio 0) is a PARSED no-op, not an unparseable change —
    it must not fall back to the legacy 1.0 floor (review finding #1)."""
    stats = _run({"replicas": {"before": 5, "after": 5}})
    assert stats["cost_impact"]["mean"] == 0.0
    assert stats["performance_impact"]["mean"] == 0.0


def test_equal_magnitude_tie_prefers_cost_increase():
    """Two changes with equal |ratio| but opposite sign must resolve the same
    way regardless of dict insertion order (review finding #2)."""
    stats_a_first = _run({
        "a": {"before": 10, "after": 15},
        "b": {"before": 10, "after": 5},
    })
    stats_b_first = _run({
        "b": {"before": 10, "after": 5},
        "a": {"before": 10, "after": 15},
    })
    assert stats_a_first["cost_impact"]["mean"] > 0
    assert stats_b_first["cost_impact"]["mean"] > 0
    assert stats_a_first["cost_impact"]["mean"] == pytest.approx(
        stats_b_first["cost_impact"]["mean"]
    )
