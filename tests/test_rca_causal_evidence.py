"""Bug 1 (memory: pharos-tool-bugs-live-testing) — causal status evidence
must outrank symptom-line counts in the advanced RCA.

Live repro 2026-08-21 (ogx-core-functional-its-*, open-data-hub-tenant,
prd-rh01): the TaskRun status said SidecarFailed with the sidecar name in its
message, but 14 "connection refused" symptom lines (the tests polling the
dead sidecar) outvoted it in categorize_errors → probable_root_cause said
"network connectivity issues". An SRE following that lands in a dead end.

Contract: perform_advanced_rca checks authoritative Tekton/K8s status
reasons (SidecarFailed, OOMKilled, timeouts, image-pull, scheduling, ...)
from the failed-task records FIRST; when one is present it becomes the
primary cause and the count vote is demoted to contributing factors.
"""
import asyncio
import logging
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.failure_analysis import perform_advanced_rca  # noqa: E402
from helpers.utils import categorize_errors  # noqa: E402

logger = logging.getLogger("test")


def _run(primary_analysis, timeline=None):
    return asyncio.run(perform_advanced_rca(
        primary_analysis, timeline or [], [], "comprehensive",
        categorize_errors, logger))


def _odh_repro():
    """The live failure shape: causal sidecar status + noisy network symptoms."""
    refused = [
        f"ERROR: vLLM not ready, attempt {i}: curl: (7) Failed to connect "
        f"to localhost port 8000: Connection refused" for i in range(14)
    ]
    return {
        "logs_analyzed": {
            "functional-tests-pod": {
                "log_analysis": {
                    "error_patterns": refused + [
                        "ERROR: vLLM not ready after 300s",
                    ],
                }
            }
        },
        "basic_analysis": {
            "pipeline_status": "Failed",
            "overall_message": "Tasks Completed: 3 (Failed: 1)",
            "failed_tasks": [{
                "task_name": "functional-tests",
                "status": "SidecarFailed",
                "message": 'the TaskRun was marked SidecarFailed: sidecar '
                           '"sidecar-vllm-inference" exited with code 1',
                "failed_steps": [{"step_name": "run-functional-tests",
                                  "exit_code": 1, "reason": "Error"}],
            }],
        },
    }


def test_sidecar_status_outranks_connection_refused_count():
    result = _run(_odh_repro())
    primary = result["root_cause_analysis"]["primary_cause"]
    assert primary.get("category") == "sidecar_failure", (
        f"causal SidecarFailed status must win over 14 connection-refused "
        f"symptom lines; got {primary}"
    )
    # the causal status line must be in the evidence, verbatim enough to act on
    assert any("sidecar-vllm-inference" in str(e) for e in primary.get("evidence", [])), (
        f"evidence must carry the causal status line, got {primary.get('evidence')}"
    )


def test_symptom_vote_becomes_contributing_factor():
    result = _run(_odh_repro())
    rca = result["root_cause_analysis"]
    factors = [f.get("factor") for f in rca.get("contributing_factors", [])]
    assert "network" in factors, (
        f"the outvoted symptom category must remain visible as a "
        f"contributing factor; got {factors}"
    )


def test_oomkilled_step_reason_outranks_noisy_logs():
    pa = _odh_repro()
    pa["basic_analysis"]["failed_tasks"] = [{
        "task_name": "build",
        "status": "Failed",
        "message": "step build-image terminated",
        "failed_steps": [{"step_name": "build-image", "exit_code": 137,
                          "reason": "OOMKilled"}],
    }]
    result = _run(pa)
    primary = result["root_cause_analysis"]["primary_cause"]
    assert primary.get("category") == "oom", f"got {primary}"


