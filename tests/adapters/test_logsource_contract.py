"""Contract suite for the LogSource protocol (spec §7).

Four adapters are exercised against a shared parametrised contract:
  - KubernetesLogSource (backed by the idiomatic k8s fake from
    test_k8s_logs_fetch.py: patch get_all_pod_logs with AsyncMock)
  - FileLogSource (backed by tmp_path fixtures)
  - LokiLogSource (backed by a selector-aware session_factory fake)
  - ESLogSource (backed by a selector-aware session_factory fake, F7 from plan)

File-specific negatives are NOT parametrised: they test security
invariants (§4.7) that only the file adapter implements.

TDD order: tests written first, KubernetesLogSource implementation follows.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# KubernetesLogSource does not exist yet -> ImportError is the RED failure.
from adapters.kubernetes import logs as k8s_logs
from adapters.kubernetes.logs import KubernetesLogSource
from adapters.elasticsearch.logs import ESLogSource
from adapters.file.logs import FileLogSource
from adapters.file.roots import PathOutsideRoots
from adapters.loki.logs import LokiLogSource
from adapters.otlp.rings import LogRing
from adapters.otlp.logs import OtlpLogSource
from core.selector import Entity, Limit, TimeWindow
from core.signals import LogBatch, LogRecord


# ─── Loki contract fake ───────────────────────────────────────────────────────
#
# This fake session_factory is selector-aware and limit-aware (F7 from plan):
#   - If the captured query contains "existing-pod" → return >= 10 records
#   - Otherwise (miss) → return empty result
#   - Always respect the limit param (return <= requested)

_LOKI_HIT_PATTERN = "existing-pod"  # must appear in query for a hit
_LOKI_FIXED_TS_NS = "1735689600000000000"  # 2026-01-01T00:00:00Z (fixed)
_LOKI_STREAM_LABELS = {"pod": "existing-pod", "namespace": "contract-test"}


def _make_loki_hit_payload(limit: int) -> dict:
    """Build a Loki response with min(limit, 10) records from a single stream."""
    n = min(limit, 10)
    values = [
        [str(int(_LOKI_FIXED_TS_NS) + i * 1_000_000_000), f"loki-line-{i}"]
        for i in range(n)
    ]
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": _LOKI_STREAM_LABELS, "values": values}],
        },
    }


def _make_loki_empty_payload() -> dict:
    return {"status": "success", "data": {"resultType": "streams", "result": []}}


class _LokiContractFakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def json(self):
        return self._payload


class _LokiContractFakeSession:
    """Selector-aware and limit-aware fake for contract tests (F7 from plan).

    Usage: pass as options["session_factory"] when constructing LokiLogSource.
    Inspects params["query"] for the hit pattern, and params["limit"] for
    the max records to return.
    """

    def __call__(self, *a, timeout=None, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def request(self, method, url, *, params=None, json=None, headers=None, **kw):
        params = params or {}
        query = params.get("query", "")
        limit = int(params.get("limit", 100))
        if _LOKI_HIT_PATTERN in query:
            payload = _make_loki_hit_payload(limit)
        else:
            payload = _make_loki_empty_payload()
        return _LokiContractFakeResponse(payload)


# ─── Elasticsearch contract fake ─────────────────────────────────────────────
#
# Selector-aware and size-aware (F7 from plan):
#   - If the json body contains "existing-pod" anywhere in its term clauses
#     → return >= 10 hit records
#   - Otherwise → return empty hits
#   - Always respect the "size" field in the body (return <= requested)

_ES_HIT_PATTERN = "existing-pod"
_ES_FIXED_TS = "2026-01-01T00:00:00+00:00"
_ES_INDEX = "k8s-logs-contract-test"


def _make_es_hit_payload(size: int) -> dict:
    """Build an ES response with min(size, 10) hit records."""
    n = min(size, 10)
    hits = [
        {
            "_index": _ES_INDEX,
            "_source": {
                "@timestamp": _ES_FIXED_TS,
                "message": f"es-line-{i}",
                "pod_name": "existing-pod",
            },
        }
        for i in range(n)
    ]
    return {"hits": {"total": {"value": n}, "hits": hits}}


def _make_es_empty_payload() -> dict:
    return {"hits": {"total": {"value": 0}, "hits": []}}


class _ESContractFakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def json(self):
        return self._payload


class _ESContractFakeSession:
    """Selector-aware and size-aware fake for ES contract tests (F7 from plan).

    Usage: pass as options["session_factory"] when constructing ESLogSource.
    Inspects the json body for the hit pattern in term clauses, and uses
    body["size"] for the max records to return.
    """

    def __call__(self, *a, timeout=None, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def request(self, method, url, *, params=None, json=None, headers=None, **kw):
        import json as _json
        body = json or {}
        size = body.get("size", 500)
        # Check if any term in the filter refers to "existing-pod"
        body_str = _json.dumps(body)
        if _ES_HIT_PATTERN in body_str:
            payload = _make_es_hit_payload(size)
        else:
            payload = _make_es_empty_payload()
        return _ESContractFakeResponse(payload)


# ─── helpers ─────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _no_window() -> TimeWindow:
    return TimeWindow(start=None, end=None)


# ─── k8s fake idiom (mirrors test_k8s_logs_fetch.py) ────────────────────────

# Ten fake log lines so max_records tests have plenty to truncate.
_K8S_FAKE_LINES = [f"k8s-line-{i}" for i in range(10)]

# "Existing pod" returns lines (respecting tail_lines for the limit test).
# "Missing pod" returns the sentinel dict that fetch_pod_logs converts to
# an empty LogBatch.
async def _k8s_side_effect(pod_name, namespace, k8s_core_api,
                           tail_lines=None, since_seconds=None):
    if pod_name == "existing-pod":
        lines = _K8S_FAKE_LINES
        if tail_lines is not None:
            lines = lines[:tail_lines]
        return {"main": "\n".join(lines)}
    # Unknown pod → sentinel → empty batch in fetch_pod_logs
    return {"pod_error": f"pod {pod_name!r} not found"}


# ─── shared fixture ───────────────────────────────────────────────────────────


@dataclass
class SourceCase:
    """Container for a parametrised adapter case."""
    source: Any            # LogSource instance
    selector_hit: Entity   # selector that returns >= 10 records
    selector_miss: Entity  # selector that returns an empty batch (not exception)
    adapter_name: str      # expected provenance.adapter value


@pytest.fixture(params=["kubernetes", "file", "loki", "elasticsearch", "otlp"])
def source_case(request, tmp_path):
    """Yield a SourceCase for each adapter variant.

    For kubernetes: get_all_pod_logs is patched so no real cluster is needed.
    For file: a plain-text log file is written into tmp_path.
    For loki: a selector-aware session_factory fake is injected.
    """
    if request.param == "kubernetes":
        mock_gapl = AsyncMock(side_effect=_k8s_side_effect)
        with patch.object(k8s_logs, "get_all_pod_logs", new=mock_gapl):
            core_api = MagicMock()
            source = KubernetesLogSource(core_api, namespace="test-ns")
            yield SourceCase(
                source=source,
                selector_hit=Entity("existing-pod"),
                selector_miss=Entity("missing-pod"),
                adapter_name="kubernetes",
            )
    elif request.param == "file":
        # Write 10 plain-text lines so max_records tests have room to truncate.
        log_file = tmp_path / "app.log"
        log_file.write_text("\n".join(f"file-line-{i}" for i in range(10)) + "\n")
        source = FileLogSource((str(tmp_path),))
        yield SourceCase(
            source=source,
            selector_hit=Entity("app.log"),
            selector_miss=Entity("nonexistent.log"),
            adapter_name="file",
        )
    elif request.param == "loki":
        # loki: inject the selector-aware fake via session_factory option.
        # Hit selector matches _LOKI_HIT_PATTERN ("existing-pod") in the LogQL;
        # miss selector ("missing-pod") returns empty.
        source = LokiLogSource(
            url="http://loki.contract-test.example.com",
            options={"session_factory": _LokiContractFakeSession()},
        )
        yield SourceCase(
            source=source,
            selector_hit=Entity("existing-pod"),
            selector_miss=Entity("missing-pod"),
            adapter_name="loki",
        )
    elif request.param == "elasticsearch":
        # elasticsearch: inject the selector-aware fake via session_factory option.
        # Hit selector embeds "existing-pod" in the term body → >= 10 records.
        # Miss selector ("missing-pod") returns empty hits.
        source = ESLogSource(
            url="http://es.contract-test.example.com:9200",
            options={
                "index_pattern": "k8s-logs-*",
                "session_factory": _ESContractFakeSession(),
            },
        )
        yield SourceCase(
            source=source,
            selector_hit=Entity("existing-pod"),
            selector_miss=Entity("missing-pod"),
            adapter_name="elasticsearch",
        )
    else:
        # otlp: pre-seeded ring with 10 records of entity="existing-pod".
        # The ring is seeded with fixed recv_ts and body strings so contract
        # tests that count records get stable results.
        # Hit selector: Entity("existing-pod") → all 10 records match via fnmatch.
        # Miss selector: Entity("missing-pod") → 0 records match.
        ring = LogRing(capacity=100, start_ts=1.0)
        for i in range(10):
            rec = LogRecord(
                timestamp="2026-01-01T00:00:00Z",
                body=f"otlp-contract-line-{i}",
                attributes={"entity": "existing-pod"},
            )
            ring.append(float(i + 1), rec)
        source = OtlpLogSource(ring, {})
        yield SourceCase(
            source=source,
            selector_hit=Entity("existing-pod"),
            selector_miss=Entity("missing-pod"),
            adapter_name="otlp",
        )


# ─── contract tests (parametrised over both adapters) ─────────────────────────


def test_fetch_logs_returns_logbatch(source_case):
    """fetch_logs always returns a LogBatch instance regardless of adapter."""
    batch = _run(source_case.source.fetch_logs(
        source_case.selector_hit, _no_window(), None))
    assert isinstance(batch, LogBatch), (
        f"expected LogBatch, got {type(batch).__name__}")


def test_provenance_adapter_name(source_case):
    """provenance.adapter reports the expected adapter name string."""
    batch = _run(source_case.source.fetch_logs(
        source_case.selector_hit, _no_window(), None))
    assert batch.provenance.adapter == source_case.adapter_name, (
        f"expected adapter={source_case.adapter_name!r}, "
        f"got {batch.provenance.adapter!r}")


def test_all_records_have_str_body(source_case):
    """Every LogRecord in the batch has a str body (never None or other type)."""
    batch = _run(source_case.source.fetch_logs(
        source_case.selector_hit, _no_window(), None))
    assert batch.records, "expected non-empty batch for hit selector"
    for record in batch.records:
        assert isinstance(record.body, str), (
            f"record body is {type(record.body).__name__!r}, expected str")


def test_missing_target_returns_empty_batch(source_case):
    """Missing target: empty LogBatch returned, NOT an exception."""
    batch = _run(source_case.source.fetch_logs(
        source_case.selector_miss, _no_window(), None))
    assert isinstance(batch, LogBatch)
    assert batch.records == [], (
        f"expected empty records for miss selector, got {len(batch.records)} records")


def test_max_records_limit_honored(source_case):
    """Limit(max_records=N) → batch contains at most N records."""
    limit = Limit(max_records=5)
    batch = _run(source_case.source.fetch_logs(
        source_case.selector_hit, _no_window(), limit))
    assert len(batch.records) <= 5, (
        f"expected at most 5 records, got {len(batch.records)}")


# ─── file-specific negatives (NOT parametrised) ───────────────────────────────


def test_file_escape_pattern_raises(tmp_path):
    """An exact non-glob escape pattern (e.g. '../outside.log') raises PathOutsideRoots."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    outside = tmp_path / "secret.log"
    outside.write_text("sensitive content\n")

    source = FileLogSource((str(root_dir),))
    with pytest.raises(PathOutsideRoots):
        _run(source.fetch_logs(Entity("../secret.log"), _no_window(), None))


