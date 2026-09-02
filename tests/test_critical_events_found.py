"""
Tests for F-21: high_or_critical_events_found field in adaptive_namespace_investigation.

RED block: fail pre-fix (field absent; 'namespace appears healthy' shown despite
           HIGH events), pass post-fix.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

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
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("server_mcp_f21", SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_f21"] = mod
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
# Mock return values
# ---------------------------------------------------------------------------

# 3 HIGH-severity events — 3 entries fit inside the 5-entry compression cap,
# so compressed_events["critical_events"] also has 3 entries.  This isolates
# the counter correctness without needing >5 events.
_HIGH_EVENTS_RESULT = {
    "namespace": "test-ns",
    "total_events": 3,
    "processed_events": 3,
    "strategy_used": "smart_summary",
    "events": [
        {
            "severity": "HIGH",
            "category": "FAILURE",
            "event_string": "[t] Warning: BackOff - restarting (Object: Pod/p1)",
            "relevance_score": 1.0,
            "token_estimate": 30,
        },
        {
            "severity": "HIGH",
            "category": "FAILURE",
            "event_string": "[t] Warning: BackOff - restarting (Object: Pod/p2)",
            "relevance_score": 1.0,
            "token_estimate": 30,
        },
        {
            "severity": "HIGH",
            "category": "FAILURE",
            "event_string": "[t] Warning: BackOff - restarting (Object: Pod/p3)",
            "relevance_score": 1.0,
            "token_estimate": 30,
        },
    ],
    "recommendations": ["Continue monitoring - event patterns appear normal"],
    "summary": {
        "total_events": 3,
        "critical_events": 0,
        "high_severity_events": 3,
        "category_breakdown": {"FAILURE": 3},
        "severity_breakdown": {"HIGH": 3},
    },
}

# 8 HIGH-severity events — exceeds the 5-entry compression cap.
# _compress_events_for_synthesis truncates critical_events to sorted_events[:5],
# so counting from compressed_events would return 5, not 8.
# This fixture pins the anti-saturation property: the counter MUST read from
# events_result.get("events") (pre-compression) to report the correct 8.
_EIGHT_HIGH_EVENTS_RESULT = {
    "namespace": "test-ns",
    "total_events": 8,
    "processed_events": 8,
    "strategy_used": "smart_summary",
    "events": [
        {
            "severity": "HIGH",
            "category": "FAILURE",
            "event_string": f"[t] Warning: BackOff - restarting (Object: Pod/p{i})",
            "relevance_score": 1.0,
            "token_estimate": 30,
        }
        for i in range(8)
    ],
    "recommendations": ["Continue monitoring - event patterns appear normal"],
    "summary": {
        "total_events": 8,
        "critical_events": 0,
        "high_severity_events": 8,
        "category_breakdown": {"FAILURE": 8},
        "severity_breakdown": {"HIGH": 8},
    },
}

# Pod analysis that produces no critical_issues (no error patterns).
_CLEAN_POD_ANALYSIS = {
    "patterns": {"errors": []},
    "metadata": {
        "processing_metrics": {
            "estimated_tokens_used": 100,
            "total_log_lines": 0,
            "patterns_extracted": 0,
            "processing_time_seconds": 0.01,
        }
    },
}

_ONE_POD = [
    {"name": "pod-1", "status": "Running", "container_states": [], "restart_count": 0}
]

# 6 pods so that pods_analyzed > 5 (the threshold for 'namespace appears healthy').
_SIX_PODS = [
    {"name": f"pod-{i}", "status": "Running", "container_states": [], "restart_count": 0}
    for i in range(6)
]


# ============================================================================
# RED TESTS (pre-fix: FAIL; post-fix: PASS)
# ============================================================================


@pytest.mark.asyncio
async def test_high_or_critical_events_found_counts_high_events(server, monkeypatch):
    """investigation_summary.high_or_critical_events_found == 3 when events_result has 3 HIGH entries.

    Pre-fix: field absent (KeyError) or 0.
    """

    async def mock_events(*args, **kwargs):
        return _HIGH_EVENTS_RESULT

    async def mock_pods(namespace, **kwargs):
        return _ONE_POD

    async def mock_pod_logs(*args, **kwargs):
        return _CLEAN_POD_ANALYSIS

    monkeypatch.setattr(server, "smart_get_namespace_events", mock_events)
    monkeypatch.setattr(server, "list_pods_in_namespace", mock_pods)
    monkeypatch.setattr(server, "smart_summarize_pod_logs", mock_pod_logs)

    result = await server.adaptive_namespace_investigation(namespace="test-ns")
    summary = result.get("investigation_summary", {})
    assert "high_or_critical_events_found" in summary, (
        f"'high_or_critical_events_found' absent from investigation_summary; keys: {list(summary.keys())}"
    )
    assert summary["high_or_critical_events_found"] == 3, (
        f"Expected high_or_critical_events_found=3, got {summary['high_or_critical_events_found']}"
    )


@pytest.mark.asyncio
async def test_namespace_healthy_guard_blocked_by_high_events(server, monkeypatch):
    """With 3 HIGH events: 'namespace appears healthy' must NOT appear in recommendations.

    Setup: 6 pods analyzed (> 5), no pod-log errors (critical_issues=[]),
           but 3 HIGH events → event_critical_count > 0.
    Pre-fix: old guard `not critical_issues and pods_analyzed > 5` fires → message present.
    Post-fix: new guard also requires `event_critical_count == 0` → message absent.
    """

    async def mock_events(*args, **kwargs):
        return _HIGH_EVENTS_RESULT

    async def mock_pods(namespace, **kwargs):
        return _SIX_PODS

    async def mock_pod_logs(*args, **kwargs):
        return _CLEAN_POD_ANALYSIS

    monkeypatch.setattr(server, "smart_get_namespace_events", mock_events)
    monkeypatch.setattr(server, "list_pods_in_namespace", mock_pods)
    monkeypatch.setattr(server, "smart_summarize_pod_logs", mock_pod_logs)

    result = await server.adaptive_namespace_investigation(namespace="test-ns")
    recs = result.get("recommendations", [])
    assert not any("namespace appears healthy" in r for r in recs), (
        f"'namespace appears healthy' should be suppressed when HIGH events exist; "
        f"recommendations: {recs}"
    )


@pytest.mark.asyncio
async def test_high_or_critical_events_found_not_saturated_at_compression_cap(server, monkeypatch):
    """high_or_critical_events_found == 8 when events_result has 8 HIGH entries (above the 5-cap).

    _compress_events_for_synthesis truncates critical_events to sorted_events[:5].
    Counting from compressed_events.get("critical_events", []) would return 5, not 8.
    This test pins the anti-saturation property: the counter must read from
    events_result.get("events") (pre-compression) to report the correct total.

    Mutation verification (M-anti-sat): switching the counter to
        `compressed_events.get("critical_events", [])` makes this test RED
        (actual=5, expected=8).
    """

    async def mock_events(*args, **kwargs):
        return _EIGHT_HIGH_EVENTS_RESULT

    async def mock_pods(namespace, **kwargs):
        return _ONE_POD

    async def mock_pod_logs(*args, **kwargs):
        return _CLEAN_POD_ANALYSIS

    monkeypatch.setattr(server, "smart_get_namespace_events", mock_events)
    monkeypatch.setattr(server, "list_pods_in_namespace", mock_pods)
    monkeypatch.setattr(server, "smart_summarize_pod_logs", mock_pod_logs)

    result = await server.adaptive_namespace_investigation(namespace="test-ns")
    summary = result.get("investigation_summary", {})
    assert summary.get("high_or_critical_events_found") == 8, (
        f"Expected high_or_critical_events_found=8 (pre-compression count); "
        f"got {summary.get('high_or_critical_events_found')} — if 5, counter reads from "
        "compressed_events (capped at 5) instead of events_result.get('events')"
    )
