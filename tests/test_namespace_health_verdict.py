"""Namespace health must not be reported from pods that were never analyzed.

adaptive_namespace_investigation and conservative_namespace_overview counted a
pod as analyzed when smart_summarize_pod_logs returned {"error": ...} (for
example pods/log forbidden), so a namespace where nothing could be read was
reported as "No critical issues detected ... namespace appears healthy" with
coverage verdict "complete".
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

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
    spec = importlib.util.spec_from_file_location("server_mcp_health_verdict", SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_health_verdict"] = mod
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


_PODS = [
    {"name": f"pod-{i}", "status": "Running", "container_states": [], "restart_count": 0}
    for i in range(8)
]

_QUIET_EVENTS = {
    "namespace": "team-a",
    "total_events": 0,
    "processed_events": 0,
    "strategy_used": "smart_summary",
    "events": [],
}

_CLEAN_ANALYSIS = {
    "patterns": {"errors": [], "warnings": []},
    "metadata": {"processing_metrics": {"estimated_tokens_used": 100,
                                        "total_log_lines": 10, "patterns_extracted": 0}},
}

_FORBIDDEN = {"error": "Failed to retrieve logs: (403) Reason: Forbidden: "
                       "pods \"pod-0\" is forbidden: cannot get resource \"pods/log\""}


def _patch(server, monkeypatch, *, analysis, events=_QUIET_EVENTS, pods=_PODS):
    async def mock_pods(namespace, **kwargs):
        return pods

    async def mock_events(*args, **kwargs):
        return events

    async def mock_pod_logs(*args, pod_name, **kwargs):
        result = analysis(pod_name) if callable(analysis) else analysis
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(server, "list_pods_in_namespace", mock_pods)
    monkeypatch.setattr(server, "smart_get_namespace_events", mock_events)
    monkeypatch.setattr(server, "smart_summarize_pod_logs", mock_pod_logs)


def _says_healthy(recommendations):
    return any("healthy" in r or "No critical issues" in r for r in recommendations)


# ── adaptive_namespace_investigation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_adaptive_all_pod_logs_forbidden_is_not_healthy(server, monkeypatch):
    _patch(server, monkeypatch, analysis=_FORBIDDEN)

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    summary = result["investigation_summary"]
    assert summary["pods_analyzed"] == 0
    assert summary["pods_failed"] == 8
    assert not _says_healthy(result["recommendations"]), result["recommendations"]
    assert any("could not be analyzed" in r for r in result["recommendations"]), result["recommendations"]
    coverage = result["adaptive_metadata"]["coverage"]
    assert coverage["scanned"] == 0
    assert coverage["denied"] == 8
    assert coverage["verdict"] == "none"
    # The reason is kept per pod, not dropped.
    assert "Forbidden" in result["pod_findings"]["pod-0"]["error"]


@pytest.mark.asyncio
async def test_adaptive_partial_failure_is_partial_and_not_healthy(server, monkeypatch):
    _patch(server, monkeypatch,
           analysis=lambda pod: _FORBIDDEN if pod == "pod-7" else _CLEAN_ANALYSIS)

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    summary = result["investigation_summary"]
    assert summary["pods_analyzed"] == 7
    assert summary["pods_failed"] == 1
    assert not _says_healthy(result["recommendations"]), result["recommendations"]
    coverage = result["adaptive_metadata"]["coverage"]
    assert (coverage["scanned"], coverage["denied"], coverage["verdict"]) == (7, 1, "partial")


@pytest.mark.asyncio
async def test_adaptive_exception_counts_as_skipped_not_analyzed(server, monkeypatch):
    _patch(server, monkeypatch,
           analysis=lambda pod: RuntimeError("boom") if pod == "pod-3" else _CLEAN_ANALYSIS)

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    coverage = result["adaptive_metadata"]["coverage"]
    assert (coverage["scanned"], coverage["skipped"], coverage["verdict"]) == (7, 1, "partial")
    assert result["pod_findings"]["pod-3"]["error"] == "boom"
    assert not _says_healthy(result["recommendations"])


@pytest.mark.asyncio
async def test_adaptive_events_failure_is_not_healthy(server, monkeypatch):
    _patch(server, monkeypatch, analysis=_CLEAN_ANALYSIS,
           events={"error": "Failed to fetch events: (403) Forbidden"})

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    assert not _says_healthy(result["recommendations"]), result["recommendations"]
    assert any("events could not be read" in r.lower() for r in result["recommendations"])


@pytest.mark.asyncio
async def test_adaptive_all_clean_still_reports_healthy(server, monkeypatch):
    _patch(server, monkeypatch, analysis=_CLEAN_ANALYSIS)

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    assert result["investigation_summary"]["pods_failed"] == 0
    assert any("namespace appears healthy" in r for r in result["recommendations"])
    assert result["adaptive_metadata"]["coverage"]["verdict"] == "complete"


# ── conservative_namespace_overview ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_conservative_all_pod_logs_forbidden_is_not_healthy(server, monkeypatch):
    _patch(server, monkeypatch, analysis=_FORBIDDEN)

    result = await server.conservative_namespace_overview(namespace="team-a")

    overview = result["overview"]
    assert overview["pods_analyzed"] == 0
    assert overview["pods_failed"] == 8
    assert not _says_healthy(result["recommendations"]), result["recommendations"]
    assert any("could not be analyzed" in r for r in result["recommendations"])
    assert result["conservative_metadata"]["coverage_ratio"] == "0/8"
    assert "Forbidden" in result["pod_findings"]["pod-0"]["error"]


@pytest.mark.asyncio
async def test_conservative_exception_not_counted_as_analyzed(server, monkeypatch):
    _patch(server, monkeypatch,
           analysis=lambda pod: RuntimeError("boom") if pod == "pod-0" else _CLEAN_ANALYSIS)

    result = await server.conservative_namespace_overview(namespace="team-a")

    assert result["overview"]["pods_analyzed"] == 7
    assert result["overview"]["pods_failed"] == 1
    assert result["conservative_metadata"]["coverage_ratio"] == "7/8"
    assert not _says_healthy(result["recommendations"])


@pytest.mark.asyncio
async def test_conservative_all_clean_reports_no_issues(server, monkeypatch):
    _patch(server, monkeypatch, analysis=_CLEAN_ANALYSIS)

    result = await server.conservative_namespace_overview(namespace="team-a")

    assert result["overview"]["pods_failed"] == 0
    assert "No critical issues detected in sampled pods" in result["recommendations"]


# ── End to end: a real pods/log 403 from the Kubernetes client ──────────────
# get_all_pod_logs used to store "Error fetching logs: Forbidden" as the
# container's log text, so smart_summarize_pod_logs analysed the error string
# as a log line and never returned {"error": ...}.

class _ForbiddenLogsCore:
    def read_namespaced_pod(self, name, namespace, **kwargs):
        container = SimpleNamespace(name="main")
        return SimpleNamespace(spec=SimpleNamespace(containers=[container]),
                               status=SimpleNamespace(start_time=None))

    def read_namespaced_pod_log(self, *args, **kwargs):
        raise ApiException(status=403, reason="Forbidden")


def _patch_forbidden_cluster(server, monkeypatch):
    real_get_pod_logs = server.get_pod_logs
    clients = SimpleNamespace(core_api=_ForbiddenLogsCore())

    async def get_pod_logs(*args, **kwargs):
        kwargs["clients"] = clients
        return await real_get_pod_logs(*args, **kwargs)

    async def mock_pods(namespace, **kwargs):
        return _PODS

    async def mock_events(*args, **kwargs):
        return _QUIET_EVENTS

    monkeypatch.setattr(server, "get_pod_logs", get_pod_logs)
    monkeypatch.setattr(server, "list_pods_in_namespace", mock_pods)
    monkeypatch.setattr(server, "smart_get_namespace_events", mock_events)


@pytest.mark.asyncio
async def test_e2e_forbidden_pod_logs_are_an_error(server, monkeypatch):
    _patch_forbidden_cluster(server, monkeypatch)

    result = await server.smart_summarize_pod_logs(namespace="team-a", pod_name="pod-0", summary_level="brief")

    assert "error" in result, result
    assert "Forbidden" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("focus_areas", [None, ["performance"]])
async def test_e2e_adaptive_forbidden_is_denied_not_healthy(server, monkeypatch, focus_areas):
    _patch_forbidden_cluster(server, monkeypatch)

    result = await server.adaptive_namespace_investigation(namespace="team-a", focus_areas=focus_areas)

    assert result["investigation_summary"]["pods_analyzed"] == 0
    assert result["critical_issues"] == []  # the error text is not a log finding
    assert not _says_healthy(result["recommendations"]), result["recommendations"]
    coverage = result["adaptive_metadata"]["coverage"]
    assert (coverage["scanned"], coverage["denied"], coverage["verdict"]) == (0, 8, "none")


@pytest.mark.asyncio
@pytest.mark.parametrize("focus_areas", [None, ["performance"]])
async def test_e2e_conservative_forbidden_is_not_healthy(server, monkeypatch, focus_areas):
    _patch_forbidden_cluster(server, monkeypatch)

    result = await server.conservative_namespace_overview(namespace="team-a", focus_areas=focus_areas)

    assert result["overview"]["pods_analyzed"] == 0
    assert result["critical_issues"] == []
    assert not _says_healthy(result["recommendations"]), result["recommendations"]


@pytest.mark.asyncio
async def test_adaptive_events_error_is_in_the_summary(server, monkeypatch):
    _patch(server, monkeypatch, analysis=_CLEAN_ANALYSIS,
           events={"error": "Failed to fetch events: (403) Forbidden"})

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    assert "Forbidden" in result["investigation_summary"]["events_error"]


@pytest.mark.asyncio
async def test_denied_count_ignores_403_inside_names(server, monkeypatch):
    _patch(server, monkeypatch, analysis={"error": 'Failed to retrieve logs: pods "build-4031" not found'})

    result = await server.adaptive_namespace_investigation(namespace="team-a")

    coverage = result["adaptive_metadata"]["coverage"]
    assert (coverage["denied"], coverage["skipped"]) == (0, 8)