def test_file_absolute_pattern_raises(tmp_path):
    """An absolute path pattern raises PathOutsideRoots immediately."""
    source = FileLogSource((str(tmp_path),))
    absolute_pattern = str(tmp_path / "something.log")
    with pytest.raises(PathOutsideRoots):
        _run(source.fetch_logs(Entity(absolute_pattern), _no_window(), None))


def test_file_symlink_out_glob_excluded(tmp_path):
    """A symlink inside the root that resolves outside is silently excluded from glob results."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()

    # A real log file inside the root
    (root_dir / "real.log").write_text("real-content\n")

    # An outside file with distinguishable content
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.log"
    outside_file.write_text("should-never-appear\n")

    # Symlink inside the root pointing outside
    link = root_dir / "evil_link.log"
    link.symlink_to(outside_file)

    source = FileLogSource((str(root_dir),))
    batch = _run(source.fetch_logs(Entity("*.log"), _no_window(), None))

    # real.log records appear; outside content must NOT appear
    bodies = [r.body for r in batch.records]
    assert "real-content" in bodies
    assert not any("should-never-appear" in b for b in bodies), (
        "outside symlink content leaked into batch")


def test_file_symlink_out_exact_raises(tmp_path):
    """An exact (non-glob) path naming a symlink that resolves outside raises PathOutsideRoots."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.log"
    outside_file.write_text("sensitive\n")

    link = root_dir / "evil_link.log"
    link.symlink_to(outside_file)

    source = FileLogSource((str(root_dir),))
    with pytest.raises(PathOutsideRoots):
        _run(source.fetch_logs(Entity("evil_link.log"), _no_window(), None))


