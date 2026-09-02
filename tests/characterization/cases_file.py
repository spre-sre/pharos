"""File-source characterization cases for the two wired log tools.

These cases rewire server._lumino_config, server._source_registry, and
server._adapter_instances so that smart_summarize_pod_logs and
stream_analyze_pod_logs use the FileLogSource adapter backed by the
committed fixtures in tests/fixtures/file_adapter/.

Integration: cases.py appends FILE_CASES to CASES at the bottom.  No
existing case ids are altered.  The circular import from cases.py is safe
because ToolCase is defined in cases.py before cases.py imports this file.

File-source golden ids (appended to case_id):
  smart_summarize_pod_logs-file-source   →  *.log (root-level fixtures)
  stream_analyze_pod_logs-file-source    →  mixed/*.log (sub-dir fixtures)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure src/ is on sys.path at collection time (the server fixture adds it
# later, but cases_file.py is imported during parametrize collection before
# any fixture runs).  Guard against duplicates from server fixture or
# concurrent test runs.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# ToolCase lives in cases.py.  The import works because Python's circular-
# import machinery returns the partially-initialised cases module here —
# ToolCase is defined near the top of cases.py, before cases.py imports this
# file.
from .cases import ToolCase  # noqa: E402 (circular — intentional, safe)

from core.config_types import ResolvedConfig, SourceConfig
from core.registry import build_registry

# Fixture directory — resolved to an absolute path at import time so that
# FileLogSource can call Path(root).resolve(strict=True) against a real
# directory.  The relpath invariant is preserved: every LogRecord.attributes
# ["file"] will be the path relative to FIXTURE_DIR (e.g. "plain.log",
# "mixed/app.log"), never an absolute path, so goldens are machine-independent.
FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "file_adapter"


def _file_source_patches() -> dict:
    """Return a fresh patches dict for a single file-source golden case.

    Patches are applied by test_golden_tools.py via monkeypatch.setattr:
      _lumino_config      – ResolvedConfig with a single "file-test" source
                            whose roots point at FIXTURE_DIR
      _source_registry    – AdapterRegistry built from that config
      _adapter_instances  – empty dict (factory cache starts cold per case)
      k8s_core_api        – MagicMock (MUST remain uncalled — proves the file
                            adapter branch bypasses all Kubernetes I/O; calling
                            it means _quick_volume_estimate leaked into the
                            adapter branch in smart_summarize_pod_logs)
    """
    cfg = ResolvedConfig(
        profile="test",
        sources={
            "file-test": SourceConfig(
                adapter="file",
                options={"roots": (str(FIXTURE_DIR),)},
            )
        },
    )
    return {
        "_lumino_config": cfg,
        "_source_registry": build_registry(cfg),
        "_adapter_instances": {},
        "k8s_core_api": MagicMock(),
    }


FILE_CASES: list[ToolCase] = [
    # ── file-source-1: smart_summarize_pod_logs via FileLogSource ────────────
    # pod_name="*.log" globs the three root-level fixtures (relpath-sorted):
    #   jsonlines.log, klog.log, plain.log
    # namespace="ignored" is noted in Provenance but not used by the adapter.
    # source="file-test" routes through _route_log_source → FileLogSource.
    # k8s_core_api is a MagicMock; if read_namespaced_pod is called, it means
    # _quick_volume_estimate was not hoisted above the adapter branch.
    ToolCase(
        name="smart_summarize_pod_logs",
        kwargs={"namespace": "ignored", "pod_name": "*.log",
                "source": "file-test"},
        patches=_file_source_patches(),
        id="file-source",
    ),

    # ── file-source-2: stream_analyze_pod_logs via FileLogSource ─────────────
    # pod_name="mixed/*.log" globs the two sub-directory fixtures (sorted):
    #   mixed/app.log, mixed/sidecar.log
    # This exercises multi-file envelope grouping (two relpath keys in the
    # legacy envelope → two groups in the result).
    ToolCase(
        name="stream_analyze_pod_logs",
        kwargs={"namespace": "ignored", "pod_name": "mixed/*.log",
                "source": "file-test"},
        patches=_file_source_patches(),
        id="file-source",
    ),

    # ── file-source-3: stream_analyze_pod_logs with TimeWindow start filter ──
    # pod_name="*.log" globs the three root-level fixtures (relpath-sorted):
    #   jsonlines.log, klog.log, plain.log
    # start_time="2026-01-01T00:00:02Z" triggers make_time_window, which sets
    # window.start and the FileLogSource adapter filters records by timestamp.
    #
    # Observable filtering (reviewed + executed against fixtures):
    #   - jsonlines.log: ts-00:00:01 dropped, ts-00:00:02 and ts-00:00:03 kept
    #     (2 of 3 records survive — the observable proof of TimeWindow being live)
    #   - klog.log: year-less header → _parse_klog yields timestamp=None → kept
    #     in full (undated records are always retained by the adapter)
    #   - plain.log: _parse_plain yields timestamp=None despite the ISO prefix
    #     in the body text (sniff.py:101-108 classifies it as plain, not jsonlines)
    #     → kept in full (undated)
    # The "undated records kept" Provenance note is NOT observable in tool output
    # (_logbatch_to_legacy_envelope reads only records, not Provenance metadata).
    ToolCase(
        name="stream_analyze_pod_logs",
        kwargs={"namespace": "ignored", "pod_name": "*.log",
                "source": "file-test", "start_time": "2026-01-01T00:00:02Z"},
        patches=_file_source_patches(),
        id="file-source-windowed",
    ),
]
