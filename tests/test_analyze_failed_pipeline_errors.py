"""
Tests for analyze_failed_pipeline error handling (Bug 3).

Live error: for a pruned PipelineRun the tool returned
  {"error": "(404)\nReason: Not Found\nHTTP response headers: ...Audit-Id..."}
— a raw ApiException string containing headers, audit IDs, and other noise
that LLM agents cannot parse.

Fix: catch ApiException status==404 in get_pipeline_details() and return a
structured dict.  analyze_failed_pipeline() checks for it and returns a
clean, agent-friendly not_found response.  Other ApiException statuses must
NOT include raw HTTP headers.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

# ---------------------------------------------------------------------------
# Server-mcp import with fake kubeconfig (same isolation as conftest.py)
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


def _make_api_exception(status: int, reason: str = "Not Found") -> ApiException:
    """Build an ApiException whose str() includes fake raw HTTP headers,
    mirroring real kubernetes client output."""
    e = ApiException(status=status, reason=reason)
    # Simulate the raw HTTP header noise the live bug exposed.
    e.body = (
        f'{{"kind":"Status","apiVersion":"v1","reason":"{reason}",'
        f'"code":{status}}}'
    )
    e.headers = {
        "Audit-Id": "fake-audit-id-1234",
        "Content-Type": "application/json",
        "X-Content-Type-Options": "nosniff",
    }
    return e


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
    spec = importlib.util.spec_from_file_location("server_mcp_pipeline", SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_pipeline"] = mod
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


def _fake_404_custom_api():
    """Return a mock k8s_custom_api that raises ApiException(404) on get."""
    api = MagicMock()
    api.get_namespaced_custom_object.side_effect = _make_api_exception(404)
    api.list_cluster_custom_object.return_value = {"items": []}
    api.list_namespaced_custom_object.return_value = {"items": []}
    return api


def _fake_403_custom_api():
    """Return a mock k8s_custom_api that raises ApiException(403) on get."""
    api = MagicMock()
    api.get_namespaced_custom_object.side_effect = _make_api_exception(403, "Forbidden")
    api.list_cluster_custom_object.return_value = {"items": []}
    api.list_namespaced_custom_object.return_value = {"items": []}
    return api


# ---------------------------------------------------------------------------
# Bug 3 tests
# ---------------------------------------------------------------------------

class TestAnalyzeFailedPipeline404:
    """When the PipelineRun is not found (404), the tool must return a
    structured not_found response — no raw HTTP headers."""

    @pytest.mark.asyncio
    async def test_returns_status_not_found(self, server, monkeypatch):
        monkeypatch.setattr(server, "k8s_custom_api", _fake_404_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "pruned-run")
        assert result.get("status") == "not_found", (
            f"Expected status=not_found, got: {result}"
        )

    @pytest.mark.asyncio
    async def test_not_found_includes_namespace(self, server, monkeypatch):
        monkeypatch.setattr(server, "k8s_custom_api", _fake_404_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "pruned-run")
        assert result.get("namespace") == "team-a"

    @pytest.mark.asyncio
    async def test_not_found_includes_pipeline_run(self, server, monkeypatch):
        monkeypatch.setattr(server, "k8s_custom_api", _fake_404_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "pruned-run")
        assert result.get("pipeline_run") == "pruned-run"

    @pytest.mark.asyncio
    async def test_not_found_includes_message(self, server, monkeypatch):
        monkeypatch.setattr(server, "k8s_custom_api", _fake_404_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "pruned-run")
        assert result.get("message"), "Expected a non-empty 'message' field"
        # Message must mention the pipeline run name and namespace.
        assert "pruned-run" in result["message"]
        assert "team-a" in result["message"]

    @pytest.mark.asyncio
    async def test_not_found_includes_suggestions(self, server, monkeypatch):
        monkeypatch.setattr(server, "k8s_custom_api", _fake_404_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "pruned-run")
        suggestions = result.get("suggestions", [])
        assert len(suggestions) >= 1, "Expected at least one suggestion"

    @pytest.mark.asyncio
    async def test_no_raw_headers_in_404_response(self, server, monkeypatch):
        monkeypatch.setattr(server, "k8s_custom_api", _fake_404_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "pruned-run")
        result_str = str(result)
        assert "Audit-Id" not in result_str, "Raw header Audit-Id must not appear in response"
        assert "HTTPHeaderDict" not in result_str, "HTTPHeaderDict must not appear in response"

    @pytest.mark.asyncio
    async def test_no_raw_headers_for_other_exception_status(self, server, monkeypatch):
        """Non-404 ApiExceptions must also not expose raw HTTP headers."""
        monkeypatch.setattr(server, "k8s_custom_api", _fake_403_custom_api())
        result = await server.analyze_failed_pipeline("team-a", "forbidden-run")
        result_str = str(result)
        assert "Audit-Id" not in result_str, "Raw header Audit-Id must not appear in response"
        assert "HTTPHeaderDict" not in result_str, "HTTPHeaderDict must not appear in response"


# ---------------------------------------------------------------------------
# F-14: Unknown-status TaskRun / pipeline classification (cleanup2b)
# ---------------------------------------------------------------------------

# A PLR that definitively failed at the pipeline level (reason="Failed")
# but has one TaskRun that never started (no conditions → reason="Unknown").
UNKNOWN_PLR = {
    "metadata": {"name": "run-x", "namespace": "ns-a", "labels": {}},
    "spec": {"pipelineRef": {"name": "pipe"}},
    "status": {
        "conditions": [{"type": "Succeeded", "status": "False", "reason": "Failed",
                        "message": "Pipeline failed"}],
    },
}

# TaskRun with NO conditions → list_taskruns resolves current_status="Unknown".
# tekton.dev/pipelineRun label is LOAD-BEARING: list_taskruns drops any TR
# whose label doesn't match the pipeline_run filter (server-mcp.py:1884).
UNKNOWN_TR = {
    "metadata": {"name": "run-x-task-1", "namespace": "ns-a",
                 "labels": {"tekton.dev/pipelineTask": "task-1",
                            "tekton.dev/pipelineRun": "run-x"}},
    "spec": {},
    "status": {
        "conditions": [],  # no conditions → reason defaults to "Unknown"
        "steps": [],
    },
}

# A PLR with NO conditions at all → get_pipeline_details resolves status="Unknown".
# Used for the pipeline-level guard test (Step 5b).
UNKNOWN_PLR_STATUS = {
    "metadata": {"name": "run-y", "namespace": "ns-b", "labels": {}},
    "spec": {"pipelineRef": {"name": "pipe"}},
    "status": {
        "conditions": [],  # no conditions → condition={} → reason defaults to "Unknown"
    },
}


def _fake_unknown_tr_api():
    """Custom API that routes PLR and TaskRun calls for the Unknown-TR test.

    get_namespaced_custom_object(plural="pipelineruns") → UNKNOWN_PLR
    list_namespaced_custom_object(plural="taskruns") → {items: [UNKNOWN_TR]}
    All other calls return empty.
    """
    api = MagicMock()

    def get_namespaced(*a, group=None, version=None, namespace=None, plural=None,
                       name=None, **kw):
        if plural == "pipelineruns":
            return UNKNOWN_PLR
        return {}

    def list_namespaced(*a, group=None, version=None, namespace=None, plural=None,
                        **kw):
        if plural == "taskruns":
            return {"items": [UNKNOWN_TR]}
        return {"items": []}

    api.get_namespaced_custom_object.side_effect = get_namespaced
    api.list_namespaced_custom_object.side_effect = list_namespaced
    api.list_cluster_custom_object.return_value = {"items": []}
    return api


def _fake_unknown_plr_status_api():
    """Custom API that routes calls for the Unknown-pipeline-status test.

    get_namespaced_custom_object(plural="pipelineruns") → UNKNOWN_PLR_STATUS
    (conditions=[] → get_pipeline_details resolves status="Unknown")
    """
    api = MagicMock()

    def get_namespaced(*a, group=None, version=None, namespace=None, plural=None,
                       name=None, **kw):
        if plural == "pipelineruns":
            return UNKNOWN_PLR_STATUS
        return {}

    def list_namespaced(*a, **kw):
        return {"items": []}

    api.get_namespaced_custom_object.side_effect = get_namespaced
    api.list_namespaced_custom_object.side_effect = list_namespaced
    api.list_cluster_custom_object.return_value = {"items": []}
    return api


class TestAnalyzeFailedPipelineUnknownStatus:
    """F-14: Unknown-status TaskRuns must not be classified as failed.

    Mutation target (M-C2b): removing "Unknown" from the exclusion tuple at
    analyze_failed_pipeline (server-mcp.py) makes this test RED
    (failed_task_count==1 instead of 0).
    """

    @pytest.mark.asyncio
    async def test_unknown_status_task_run_not_classified_as_failed(
        self, server, monkeypatch
    ):
        """A TaskRun with no conditions (reason="Unknown") must not appear in
        failed_tasks — it never definitively failed, it never started.

        Pre-fix: "Unknown" not in exclusion tuple → task classified as failed
                 → failed_task_count == 1  (FAILS assertion == 0)
        Post-fix: "Unknown" in exclusion tuple → task skipped
                 → failed_task_count == 0  (PASSES)
        """
        monkeypatch.setattr(server, "k8s_custom_api", _fake_unknown_tr_api())
        result = await server.analyze_failed_pipeline("ns-a", "run-x")
        assert result.get("failed_task_count") == 0, (
            f"Unknown-status TaskRun must not count as failed; "
            f"got failed_task_count={result.get('failed_task_count')!r}; "
            f"result={result}"
        )

    @pytest.mark.asyncio
    async def test_unknown_pipeline_status_not_definitive_failure(
        self, server, monkeypatch
    ):
        """A PipelineRun whose resolved status is "Unknown" (no conditions) is not
        a definitive failure — the tool must return early with an error message.

        Step 5b fixture: conditions=[] → condition={} → reason defaults to "Unknown"
        (NOT None — using "status": {} would resolve to None which is not "Unknown").

        Pre-fix: guard checks == "Succeeded" only → "Unknown" proceeds to full
                 analysis and may fabricate failed-task advice (FAILS assertion).
        Post-fix: guard widened to in ("Succeeded", "Unknown") → early return
                 with "not a definitive failure" in result["error"] (PASSES).

        Deferral: pipeline_details.get("status") is None (completely absent status)
        is also not a definitive failure but is NOT caught by the widened guard.
        That is a separate scope from F-14 (Unknown-status TaskRuns) and is
        recorded as a deferral — it is not fixed here.
        """
        monkeypatch.setattr(server, "k8s_custom_api", _fake_unknown_plr_status_api())
        result = await server.analyze_failed_pipeline("ns-b", "run-y")
        assert "error" in result, (
            f"Expected early-return with 'error' key for Unknown pipeline status; "
            f"got result={result}"
        )
        assert "not a definitive failure" in result["error"], (
            f"Expected 'not a definitive failure' in error message; "
            f"got error={result.get('error')!r}"
        )
