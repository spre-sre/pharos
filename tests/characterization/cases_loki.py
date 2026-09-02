"""Loki-source characterization cases for the two wired log tools.

These cases wire server._adapter_instances, _lumino_config, and
_source_registry so that smart_summarize_pod_logs uses the LokiLogSource
adapter backed by a canned 2-stream session_factory (FIXED ns timestamps).

Integration: cases.py appends LOKI_CASES to CASES at the bottom.  No
existing case ids are altered.  The circular import from cases.py is safe
because ToolCase is defined in cases.py before cases.py imports this file.

Loki source golden id:
  smart_summarize_pod_logs-loki-source  ->  new golden file
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from .cases import ToolCase  # noqa: E402 (circular -- intentional, safe)

from core.config_types import ResolvedConfig, SourceConfig
from core.registry import build_registry


# ─── Canned 2-stream Loki payload ────────────────────────────────────────────
#
# Fixed nanosecond timestamps so the golden is machine-independent and
# double-regen stable.
#
# Stream 1: api-1 pod, 5 log lines
# Stream 2: api-2 pod, 5 log lines
#
# These timestamps are fixed round values:
#   1735689600000000000 = 2026-01-01T00:00:00+00:00
#   1735689601000000000 = 2026-01-01T00:00:01+00:00
#   ...

_STREAM_1_LABELS = {"namespace": "loki-test-ns", "pod": "api-1"}
_STREAM_2_LABELS = {"namespace": "loki-test-ns", "pod": "api-2"}

_BASE_TS_NS = 1_735_689_600_000_000_000  # 2026-01-01T00:00:00Z

_STREAM_1_VALUES = [
    [str(_BASE_TS_NS + i * 1_000_000_000), f"api-1 INFO: message-{i}"]
    for i in range(5)
]
_STREAM_2_VALUES = [
    [str(_BASE_TS_NS + i * 1_000_000_000), f"api-2 ERROR: message-{i}"]
    for i in range(5)
]

_CANNED_LOKI_PAYLOAD = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {"stream": _STREAM_1_LABELS, "values": _STREAM_1_VALUES},
            {"stream": _STREAM_2_LABELS, "values": _STREAM_2_VALUES},
        ],
    },
}


# ─── Fake session with fixed canned payload ───────────────────────────────────


class _CannedLokiResponse:
    """Fake aiohttp response returning the canned 2-stream payload."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: Any):
        pass

    async def json(self) -> dict:
        return self._payload


class _CannedLokiFakeSession:
    """Fake session_factory: always returns the fixed canned payload.

    This is injected via options["session_factory"] when constructing
    LokiLogSource.  It does NOT contact a real Loki instance.

    The golden is stable because:
    - the payload is a module-level constant (not time-dependent)
    - the nanosecond timestamps in the payload are fixed integers
    """

    def __call__(self, *a: Any, timeout: Any = None, **kw: Any) -> "_CannedLokiFakeSession":
        return self

    async def __aenter__(self) -> "_CannedLokiFakeSession":
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        json: Any = None,
        headers: Any = None,
        **kw: Any,
    ) -> _CannedLokiResponse:
        return _CannedLokiResponse(_CANNED_LOKI_PAYLOAD)


# ─── Pre-seeded patches for the loki golden case ─────────────────────────────


def _loki_source_patches() -> dict:
    """Return a fresh patches dict for a single loki-source golden case.

    Patches applied by test_golden_tools.py via monkeypatch.setattr:
      _lumino_config       -- ResolvedConfig with a single "loki-test" source
      _source_registry     -- AdapterRegistry built from that config
      _adapter_instances   -- pre-seeded with the real LokiLogSource instance
                              (canned session_factory baked in); this bypasses
                              the factory cache and avoids needing a real url
      k8s_core_api         -- MagicMock (must remain uncalled -- proves the
                              loki adapter branch bypasses k8s I/O entirely)

    The _adapter_instances pre-seed is the key difference from the file-source
    cases (which start cold): for loki we pre-seed to thread the canned
    session_factory through the constructor without server-module patching.
    """
    from adapters.loki.logs import LokiLogSource

    instance = LokiLogSource(
        url="http://loki.golden-test.example.com",
        options={"session_factory": _CannedLokiFakeSession()},
    )

    cfg = ResolvedConfig(
        profile="test",
        sources={
            "loki-test": SourceConfig(
                adapter="loki",
                options={"url": "http://loki.golden-test.example.com"},
            )
        },
    )

    return {
        "_lumino_config": cfg,
        "_source_registry": build_registry(cfg),
        "_adapter_instances": {"loki-test": instance},
        "k8s_core_api": MagicMock(),
    }


LOKI_CASES: list[ToolCase] = [
    # ── loki-source: smart_summarize_pod_logs via LokiLogSource ─────────────
    # pod_name="*" is the glob (Entity selector) -- LokiLogSource compiles it
    # to {pod=~"*"} which is a valid LogQL regex-match selector.
    # source="loki-test" routes through _route_log_source -> LokiLogSource.
    # The canned 2-stream payload produces two stream groups in the envelope
    # (api-1 and api-2), proving the grouping_attr="stream" mechanism works.
    # k8s_core_api is a MagicMock; if read_namespaced_pod is called the
    # test fails, proving _quick_volume_estimate is hoisted above the adapter
    # branch in smart_summarize_pod_logs.
    ToolCase(
        name="smart_summarize_pod_logs",
        kwargs={"namespace": "loki-test-ns", "pod_name": "*",
                "source": "loki-test"},
        patches=_loki_source_patches(),
        id="loki-source",
    ),
]
