"""Elasticsearch-source characterization cases for the two wired log tools.

These cases wire server._adapter_instances, _lumino_config, and
_source_registry so that smart_summarize_pod_logs uses the ESLogSource
adapter backed by a canned 2-hit session_factory (FIXED ISO timestamps).

Integration: cases.py appends ES_CASES to CASES at the bottom.  No
existing case ids are altered.  The circular import from cases.py is safe
because ToolCase is defined in cases.py before cases.py imports this file.

ES source golden id:
  smart_summarize_pod_logs-es-source  ->  new golden file
"""
from __future__ import annotations

import json
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


# ─── Canned 2-hit ES payload ─────────────────────────────────────────────────
#
# Fixed ISO timestamps so the golden is machine-independent and
# double-regen stable.
#
# Hit 1: api-1 pod, log line from index k8s-logs-2026.01.01
# Hit 2: api-2 pod, log line from index k8s-logs-2026.01.01
#
# Using fixed timestamps: 2026-01-01T00:00:00+00:00 and 2026-01-01T01:00:00+00:00

_INDEX = "k8s-logs-2026.01.01"
_TS_1 = "2026-01-01T00:00:00+00:00"
_TS_2 = "2026-01-01T01:00:00+00:00"

_CANNED_ES_PAYLOAD = {
    "hits": {
        "total": {"value": 2, "relation": "eq"},
        "hits": [
            {
                "_index": _INDEX,
                "_source": {
                    "@timestamp": _TS_1,
                    "message": "api-1 INFO: server started",
                    "kubernetes.pod_name": "api-1",
                    "namespace": "es-test-ns",
                },
            },
            {
                "_index": _INDEX,
                "_source": {
                    "@timestamp": _TS_2,
                    "message": "api-2 ERROR: connection refused",
                    "kubernetes.pod_name": "api-2",
                    "namespace": "es-test-ns",
                },
            },
        ],
    }
}


# ─── Fake session with fixed canned payload ───────────────────────────────────


class _CannedESResponse:
    """Fake aiohttp response returning the canned 2-hit ES payload."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: Any):
        pass

    async def json(self) -> dict:
        return self._payload


class _CannedESFakeSession:
    """Fake session_factory: always returns the fixed canned payload.

    This is injected via options["session_factory"] when constructing
    ESLogSource.  It does NOT contact a real Elasticsearch instance.

    The golden is stable because:
    - the payload is a module-level constant (not time-dependent)
    - the ISO timestamps in the payload are fixed strings
    """

    def __call__(self, *a: Any, timeout: Any = None, **kw: Any) -> "_CannedESFakeSession":
        return self

    async def __aenter__(self) -> "_CannedESFakeSession":
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
    ) -> _CannedESResponse:
        return _CannedESResponse(_CANNED_ES_PAYLOAD)


# ─── Pre-seeded patches for the ES golden case ───────────────────────────────


def _es_source_patches() -> dict:
    """Return a fresh patches dict for a single ES-source golden case.

    Patches applied by test_golden_tools.py via monkeypatch.setattr:
      _lumino_config       -- ResolvedConfig with a single "es-test" source
      _source_registry     -- AdapterRegistry built from that config
      _adapter_instances   -- pre-seeded with the real ESLogSource instance
                              (canned session_factory baked in); this bypasses
                              the factory cache and avoids needing a real url
      k8s_core_api         -- MagicMock (must remain uncalled -- proves the
                              ES adapter branch bypasses k8s I/O entirely)

    The _adapter_instances pre-seed is identical to the loki-source pattern
    (cases_loki.py): build a REAL ESLogSource with the canned session_factory
    threaded through options, pre-seed _adapter_instances to bypass the factory
    cache, and patch _lumino_config/_source_registry to match.
    """
    from adapters.elasticsearch.logs import ESLogSource

    instance = ESLogSource(
        url="http://es.golden-test.example.com:9200",
        options={
            "index_pattern": "k8s-logs-*",
            "session_factory": _CannedESFakeSession(),
        },
    )

    cfg = ResolvedConfig(
        profile="test",
        sources={
            "es-test": SourceConfig(
                adapter="elasticsearch",
                options={
                    "url": "http://es.golden-test.example.com:9200",
                    "index_pattern": "k8s-logs-*",
                },
            )
        },
    )

    return {
        "_lumino_config": cfg,
        "_source_registry": build_registry(cfg),
        "_adapter_instances": {"es-test": instance},
        "k8s_core_api": MagicMock(),
    }


ES_CASES: list[ToolCase] = [
    # ── es-source: smart_summarize_pod_logs via ESLogSource ─────────────────
    # pod_name="*" is the entity pattern — ESLogSource compiles it to a
    # term clause on kubernetes.pod_name.  source="es-test" routes through
    # _route_log_source -> ESLogSource.
    # The canned 2-hit payload groups both records under the same index
    # (k8s-logs-2026.01.01), proving the grouping_attr="index" mechanism.
    # k8s_core_api is a MagicMock; if read_namespaced_pod is called the
    # test fails, proving _quick_volume_estimate is hoisted above the adapter
    # branch in smart_summarize_pod_logs.
    ToolCase(
        name="smart_summarize_pod_logs",
        kwargs={"namespace": "es-test-ns", "pod_name": "*",
                "source": "es-test"},
        patches=_es_source_patches(),
        id="es-source",
    ),
]
