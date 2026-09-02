"""Tracer KubeArchive fallback (live finding 2026-08-20).

Prod Tekton GC prunes PLRs within ~2 hours; a commit trace for a build that
provably happened returned not_found because correlate_pipeline_events only
searches live PLRs.  merge_archived_plrs() folds archived raw PLR objects into
the flow through the same matcher/time filter, deduped against live entries.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.extension import ToolRegistry
from extensions.konflux.lineage import merge_archived_plrs

COMMIT = "0ed18e76072fa6141d2d8db8caf49a3d8998880e"


def _archived_plr(name, commit=COMMIT, start="2026-08-20T11:30:41Z", ns="hummingbird-tenant"):
    return {
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"pipelinesascode.tekton.dev/sha": commit},
            "annotations": {},
        },
        "status": {
            "startTime": start,
            "completionTime": "2026-08-20T11:44:00Z",
            "conditions": [{"type": "Succeeded", "status": "True", "reason": "Succeeded"}],
        },
    }


def _live_entry(name, ns="hummingbird-tenant", start="2026-08-20T12:00:00Z"):
    return {
        "cluster": "current",
        "namespace": ns,
        "pipeline_name": name,
        "pipeline_run_name": name,
        "status": "succeeded",
        "start_time": start,
        "completion_time": None,
        "tasks": [],
        "labels": {},
        "annotations": {},
    }


def test_archived_matches_are_merged_and_flagged():
    merged = merge_archived_plrs(
        [], [_archived_plr("build-m7xdp")], COMMIT, "commit"
    )
    assert len(merged) == 1
    assert merged[0]["archived"] is True
    assert merged[0]["cluster"] == "kubearchive"
    assert merged[0]["pipeline_run_name"] == "build-m7xdp"


def test_non_matching_archived_plrs_are_filtered():
    merged = merge_archived_plrs(
        [], [_archived_plr("other", commit="deadbeef" * 5)], COMMIT, "commit"
    )
    assert merged == []


def test_live_entry_wins_dedup_over_archived_same_name():
    live = [_live_entry("build-m7xdp")]
    merged = merge_archived_plrs(live, [_archived_plr("build-m7xdp")], COMMIT, "commit")
    assert len(merged) == 1
    assert "archived" not in merged[0]


def test_time_window_filters_archived_entries():
    merged = merge_archived_plrs(
        [], [_archived_plr("old", start="2026-08-19T00:00:00Z")], COMMIT, "commit",
        start_time="2026-08-20T00:00:00Z", end_time="2026-08-20T23:59:59Z",
    )
    assert merged == []


def test_merged_flow_is_sorted_by_start_time():
    live = [_live_entry("later", start="2026-08-20T12:00:00Z")]
    merged = merge_archived_plrs(
        live, [_archived_plr("earlier", start="2026-08-20T11:30:41Z")], COMMIT, "commit"
    )
    assert [p["pipeline_run_name"] for p in merged] == ["earlier", "later"]


def test_malformed_archived_object_is_skipped():
    merged = merge_archived_plrs(
        [], [None, {"metadata": "not-a-dict"}, _archived_plr("ok")], COMMIT, "commit"
    )
    assert [p["pipeline_run_name"] for p in merged] == ["ok"]


# ── Registry seams ───────────────────────────────────────────────────────────


def _registry_with_fake_server(calls):
    server = types.SimpleNamespace()

    async def fake_fetch(namespace, **kwargs):
        calls.append((namespace, kwargs.get("source")))
        return []

    server._query_archived_plrs_for_trace = fake_fetch
    return ToolRegistry(server, mcp=object(), config=None, adapters=None, packs={})


def test_registry_default_source_seam_pins_empty_source():
    calls = []
    reg = _registry_with_fake_server(calls)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        reg.query_archived_plrs(namespace="ns1")
    )
    assert calls == [("ns1", "")]


def test_instance_view_seam_pins_named_source():
    calls = []
    reg = _registry_with_fake_server(calls)
    view = reg.for_instance("kflux-prd-rh03")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        view.query_archived_plrs(namespace="ns2")
    )
    assert calls == [("ns2", "kflux-prd-rh03")]


def test_seam_forwards_label_selector_kwarg():
    forwarded = []
    server = types.SimpleNamespace()

    async def fake_fetch(namespace, **kwargs):
        forwarded.append(kwargs.get("label_selector"))
        return []

    server._query_archived_plrs_for_trace = fake_fetch
    reg = ToolRegistry(server, mcp=object(), config=None, adapters=None, packs={})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        reg.query_archived_plrs(
            namespace="ns", label_selector=f"pipelinesascode.tekton.dev/sha={COMMIT}"
        )
    )
    assert forwarded == [f"pipelinesascode.tekton.dev/sha={COMMIT}"]