def test_file_no_mutation_sha256(tmp_path):
    """Fetching logs does not mutate the fixture tree (SHA-256 of tree is stable)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "a.log").write_text("line one\nline two\n")
    (log_dir / "b.log").write_text("line three\nline four\n")

    def _tree_sha256(directory: Path) -> str:
        h = hashlib.sha256()
        for f in sorted(directory.rglob("*")):
            if f.is_file() and not f.is_symlink():
                rel = str(f.relative_to(directory))
                h.update(rel.encode())
                h.update(f.read_bytes())
        return h.hexdigest()

    sha_before = _tree_sha256(log_dir)

    source = FileLogSource((str(log_dir),))
    _run(source.fetch_logs(Entity("*.log"), _no_window(), Limit(max_records=1000)))

    sha_after = _tree_sha256(log_dir)

    assert sha_before == sha_after, (
        "fixture tree was mutated by fetch_logs (SHA-256 changed)")


def test_file_write_verb_source_tripwire():
    """No write-verb patterns exist in src/adapters/file/ source code.

    Scans all .py files under src/adapters/file/ for mutation primitives:
    open(..., 'w'/'a'), .write_text, .write_bytes, .write(, .unlink,
    .rename, os.remove, os.unlink, .rmdir.  Comments are NOT excluded so
    that even commented-out write code is flagged.
    """
    adapter_src = Path(__file__).resolve().parents[2] / "src" / "adapters" / "file"

    # Patterns from the spec §4.7 tripwire list.
    forbidden = re.compile(
        r"""open\s*\([^)]*['"][wa]['"]"""  # open(..., "w") / open(..., "a")
        r"""|\.write_text\s*\("""          # .write_text(
        r"""|\.write_bytes\s*\("""         # .write_bytes(
        r"""|(?<!\w)\.write\s*\("""        # .write( (not part of a larger word)
        r"""|\bunlink\s*\("""              # unlink(
        r"""|\brename\s*\("""              # rename(
        r"""|os\.remove\s*\("""            # os.remove(
        r"""|os\.unlink\s*\("""            # os.unlink(
        r"""|\.rmdir\s*\("""               # .rmdir(
    )

    violations: list[str] = []
    for py_file in sorted(adapter_src.glob("*.py")):
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            if forbidden.search(line):
                violations.append(f"{py_file.name}:{lineno}: {line.rstrip()}")

    assert not violations, (
        "write-verb detected in src/adapters/file/ source code "
        "(mutations are forbidden; see spec §4.7):\n"
        + "\n".join(violations)
    )


def test_remote_adapter_write_verb_tripwire():
    """No write-verb patterns exist in the remote adapter source files.

    Extends the file-adapter tripwire (above) to cover:
    - src/adapters/loki/     (all .py files)
    - src/adapters/elasticsearch/  (all .py files)
    - src/adapters/http.py

    The HTTP-mutation checks include both generic write verbs (from the file-adapter
    tripwire set) and ES-specific bulk-mutation endpoints that must NEVER appear in
    the read-only adapter code:
    - _delete_by_query
    - _update_by_query
    - _bulk

    Comments are NOT excluded: even commented-out mutation code is flagged.
    """
    repo = Path(__file__).resolve().parents[2]
    src_adapters = repo / "src" / "adapters"

    # Collect all target files.
    target_files: list[Path] = []
    for subdir in ("loki", "elasticsearch"):
        target_files.extend(sorted((src_adapters / subdir).glob("*.py")))
    target_files.append(src_adapters / "http.py")

    # Generic write-verb pattern (same as file-adapter tripwire).
    write_verbs = re.compile(
        r"""open\s*\([^)]*['"][wa]['"]"""  # open(..., "w") / open(..., "a")
        r"""|\.write_text\s*\("""          # .write_text(
        r"""|\.write_bytes\s*\("""         # .write_bytes(
        r"""|(?<!\w)\.write\s*\("""        # .write( (not part of a larger word)
        r"""|\bunlink\s*\("""              # unlink(
        r"""|\brename\s*\("""              # rename(
        r"""|os\.remove\s*\("""            # os.remove(
        r"""|os\.unlink\s*\("""            # os.unlink(
        r"""|\.rmdir\s*\("""               # .rmdir(
    )

    # ES-specific forbidden endpoint strings (must never appear in source text).
    es_forbidden_strings = ("_delete_by_query", "_update_by_query", "_bulk")

    violations: list[str] = []
    for py_file in target_files:
        text = py_file.read_text()
        rel = py_file.relative_to(repo)
        for lineno, line in enumerate(text.splitlines(), 1):
            if write_verbs.search(line):
                violations.append(
                    f"{rel}:{lineno}: [write-verb] {line.rstrip()}"
                )
            for es_str in es_forbidden_strings:
                if es_str in line:
                    violations.append(
                        f"{rel}:{lineno}: [forbidden-ES-endpoint '{es_str}'] {line.rstrip()}"
                    )

    assert not violations, (
        "Forbidden pattern detected in remote adapter source "
        "(write verbs and ES mutation endpoints are forbidden; see spec §4.7):\n"
        + "\n".join(violations)
    )


# ─── KubernetesLogSource: carry-in tests from Task-3 review ──────────────────
# These tests were identified as missing during the Task-3 code review.
# The implementation exists; these add the safety net.


def test_k8s_log_source_matchers_raises_selector_not_supported():
    """KubernetesLogSource rejects Matchers with SelectorNotSupported."""
    from core.selector import Matchers, SelectorNotSupported
    mock_core_api = MagicMock()
    source = KubernetesLogSource(mock_core_api, namespace="test-ns")
    import pytest as _pytest
    with _pytest.raises(SelectorNotSupported):
        _run(source.fetch_logs(Matchers({"app": "myapp"}), _no_window(), None))


def test_k8s_log_source_native_raises_selector_not_supported():
    """KubernetesLogSource rejects Native with SelectorNotSupported."""
    from core.selector import Native, SelectorNotSupported
    mock_core_api = MagicMock()
    source = KubernetesLogSource(mock_core_api, namespace="test-ns")
    import pytest as _pytest
    with _pytest.raises(SelectorNotSupported):
        _run(source.fetch_logs(Native("kubectl logs ..."), _no_window(), None))


def test_k8s_log_source_past_window_start_forwards_positive_since_seconds():
    """A window.start in the past produces a POSITIVE since_seconds in the fetch call.

    Capture via AsyncMock on the module-level get_all_pod_logs so we can
    inspect the kwarg without touching the kubernetes client.
    """
    from datetime import datetime, timedelta, timezone
    from core.selector import Entity, TimeWindow
    past_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window = TimeWindow(start=past_start)

    captured = {}

    async def _capturing_gapl(pod_name, namespace, k8s_core_api,
                               tail_lines=None, since_seconds=None):
        captured["since_seconds"] = since_seconds
        return {"main": "some-log-line"}

    mock_gapl = AsyncMock(side_effect=_capturing_gapl)
    with patch.object(k8s_logs, "get_all_pod_logs", new=mock_gapl):
        source = KubernetesLogSource(MagicMock(), namespace="test-ns")
        _run(source.fetch_logs(Entity("existing-pod"), window, None))

    assert "since_seconds" in captured, "get_all_pod_logs was not called"
    assert isinstance(captured["since_seconds"], int), (
        f"expected int since_seconds, got {type(captured['since_seconds'])}"
    )
    assert captured["since_seconds"] > 0, (
        f"expected positive since_seconds for past window.start, "
        f"got {captured['since_seconds']}"
    )
