"""Bug 5 (memory: pharos-tool-bugs-live-testing) — Normal scale-down events
must not classify as CRITICAL.

Live repro 2026-08-21 (openshift-pipelines, prd-rh01, HPA window): two
'Normal: ScalingReplicaSet - Scaled down replica set ...' events got severity
CRITICAL because the CRITICAL keyword list contains the bare substring
"down", which matches "scaled down" (and shutdown/cooldown/teardown).
Routine HPA activity must stay LOW; genuinely-down signals must stay CRITICAL.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.event_analysis import classify_event_severity_from_string  # noqa: E402


SCALE_DOWN = ("[2026-08-21 16:11:03+00:00] Normal: ScalingReplicaSet - "
              "Scaled down replica set tekton-operator-proxy-webhook-6b7794fc77 "
              "from 3 to 2 (Object: Deployment/tekton-operator-proxy-webhook)")


def test_normal_scale_down_is_low():
    assert classify_event_severity_from_string(SCALE_DOWN) == "LOW", (
        "routine HPA scale-down (Normal event) must not be CRITICAL"
    )


def test_normal_shutting_down_is_low():
    event = ("[2026-08-21 16:11:03+00:00] Normal: Killing - "
             "Container proxy shutting down gracefully (Object: Pod/x)")
    assert classify_event_severity_from_string(event) == "LOW"


def test_warning_node_down_stays_critical():
    event = ("[2026-08-21 16:11:03+00:00] Warning: NodeNotReady - "
             "Node worker-3 is down (Object: Node/worker-3)")
    assert classify_event_severity_from_string(event) == "CRITICAL"


def test_warning_camelcase_nodedown_stays_critical():
    """Review MINOR-10: the camelCase reason NodeDown lost its escalation
    when bare 'down' was removed."""
    event = ("[2026-08-21 16:11:03+00:00] Warning: NodeDown - "
             "Node worker-3 NodeDown (Object: Node/worker-3)")
    assert classify_event_severity_from_string(event) == "CRITICAL"


def test_normal_oomkilled_stays_critical():
    event = ("[2026-08-21 16:11:03+00:00] Normal: Killing - "
             "Container was OOMKilled (Object: Pod/x)")
    assert classify_event_severity_from_string(event) == "CRITICAL"
