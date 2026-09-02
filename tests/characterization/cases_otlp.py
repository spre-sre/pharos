"""OTLP-source characterization cases for the two wired log tools.

These cases rewire server._lumino_config, server._source_registry,
server._adapter_instances, server._otlp_rings, and server._otlp_listening so
that smart_summarize_pod_logs and stream_analyze_pod_logs use the OtlpLogSource
adapter backed by a pre-seeded LogRing.

Integration: cases.py appends OTLP_CASES to CASES at the bottom.  No existing
case ids are altered.  The circular import pattern mirrors cases_file.py.

Golden case ids (appended to case_id):
  smart_summarize_pod_logs-otlp-source   →  normal fetch from pre-seeded ring
  stream_analyze_pod_logs-otlp-outside-retention  →  outside-retention F12 return
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from .cases import ToolCase  # noqa: E402  (circular — intentional, safe)

from core.config_types import ResolvedConfig, SourceConfig
from core.registry import build_registry

# ─── shared config ────────────────────────────────────────────────────────────

_OTLP_SOURCE_NAME = "otlp-test"
_OTLP_OPTS = {"ring_capacity": 10, "max_body_bytes": 65536}

_OTLP_CFG = ResolvedConfig(
    profile="test",
    sources={
        _OTLP_SOURCE_NAME: SourceConfig(
            adapter="otlp",
            options=_OTLP_OPTS,
        )
    },
)
_OTLP_REGISTRY = build_registry(_OTLP_CFG)


# ─── ring + adapter factories ─────────────────────────────────────────────────

def _make_ring(capacity: int = 10, start_ts: float = 1_000_000.0,
               now_ts: float = 1_000_100.0):
    """Create a LogRing with a fixed clock for golden determinism."""
    from adapters.otlp.rings import LogRing
    return LogRing(capacity=capacity, now_fn=lambda: now_ts, start_ts=start_ts)


def _push_log(ring, entity: str, body: str, recv_ts: float = 1_000_010.0) -> None:
    """Append a LogRecord to *ring* with a fixed recv_ts."""
    from core.signals import LogRecord
    from adapters.otlp.rings import iso_z
    rec = LogRecord(
        timestamp=iso_z(recv_ts),
        body=body,
        attributes={"entity": entity},
    )
    ring.append(recv_ts, rec)


def _make_adapter(ring, now_ts: float = 1_000_100.0):
    """Create an OtlpLogSource wrapping *ring* with a fixed clock."""
    from adapters.otlp.logs import OtlpLogSource
    opts = {
        "ring_capacity": ring._capacity,
        "max_body_bytes": 65536,
        "max_record_bytes": 65536,
        "signals": ["logs"],
    }
    return OtlpLogSource(ring, opts, now_fn=lambda: now_ts)


# ─── case-1 patches: normal fetch ─────────────────────────────────────────────

def _normal_otlp_patches() -> dict:
    """Patches for the normal OTLP fetch golden.

    Ring seeded with 3 records for entity="test-app".  pod_name="*" matches
    all entities via fnmatch so all 3 records are returned.  No eviction.
    """
    ring = _make_ring(capacity=10)
    for body in [
        "ERROR: database connection timeout after 30s",
        "WARN: retrying database connection (attempt 1/3)",
        "INFO: database connection established successfully",
    ]:
        _push_log(ring, entity="test-app", body=body)

    adapter = _make_adapter(ring)

    return {
        "_lumino_config": _OTLP_CFG,
        "_source_registry": _OTLP_REGISTRY,
        "_adapter_instances": {_OTLP_SOURCE_NAME: adapter},
        "_otlp_rings": {_OTLP_SOURCE_NAME: ring},
        "_otlp_listening": True,
        "k8s_core_api": None,   # proves k8s path was never taken
    }


# ─── case-2 patches: outside-retention (F12 evicted>0 variant) ───────────────

def _outside_retention_patches() -> dict:
    """Patches for the outside-retention golden (V1 predicate, F12 evicted>0).

    Ring capacity=2; 3 records pushed → 1 evicted, 2 retained (entity="kept-app").
    pod_name="no-match-pod" does NOT match "kept-app" via fnmatch → empty batch.
    window.start=None (default no-time-params shape, V6) and evicted=1>0 →
    V1 predicate fires → outside_retention return with F12 evicted>0 message.
    """
    ring = _make_ring(capacity=2)
    for i in range(3):
        _push_log(ring, entity="kept-app", body=f"INFO: log line {i}")
    # After 3 pushes into capacity-2 ring: 1 evicted, 2 retained.

    adapter = _make_adapter(ring)

    return {
        "_lumino_config": _OTLP_CFG,
        "_source_registry": _OTLP_REGISTRY,
        "_adapter_instances": {_OTLP_SOURCE_NAME: adapter},
        "_otlp_rings": {_OTLP_SOURCE_NAME: ring},
        "_otlp_listening": True,
        "k8s_core_api": None,
    }


# ─── golden cases list ────────────────────────────────────────────────────────

OTLP_CASES: list[ToolCase] = [
    # ── otlp-1: smart_summarize_pod_logs via OtlpLogSource (normal fetch) ─────
    # pod_name="*" matches entity="test-app" via fnmatch; no time params → all
    # records returned; ring has no eviction.
    # Case name uses the OLD module-attr name per F10 (brief §Task-5).
    ToolCase(
        name="smart_summarize_pod_logs",
        kwargs={
            "namespace": "ns",
            "pod_name": "*",
            "source": _OTLP_SOURCE_NAME,
        },
        patches=_normal_otlp_patches(),
        id="otlp-source",
    ),

    # ── otlp-2: stream_analyze_pod_logs, outside-retention (F12 evicted>0) ───
    # Default no-time-params call shape (only namespace/pod_name/source passed).
    # pod_name="no-match-pod" doesn't match "kept-app" → empty batch.
    # evicted=1 > 0 + window.start=None → V1 predicate fires.
    # Golden pins: outside_retention=True, evicted=1, F12 evicted>0 message.
    # Case name uses the OLD module-attr name per F10.
    ToolCase(
        name="stream_analyze_pod_logs",
        kwargs={
            "namespace": "ns",
            "pod_name": "no-match-pod",
            "source": _OTLP_SOURCE_NAME,
        },
        patches=_outside_retention_patches(),
        id="otlp-outside-retention",
    ),
]