def test_determine_root_cause_uses_causal_status():
    """Review BLOCKER: analyze_failed_pipeline derives probable_root_cause
    from determine_root_cause, which must ALSO honor causal status evidence —
    the live repro tool still said 'network' after the first fix."""
    from helpers.utils import determine_root_cause

    results = _odh_repro()["basic_analysis"]
    # give the count vote its network fodder, as the live tool sees it
    results["failed_tasks"][0]["error_categories"] = {"network": 14}
    results["failed_tasks"][0]["error_patterns"] = [
        "Connection refused" for _ in range(14)]

    cause = determine_root_cause(results)
    assert "sidecar" in cause.lower(), (
        f"determine_root_cause must honor SidecarFailed status; got {cause!r}"
    )
    assert "network policies" not in cause.lower()


def test_step_reason_outranks_message_prose():
    """Review MAJOR-4: a step's own termination reason (OOMKilled) is hard
    platform evidence and must beat a sidecar name quoted in message prose."""
    from helpers.failure_analysis import extract_causal_status_evidence

    result = extract_causal_status_evidence({
        "failed_tasks": [{
            "status": "Failed",
            "message": 'sidecar "vault-agent" logged: retrying',
            "failed_steps": [{"step_name": "build", "exit_code": 137,
                              "reason": "OOMKilled"}],
        }],
    })
    assert result and result["category"] == "oom", f"got {result}"


def test_causal_categories_score_high_severity():
    """Review MAJOR-3: the categories the causal tier emits must not be
    triaged LOW/P4 — an OOMKilled pipeline is not a 24-hour problem."""
    from helpers.failure_analysis import assess_failure_severity

    for category in ("oom", "sidecar_failure", "scheduling", "timeout",
                     "image", "config", "resource_limits"):
        severity = assess_failure_severity(
            {"basic_analysis": {"failed_tasks": [{}]}},
            {"root_cause_analysis": {"primary_cause": {"category": category}}},
            {}, {}, [],
        )
        assert severity["severity_level"] != "LOW", (
            f"category {category!r} triaged {severity['severity_level']} "
            f"(score {severity['severity_score']}) — must be at least MEDIUM"
        )
        assert severity["severity_score"] >= 3


def test_quoted_reason_in_message_prose_is_not_causal():
    """Re-review MINOR-3: a condition message can QUOTE step output. Only
    the sidecar phrases are authoritative in messages; every other category
    must come from status or a step termination reason."""
    from helpers.failure_analysis import extract_causal_status_evidence

    result = extract_causal_status_evidence({
        "failed_tasks": [{
            "status": "Failed",
            "message": 'step "test" exited: FAIL: expected ImagePullBackOff got nil',
            "failed_steps": [{"step_name": "test", "exit_code": 1,
                              "reason": "Error"}],
        }],
    })
    assert result is None, (
        f"quoted reason token in message prose must not be causal; got {result}"
    )


def test_timeout_root_cause_gets_timeout_actions():
    """Re-review MINOR-6: the causal timeout root cause must produce
    timeout-specific recommendations, not generic advice."""
    from helpers.utils import recommend_actions

    actions = recommend_actions({
        "probable_root_cause": "Timeout - the run exceeded its deadline "
                               "(TaskRun/PipelineRun timeout)",
        "failed_tasks": [{}],
    })
    assert any("timeout" in a.lower() or "deadline" in a.lower()
               for a in actions), f"got {actions}"


def test_no_causal_status_keeps_count_vote():
    """Regression guard: without an authoritative status reason the count
    vote must behave exactly as before."""
    pa = _odh_repro()
    pa["basic_analysis"]["failed_tasks"] = [{
        "task_name": "tests",
        "status": "Failed",
        "message": "step run-tests failed",
        "failed_steps": [{"step_name": "run-tests", "exit_code": 1,
                          "reason": "Error"}],
    }]
    result = _run(pa)
    primary = result["root_cause_analysis"]["primary_cause"]
    assert primary.get("category") == "network", (
        f"count vote must still decide when no causal status exists; "
        f"got {primary}"
    )
