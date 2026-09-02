"""Bug 3 (memory: pharos-tool-bugs-live-testing) — get_tekton_pipeline_runs_status
must separate cancelled runs from failures and sort failures newest-first.

Live findings (fleet runs 2026-08-21, rh01 + p02): 4 of 10 "recent_failures"
were user-cancelled runs (reason *Cancelled*), and the list was not
time-ordered — entries spanned Jul 2025 to Apr 2026, burying today's real
failures. Cancelled is an operator action, not a failure.
"""
from types import SimpleNamespace

import pytest


def _pr(name, ns, reason, start, cond_status="False", cond_type="Succeeded"):
    return {
        "metadata": {"name": name, "namespace": ns},
        "status": {
            "startTime": start,
            "completionTime": start,
            "conditions": [{
                "type": cond_type, "status": cond_status,
                "reason": reason, "message": f"{name} {reason}",
            }],
        },
    }


_PRS = [
    _pr("old-fail", "team-a", "Failed", "2025-07-01T10:00:00Z"),
    _pr("cancelled-1", "team-a", "Cancelled", "2026-08-21T10:00:00Z"),
    _pr("new-fail", "team-b", "Failed", "2026-08-21T12:00:00Z"),
    _pr("cancelled-2", "team-b", "PipelineRunCancelled", "2026-08-21T11:00:00Z"),
    _pr("mid-fail", "team-a", "CouldntGetTask", "2026-04-01T09:00:00Z"),
    # review MINOR-5: Tekton FAILED to cancel — a genuine error, not an
    # operator cancellation
    _pr("couldnt-cancel", "team-a", "PipelineRunCouldntCancel",
        "2026-08-21T09:00:00Z"),
    # re-review MAJOR-1: Tekton v1 graceful-cancel reasons are
    # CancelledRunningFinally / StoppedRunningFinally (with "Running")
    _pr("cancelled-3", "team-b", "CancelledRunningFinally",
        "2026-08-21T08:00:00Z"),
    _pr("ok-run", "team-a", "Succeeded", "2026-08-21T13:00:00Z", cond_status="True"),
]


def _fake_clients():
    custom = SimpleNamespace(
        list_cluster_custom_object=lambda **kw: {"items": list(_PRS)},
        list_namespaced_custom_object=lambda **kw: {"items": []},
    )
    core = SimpleNamespace(
        list_namespace=lambda **kw: SimpleNamespace(items=[]),
    )
    return SimpleNamespace(custom_api=custom, core_api=core)


@pytest.fixture()
def status_result(server, monkeypatch):
    monkeypatch.setattr(server, "_resolve_k8s",
                        lambda source: (_fake_clients(), None))
    monkeypatch.setattr(server, "_gate_extension", lambda *a, **k: None)
    monkeypatch.setattr(server.ReadOnlyK8sClient, "wrap",
                        staticmethod(lambda c: c))
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        server.get_tekton_pipeline_runs_status())


def test_cancelled_runs_not_in_recent_failures(status_result):
    names = [f["name"] for f in status_result["pipeline_runs"]["recent_failures"]]
    assert "cancelled-1" not in names and "cancelled-2" not in names, (
        f"cancelled runs must not appear as failures; got {names}"
    )


def test_cancelled_runs_reported_separately(status_result):
    cancelled = status_result["pipeline_runs"].get("recent_cancelled", [])
    names = [c["name"] for c in cancelled]
    assert set(names) == {"cancelled-1", "cancelled-2", "cancelled-3"}, (
        f"cancelled runs must be reported in their own bucket; got {names}"
    )


def test_recent_failures_sorted_newest_first(status_result):
    names = [f["name"] for f in status_result["pipeline_runs"]["recent_failures"]]
    assert names == ["new-fail", "couldnt-cancel", "mid-fail", "old-fail"], (
        f"'recent' must mean newest-first by start_time; got {names}"
    )


def test_couldnt_cancel_is_a_failure(status_result):
    """PipelineRunCouldntCancel means Tekton FAILED to cancel — an error
    condition, not an operator action (review MINOR-5)."""
    failures = [f["name"] for f in status_result["pipeline_runs"]["recent_failures"]]
    cancelled = [c["name"] for c in status_result["pipeline_runs"]["recent_cancelled"]]
    assert "couldnt-cancel" in failures and "couldnt-cancel" not in cancelled


def test_success_rate_excludes_cancelled(status_result):
    """Review MINOR-6: cancelled runs must not drag the success rate down.
    Fixture: 1 success, 4 genuine failures, 2 cancelled, 0 running →
    rate = 1/5 = 20.0%, not 1/7 = 14.3%."""
    rate_lines = [i for i in status_result["insights"] if "success rate" in i]
    assert rate_lines, f"no success-rate insight in {status_result['insights']}"
    assert "20.0%" in rate_lines[0], (
        f"cancelled runs must be excluded from the rate; got {rate_lines[0]!r}"
    )
    assert "cancelled excluded" in rate_lines[0], (
        f"the exclusion must be stated; got {rate_lines[0]!r}"
    )


def test_cancelled_runs_get_an_insight(status_result):
    """Review NIT-15: total_cancelled must not be silent in the report."""
    assert any("cancelled" in i.lower() for i in status_result["insights"]), (
        f"cancelled count must surface in insights; got {status_result['insights']}"
    )


def test_sampling_disclosure_present(status_result):
    info = status_result.get("sampling_info", {})
    assert "census" in info.get("note", ""), (
        f"sampling_info.note must state counts are not a full census (fleet "
        f"agents read the percentages as cluster-wide); got {info.get('note')!r}"
    )
