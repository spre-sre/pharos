"""
Tests for F-13: Kueue/ResourceFlavor event classification.

All tests in the _RED_ block must FAIL pre-fix and PASS post-fix.
Regression tests must be GREEN both before and after the fix.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from helpers.event_analysis import (
    classify_event_category_from_string,
    classify_event_severity_from_string,
)

# ============================================================================
# RED TESTS (pre-fix: FAIL; post-fix: PASS)
# ============================================================================


def test_couldnt_assign_flavors_resource():
    """couldn't assign flavors → RESOURCE (pre-fix: OTHER)."""
    event = (
        "[2026-01-01T00:00:00Z] Warning: Pending - couldn't assign flavors"
        " (Object: Pod/mintmaker-1)"
    )
    assert classify_event_category_from_string(event) == "RESOURCE"


def test_couldnt_assign_flavors_high_severity():
    """couldn't assign flavors → HIGH (pre-fix: MEDIUM via 'pending')."""
    event = (
        "[2026-01-01T00:00:00Z] Warning: Pending - couldn't assign flavors"
        " (Object: Pod/mintmaker-1)"
    )
    assert classify_event_severity_from_string(event) == "HIGH"


def test_insufficient_quota_resource():
    """insufficient quota → RESOURCE/HIGH.

    cpu/memory inputs: pre-fix RESOURCE/MEDIUM (category via coincident keyword).
    The severity assertion is the load-bearing RED pin for those two.

    pods input (no cpu/memory): pre-fix OTHER/MEDIUM — pins the 'insufficient quota'
    RESOURCE keyword itself, which is otherwise covered only by coincident matches.
    """
    cpu_event = (
        "[2026-01-01T00:00:00Z] Warning: ResourceQuotaExceeded"
        " - insufficient quota for cpu: requested 4, used 100, limited 100"
        " (Object: Pod/mintmaker-2)"
    )
    mem_event = (
        "[2026-01-01T00:00:00Z] Warning: ResourceQuotaExceeded"
        " - insufficient quota for memory: requested 8Gi, used 16Gi, limited 16Gi"
        " (Object: Pod/mintmaker-3)"
    )
    pods_event = (
        "[2026-01-01T00:00:00Z] Warning: ResourceQuotaExceeded"
        " - insufficient quota for pods in flavor default-flavor"
        " (Object: Pod/mintmaker-4)"
    )
    # cpu/memory: RESOURCE (regression-safe via coincident keyword); HIGH is the RED pin
    assert classify_event_category_from_string(cpu_event) == "RESOURCE"
    assert classify_event_severity_from_string(cpu_event) == "HIGH"
    assert classify_event_category_from_string(mem_event) == "RESOURCE"
    assert classify_event_severity_from_string(mem_event) == "HIGH"
    # pods: TRUE RED for both — pins 'insufficient quota' in RESOURCE and HIGH
    assert classify_event_category_from_string(pods_event) == "RESOURCE"
    assert classify_event_severity_from_string(pods_event) == "HIGH"


def test_resourceflavor_resource():
    """resourceflavor → RESOURCE/HIGH (pre-fix: OTHER/MEDIUM)."""
    event = (
        "[2026-01-01T00:00:00Z] Warning: ResourceFlavorNotFound"
        " - resourceflavor spot-2xlarge not found"
        " (Object: LocalQueue/mintmaker)"
    )
    assert classify_event_category_from_string(event) == "RESOURCE"
    assert classify_event_severity_from_string(event) == "HIGH"


def test_exceeds_quota_resource():
    """exceeds quota → RESOURCE/HIGH (pre-fix: OTHER/MEDIUM)."""
    event = (
        "[2026-01-01T00:00:00Z] Warning: Pending - exceeds quota"
        " (Object: Workload/mintmaker-job)"
    )
    assert classify_event_category_from_string(event) == "RESOURCE"
    assert classify_event_severity_from_string(event) == "HIGH"


def test_full_live_string_resource_high():
    """CONTROLLER LIVE VERIFICATION 2026-07-28 — full mintmaker event text.

    This is the exact string observed on the stage cluster.
    Pre-fix: OTHER/MEDIUM.  Post-fix: RESOURCE/HIGH.

    For THIS string 'couldn't assign flavors' is the load-bearing keyword —
    it alone drives both RESOURCE and HIGH.  'insufficient unused quota' adds
    defence-in-depth but is not the sole pin here (M-C3c confirmed this).
    The quota-only shape is pinned separately by test_quota_text_alone_resource_high.

    Note: 'insufficient quota' does NOT substring-match 'insufficient unused quota'
    (the word 'unused' breaks it), so shipping only 'insufficient quota' would leave
    the live event shape unmatched by that keyword.
    """
    event = (
        "Warning: Pending - couldn't assign flavors to pod set pod-set-1:"
        " insufficient unused quota for mintmaker in flavor default-flavor,"
        " 1 more needed"
    )
    assert classify_event_category_from_string(event) == "RESOURCE"
    assert classify_event_severity_from_string(event) == "HIGH"


def test_quota_text_alone_resource_high():
    """Quota clause WITHOUT the flavor-assignment prefix.

    Isolates 'insufficient unused quota' from 'couldn't assign flavors' —
    this is the test that kills M-C3c (removing 'insufficient unused quota'
    makes this test go RED).

    Pre-fix: OTHER/MEDIUM (bracketed Warning, no RESOURCE or HIGH keyword matches).
    Post-fix: RESOURCE/HIGH via 'insufficient unused quota' in both lists.
    """
    event = (
        "[2026-01-01T00:00:00Z] Warning: Pending - insufficient unused quota"
        " for mintmaker in flavor default-flavor, 1 more needed"
        " (Object: Workload/mintmaker-job)"
    )
    assert classify_event_category_from_string(event) == "RESOURCE"
    assert classify_event_severity_from_string(event) == "HIGH"


def test_exceeded_quota_k8s_admission_resource():
    """Real K8s admission-controller text → RESOURCE.

    Kueue emits present tense ('exceeds quota'); the K8s ResourceQuota
    admission webhook uses past tense ('exceeded quota').  Both must classify
    as RESOURCE/HIGH.  This test is RED pre-fix because only 'exceeds quota'
    exists in the keyword lists; 'exceeded quota' does not substring-match it.

    Sample chosen without cpu/memory tokens — those keywords already drive
    RESOURCE via other entries and would mask the gap.
    """
    event = (
        'pods "x" is forbidden: exceeded quota: object-counts,'
        " requested: pods=1, used: pods=10, limited: pods=10"
    )
    assert classify_event_category_from_string(event) == "RESOURCE"
    assert classify_event_severity_from_string(event) == "HIGH"


# ============================================================================
# REGRESSION TESTS (GREEN both before and after the fix)
# ============================================================================


def test_backoff_still_failure():
    assert (
        classify_event_category_from_string(
            "[...] Warning: BackOff - Back-off restarting failed container"
            " (Object: Pod/p)"
        )
        == "FAILURE"
    )


def test_failedscheduling_still_failure():
    assert (
        classify_event_category_from_string(
            "[...] Warning: FailedScheduling - 0/3 nodes are available"
            " (Object: Pod/api-2)"
        )
        == "FAILURE"
    )
