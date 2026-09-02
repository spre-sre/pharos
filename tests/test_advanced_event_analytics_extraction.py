"""
Tests for _extract_events_from_progressive() in server-mcp.py.

Bug: advanced_event_analytics called progressive_event_analysis with
depth="comprehensive" → level="detailed", whose result carries the key
"detailed_analysis" not "overview".  The extraction gate was:

    if "overview" in base_result:

...which is always False for "detailed" level, so events_data stayed
empty and total_events_analyzed was always 0 even when 3417 events existed.

A second issue: even the "overview" branch read
    base_result["overview"].get("events", [])
but get_overview() does not return an "events" key — it returns
"critical_events_preview" and "recent_high_impact".

Fix: _extract_events_from_progressive() is level-aware and reads events
from whichever key the progressive result actually carries.

These tests feed synthetic progressive-result dicts directly into the
helper and assert the correct extraction.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import server-mcp module using the same isolation pattern as the
# characterization conftest (fake kubeconfig + KUBEARCHIVE_ENABLED=false).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

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


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig."""
    kubeconfig = tmp_path_factory.mktemp("kube") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    # Phase 2a: server-mcp runs load_config() at import; pin LUMINO_* (see characterization/conftest.py).
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("server_mcp_evt", SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_evt"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Synthetic base-result fixtures
# ---------------------------------------------------------------------------

_DETAILED_RESULT = {
    "namespace": "team-a",
    "analysis_level": "detailed",
    "total_events": 2,
    "detailed_analysis": {
        "detailed_level": "comprehensive_analysis",
        "total_analyzed": 2,
        "category_analysis": {
            "FAILURE": {
                "count": 2,
                "severity_breakdown": {"HIGH": 2},
                "sample_events": [
                    {
                        "event_string": "Back-off restarting failed container",
                        "severity": "HIGH",
                        "timestamp": "2026-07-23T10:00:00",
                    },
                    {
                        "event_string": "0/3 nodes are available for scheduling",
                        "severity": "HIGH",
                        "timestamp": "2026-07-23T10:01:00",
                    },
                ],
            }
        },
        "severity_analysis": {
            "HIGH": {
                "count": 2,
                "percentage": 100.0,
                "categories": {"FAILURE": 2},
                "sample_events": [],
            }
        },
    },
}

_OVERVIEW_RESULT = {
    "namespace": "team-a",
    "analysis_level": "overview",
    "total_events": 2,
    "overview": {
        "overview_level": "high_level_summary",
        "critical_events_preview": [],
        "recent_high_impact": [
            {
                "severity": "HIGH",
                "category": "FAILURE",
                "preview": "Back-off restarting failed container...",
                "timestamp": "2026-07-23T10:00:00",
            },
            {
                "severity": "HIGH",
                "category": "FAILURE",
                "preview": "0/3 nodes are available...",
                "timestamp": "2026-07-23T10:01:00",
            },
        ],
        "quick_patterns": {},
        "drill_down_suggestions": [],
    },
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExtractEventsFromProgressive:
    """_extract_events_from_progressive must return non-empty event lists
    for both 'detailed' and 'overview' level progressive results."""

    def test_detailed_level_returns_events(self, server):
        fn = server._extract_events_from_progressive
        events = fn(_DETAILED_RESULT)
        assert len(events) == 2, (
            f"Expected 2 events from detailed level, got {len(events)}"
        )

    def test_detailed_level_preserves_severity(self, server):
        fn = server._extract_events_from_progressive
        events = fn(_DETAILED_RESULT)
        severities = {e["severity"] for e in events}
        assert "HIGH" in severities, f"Severity HIGH not found in {severities}"

    def test_detailed_level_preserves_category(self, server):
        fn = server._extract_events_from_progressive
        events = fn(_DETAILED_RESULT)
        categories = {e["category"] for e in events}
        assert "FAILURE" in categories, f"Category FAILURE not found in {categories}"

    def test_detailed_level_has_event_string(self, server):
        fn = server._extract_events_from_progressive
        events = fn(_DETAILED_RESULT)
        for ev in events:
            assert ev.get("event_string"), "event_string must be non-empty"

    def test_overview_level_returns_events(self, server):
        fn = server._extract_events_from_progressive
        events = fn(_OVERVIEW_RESULT)
        assert len(events) > 0, "Expected events from overview level, got 0"

    def test_overview_level_preserves_severity(self, server):
        fn = server._extract_events_from_progressive
        events = fn(_OVERVIEW_RESULT)
        severities = {e["severity"] for e in events}
        assert "HIGH" in severities, f"Severity HIGH not found in {severities}"

    def test_overview_level_no_duplicates(self, server):
        """recent_high_impact has 2 events with distinct timestamps — no dedup needed."""
        fn = server._extract_events_from_progressive
        events = fn(_OVERVIEW_RESULT)
        # Should not return more than the source has
        assert len(events) <= 2

    def test_empty_result_returns_empty_list(self, server):
        fn = server._extract_events_from_progressive
        events = fn({"namespace": "x", "analysis_level": "overview"})
        assert events == []
