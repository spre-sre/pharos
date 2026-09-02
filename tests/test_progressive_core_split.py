"""
tests/test_progressive_core_split.py

Purpose-built correctness pins for C2 (F-12 core split).

Two tests — each guards one half of the fix:

M-C2a — cap bypass
  advanced_event_analytics reads full classified_events from the core, not the
  3-capped sample_events list that _extract_events_from_progressive returns.
  10 events in one category → pre-fix total_events_analyzed == 3 (cap), post-fix == 10.

M-C2b — datetime guard
  The core returns datetime OBJECTS in the 'timestamp' field. Without the
  isinstance guard, advanced_event_analytics calls fromisoformat(datetime_obj)
  which raises TypeError, is swallowed, and every timestamp becomes datetime.now().
  MLPatternDetector then sees all events as recent → escalation_risk == "HIGH",
  risk_score == 0.15.  With the guard, timestamps are preserved as 2-3h-ago
  datetimes → escalation_risk == "LOW", risk_score == 0.0.

Golden-neutral: the characterization fixture has 2 events (both FAILURE; 2 < cap
of 3) so total_events_analyzed stays 2 regardless of split.  These purpose-built
tests are the ONLY oracle for C2.
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Module import (same pattern as test_search_resources_by_labels.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"

_FAKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1"}
  name: fake
contexts:
- context: {cluster: fake, user: fake}
  name: fake
current-context: fake
users:
- name: fake
  user: {token: "fake-token"}
"""

# Import EVENT, FakeApi, items_list from k8s_fakes — the EVENT factory sets
# timestamps to 2-3 hours ago, which is what both tests rely on.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from characterization.k8s_fakes import EVENT, FakeApi, items_list  # noqa: E402


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once per module with a fake kubeconfig.

    Uses a distinct sys.modules key (server_mcp_coresplt) so this import
    coexists with session-scoped characterization fixtures without collision.
    """
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }

    kubeconfig = tmp_path_factory.mktemp("kube_coresplt") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_coresplt", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_coresplt"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    sys.modules.pop("server_mcp_coresplt", None)
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# 10 events, all BackOff → all classify into FAILURE category.
# _analyze_by_category caps sample_events at 3 per category, so
# _extract_events_from_progressive returns only 3.  Post-fix: the core
# returns the full classified_events list (10).
_EVENTS_10 = [
    EVENT(
        "BackOff",
        "Back-off restarting failed container",
        "team-a",
        name=f"pod-{i}",
        count=1,
    )
    for i in range(10)
]


# ---------------------------------------------------------------------------
# M-C2a: cap-bypass test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advanced_event_analytics_reads_all_events(server, monkeypatch):
    """M-C2a: advanced_event_analytics must see all 10 events, not the 3-cap.

    Pre-fix (_extract_events_from_progressive reads sample_events):
        total_events_analyzed == 3

    Post-fix (core returns full classified_events):
        total_events_analyzed == 10
    """
    fake_core = FakeApi(list_namespaced_event=items_list(_EVENTS_10))
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    result = await server.advanced_event_analytics(namespace="team-a")

    assert result.get("total_events_analyzed") == 10, (
        f"Expected 10, got {result.get('total_events_analyzed')} "
        "— pre-fix cap still active? (sample_events capped at 3 per category)"
    )


# ---------------------------------------------------------------------------
# M-C2b: datetime-guard test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advanced_event_analytics_uses_datetime_objects(server, monkeypatch):
    """M-C2b: classified_events carry datetime objects; fromisoformat must be
    guarded with isinstance(ts_raw, datetime) before the call.

    Without the guard: fromisoformat(datetime_obj) raises TypeError, is swallowed
    by the except block, every timestamp becomes datetime.now(), ML scoring sees
    all events as a current burst → escalation_risk == "HIGH", risk_score == 0.15.

    With the guard: datetime objects pass through unmodified (2-3h ago timestamps
    from EVENT()), ML scoring sees old events → escalation_risk == "LOW",
    risk_score == 0.0.

    No time mocking.  Do NOT assert on generated_at (it is datetime.now()
    unconditionally — freezing time makes the assertion vacuously true in both
    states).  Assert on ml_patterns (NOT ml_pattern_analysis) and risk_assessment.
    """
    fake_core = FakeApi(list_namespaced_event=items_list(_EVENTS_10))
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    result = await server.advanced_event_analytics(namespace="team-a")

    assert "ml_patterns" in result, (
        f"Expected 'ml_patterns' key in result; got keys: {list(result.keys())}"
    )
    pred = result["ml_patterns"].get("predictive_indicators", {})
    assert pred.get("escalation_risk") == "LOW", (
        f"Expected escalation_risk='LOW' (events 2-3h old); "
        f"got {pred.get('escalation_risk')!r} — isinstance guard absent? "
        "(fromisoformat on datetime obj raises TypeError → datetime.now() → HIGH)"
    )
    # C4 adds +0.25 for 10 HIGH events (HIGH-volume factor).  With the datetime guard
    # working, escalation_risk=LOW (ML adds 0), so total == 0.25.  Without the guard,
    # escalation_risk=HIGH (ML adds 0.15) giving 0.40 — the assertion still catches it.
    assert result["risk_assessment"]["risk_score"] == 0.25, (
        f"Expected risk_score=0.25 (HIGH-vol factor only); "
        f"got {result['risk_assessment']['risk_score']} "
        "— if 0.40, datetime guard is absent (escalation_risk=HIGH adds 0.15)"
    )
