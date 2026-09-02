"""Tests for core.packs: Pack loader + YAML round-trip + runbook faithfulness proof.

Step 1 tests (a)–(d) as specified in task-4-brief.md.
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.packs import Pack, _PACKS_DIR, load_packs
from helpers.event_analysis import RunbookSuggestionEngine


# ---------------------------------------------------------------------------
# (a) Round-trip: each pack loads with correct name, non-empty runbooks,
#     and appropriate labels.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["k8s-core", "openshift", "konflux"])
def test_pack_name_matches_stem(name):
    packs = load_packs((name,))
    assert packs[name].name == name


@pytest.mark.parametrize("name", ["k8s-core", "openshift", "konflux"])
def test_pack_runbooks_nonempty(name):
    packs = load_packs((name,))
    assert len(packs[name].runbooks) > 0


@pytest.mark.parametrize("name", ["k8s-core", "openshift"])
def test_pack_labels_empty_for_non_konflux(name):
    packs = load_packs((name,))
    assert packs[name].labels == {}


def test_konflux_pack_has_expected_label_keys():
    packs = load_packs(("konflux",))
    labels = packs["konflux"].labels
    assert "trace_commit" in labels
    assert "trace_pr" in labels
    assert "trace_pr_fallback" in labels
    assert isinstance(labels["trace_commit"], list)
    assert isinstance(labels["trace_pr"], list)
    assert isinstance(labels["trace_pr_fallback"], list)


def test_load_packs_returns_name_sorted_dict():
    packs = load_packs(("openshift", "k8s-core", "konflux"))
    assert list(packs.keys()) == ["k8s-core", "konflux", "openshift"]


def test_pack_is_frozen_dataclass():
    packs = load_packs(("k8s-core",))
    pack = packs["k8s-core"]
    assert isinstance(pack, Pack)
    with pytest.raises((AttributeError, TypeError)):
        pack.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# (b) Unknown pack name → ValueError naming it.
# ---------------------------------------------------------------------------


def test_unknown_pack_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="nonexistent_pack"):
        load_packs(("nonexistent_pack",), root=tmp_path)


# ---------------------------------------------------------------------------
# (c) Unknown top-level key in YAML → ValueError mentioning the bad key.
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_raises_value_error(tmp_path):
    bad_yaml = tmp_path / "bad-pack.yaml"
    bad_yaml.write_text("name: bad-pack\nrunbooks: {}\nlabels: {}\nbanana: yes\n")
    with pytest.raises(ValueError, match="banana"):
        load_packs(("bad-pack",), root=tmp_path)


# ---------------------------------------------------------------------------
# (d) Faithfulness equivalence: merged runbooks == RunbookSuggestionEngine DB.
# ---------------------------------------------------------------------------


def test_pack_name_stem_mismatch_raises_value_error(tmp_path):
    bad_yaml = tmp_path / "real-name.yaml"
    bad_yaml.write_text("name: wrong-name\nrunbooks: {}\nlabels: {}\n")
    with pytest.raises(ValueError, match="wrong-name"):
        load_packs(("real-name",), root=tmp_path)


# ---------------------------------------------------------------------------
# Per-file key-set pins: catch runbooks moved to the wrong pack file.
# (The merged-dict equivalence test cannot detect this; these can.)
# ---------------------------------------------------------------------------

_K8S_CORE_KEYS = {"pod_crash_loop", "memory_exhaustion", "network_connectivity"}
_KONFLUX_KEYS = {
    "task_bundle_resolution",
    "push_snapshot_failure",
    "trusted_artifact_failure",
    "registry_auth_failure",
    "pyxis_registration_failure",
    "pipeline_timeout",
}
_OPENSHIFT_KEYS = {
    "etcd_issues",
    "node_not_ready",
    "certificate_issues",
    "machine_config_degraded",
}


def test_k8s_core_pack_contains_exactly_generic_kubernetes_keys():
    packs = load_packs(("k8s-core",))
    assert set(packs["k8s-core"].runbooks.keys()) == _K8S_CORE_KEYS


def test_konflux_pack_contains_exactly_tekton_konflux_keys():
    packs = load_packs(("konflux",))
    assert set(packs["konflux"].runbooks.keys()) == _KONFLUX_KEYS


def test_openshift_pack_contains_exactly_openshift_keys():
    packs = load_packs(("openshift",))
    assert set(packs["openshift"].runbooks.keys()) == _OPENSHIFT_KEYS


def test_konflux_pack_labels_match_lineage_constants():
    """Pack label lists must equal the lineage module constants byte-for-byte.

    This is the Task 5 Step 3 faithfulness gate: packs/konflux.yaml labels
    section is the documentation source of truth; the lineage module constants
    (TRACE_COMMIT_LABEL_KEYS, TRACE_PR_LABEL_KEYS, TRACE_PR_FALLBACK_KEYS) are
    the code source of truth.  They must be identical.
    """
    from extensions.konflux.lineage import (
        TRACE_COMMIT_LABEL_KEYS,
        TRACE_PR_FALLBACK_KEYS,
        TRACE_PR_LABEL_KEYS,
    )

    packs = load_packs(("konflux",))
    labels = packs["konflux"].labels

    assert labels["trace_commit"] == TRACE_COMMIT_LABEL_KEYS, (
        f"Pack trace_commit diverges from lineage.TRACE_COMMIT_LABEL_KEYS:\n"
        f"  pack:    {labels['trace_commit']}\n"
        f"  lineage: {TRACE_COMMIT_LABEL_KEYS}"
    )
    assert labels["trace_pr"] == TRACE_PR_LABEL_KEYS, (
        f"Pack trace_pr diverges from lineage.TRACE_PR_LABEL_KEYS:\n"
        f"  pack:    {labels['trace_pr']}\n"
        f"  lineage: {TRACE_PR_LABEL_KEYS}"
    )
    assert labels["trace_pr_fallback"] == TRACE_PR_FALLBACK_KEYS, (
        f"Pack trace_pr_fallback diverges from lineage.TRACE_PR_FALLBACK_KEYS:\n"
        f"  pack:    {labels['trace_pr_fallback']}\n"
        f"  lineage: {TRACE_PR_FALLBACK_KEYS}"
    )


def test_packs_faithfulness_equals_runbook_database():
    """Merged runbooks from 3 packs must exactly equal RunbookSuggestionEngine's DB.

    This is the core deliverable: every title, step string, estimated_time,
    severity, and references value must be byte-equal after YAML round-trip.
    """
    packs = load_packs(("k8s-core", "konflux", "openshift"))
    merged = {
        **packs["k8s-core"].runbooks,
        **packs["konflux"].runbooks,
        **packs["openshift"].runbooks,
    }
    engine = RunbookSuggestionEngine(events=[], patterns={})
    expected = engine._initialize_runbook_database()
    assert merged == expected, (
        "Runbook packs diverge from RunbookSuggestionEngine._initialize_runbook_database(). "
        "Keys in packs but not engine: "
        f"{set(merged) - set(expected)}. "
        "Keys in engine but not packs: "
        f"{set(expected) - set(merged)}."
    )
