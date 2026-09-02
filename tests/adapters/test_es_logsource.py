"""Unit tests for src/adapters/elasticsearch/logs.py — ESLogSource.

TDD order: tests written FIRST, implementation follows.
All HTTP is faked via the session_factory injection seam (NOT module patching).

Design notes (from plan/phase4 Task 4):
- Entity → {entity_query: {entity_field: pattern}} (default entity_query="term";
  F5: entity_field is assumed keyword-mapped; "match" is an escape hatch)
- Matchers → bool.filter of term clauses (one per key, sorted for determinism)
- Native → the dict body ITSELF parsed from the JSON string; JSON-encoding
  structurally neutralizes injection (the documented contrast to loki's
  string interpolation where we had to escape backslash+quote)
- Window → range on timestamp_field merged into bool.filter
- size = min(limit.max_records or 500, 1000 hard cap)
- BEFORE the request: enforce_body_limits(body) THEN _reject_scripts(body);
  script/script_score keys in Native bodies → AdapterError with ZERO requests made
- POST /{index_pattern}/_search ONLY (endpoint allowlist asserted)
- Parse hits.hits[] → LogRecord(timestamp_field, message_field, level_field,
  attributes=_source minus message + "index"=hit._index)
- Provenance(adapter="elasticsearch", grouping_attr="index")
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.errors import AdapterError
from core.selector import Entity, Limit, Matchers, Native, TimeWindow
from adapters.http import HttpAdapterError


# ─── fake session infrastructure ─────────────────────────────────────────────


class _FakeResponse:
    """Minimal aiohttp response stand-in."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    async def json(self) -> dict:
        return self._payload


class _FakeSession:
    """Fake aiohttp.ClientSession injected via session_factory.

    Captures all calls; returns canned payloads.
    Matches the idiom from test_loki_logsource.py and test_http_helper.py.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        payload: dict | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._status = status
        self._payload = payload if payload is not None else {}
        self._raise_exc = raise_exc
        self.captured_timeout: Any = None

    def __call__(self, *a: Any, timeout: Any = None, **kw: Any) -> "_FakeSession":
        self.captured_timeout = timeout
        return self

    async def __aenter__(self) -> "_FakeSession":
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
    ) -> "_FakeResponse":
        if self._raise_exc is not None:
            raise self._raise_exc
        call = {"method": method, "url": url, "params": params, "json": json}
        self.calls.append(call)
        return _FakeResponse(self._status, self._payload)


def _run(coro):
    return asyncio.run(coro)


# ─── helpers for test data ────────────────────────────────────────────────────

_BASE_URL = "http://es.example.com:9200"
_INDEX_PATTERN = "k8s-logs-*"
_SEARCH_URL = f"{_BASE_URL}/{_INDEX_PATTERN}/_search"

# Fixed ISO timestamps for deterministic parse tests
_TS_1 = "2026-01-01T00:00:00+00:00"
_TS_2 = "2026-01-01T01:00:00+00:00"


def _es_hit(ts: str, msg: str, level: str | None = None, index: str = "k8s-logs-2026.01.01",
             **extra_fields) -> dict:
    """Build a minimal ES hits.hits[] entry."""
    source = {"@timestamp": ts, "message": msg, **extra_fields}
    if level is not None:
        source["level"] = level
    return {"_index": index, "_source": source}


def _es_payload(*hits) -> dict:
    """Build a minimal ES _search response."""
    return {"hits": {"total": {"value": len(hits)}, "hits": list(hits)}}


def _make_source(fake: _FakeSession, **opts) -> Any:
    """Build an ESLogSource with the given fake session injected."""
    from adapters.elasticsearch.logs import ESLogSource

    options = {
        "index_pattern": _INDEX_PATTERN,
        "session_factory": fake,
        **opts,
    }
    return ESLogSource(url=_BASE_URL, options=options)


def _no_window() -> TimeWindow:
    return TimeWindow(start=None, end=None)


def _get_body(fake: _FakeSession) -> dict:
    """Return the JSON body from the first captured request."""
    assert fake.calls, "no request was made"
    return fake.calls[0]["json"]


# ─── Constructor validation ───────────────────────────────────────────────────


def test_missing_index_pattern_raises_adapter_error():
    """ESLogSource requires index_pattern; missing → AdapterError at construction."""
    from adapters.elasticsearch.logs import ESLogSource

    with pytest.raises(AdapterError, match="index_pattern"):
        ESLogSource(url=_BASE_URL, options={})


def test_missing_url_raises_adapter_error():
    """ESLogSource is instantiated with url; empty url → AdapterError."""
    from adapters.elasticsearch.logs import ESLogSource

    with pytest.raises(AdapterError, match="url"):
        ESLogSource(url="", options={"index_pattern": _INDEX_PATTERN})


# ─── Body compilation — Entity ────────────────────────────────────────────────


def test_entity_selector_builds_term_query_by_default():
    """Entity → {"term": {entity_field: pattern}} in bool.filter (default entity_query='term')."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("my-pod"), _no_window(), None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    entity_clauses = [f for f in filters if "term" in f]
    assert entity_clauses, f"no term clause in filter: {filters!r}"
    term_clause = entity_clauses[0]["term"]
    # entity_field default is "kubernetes.pod_name"
    assert "kubernetes.pod_name" in term_clause, (
        f"expected kubernetes.pod_name in term clause: {term_clause!r}"
    )
    assert term_clause["kubernetes.pod_name"] == "my-pod", (
        f"expected term value 'my-pod', got {term_clause['kubernetes.pod_name']!r}"
    )


def test_entity_selector_custom_entity_field():
    """entity_field option overrides default 'kubernetes.pod_name'."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake, entity_field="host.name")
    _run(source.fetch_logs(Entity("node-01"), _no_window(), None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    term_clauses = [f["term"] for f in filters if "term" in f]
    assert any("host.name" in tc for tc in term_clauses), (
        f"expected host.name in term clauses: {term_clauses!r}"
    )


def test_entity_selector_match_escape_hatch():
    """entity_query='match' → uses 'match' clause instead of 'term'."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake, entity_query="match")
    _run(source.fetch_logs(Entity("my-pod"), _no_window(), None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    match_clauses = [f for f in filters if "match" in f]
    assert match_clauses, f"expected match clause (escape hatch), got: {filters!r}"


# ─── Body compilation — Matchers ─────────────────────────────────────────────


def test_matchers_selector_builds_term_clauses():
    """Matchers → bool.filter with one term clause per key."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(
        Matchers({"app": "frontend", "env": "prod"}), _no_window(), None
    ))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    term_fields = {}
    for f in filters:
        if "term" in f:
            term_fields.update(f["term"])
    assert term_fields.get("app") == "frontend", f"app missing: {term_fields!r}"
    assert term_fields.get("env") == "prod", f"env missing: {term_fields!r}"


def test_matchers_selector_sorted_by_key():
    """Matchers term clauses appear in sorted key order in the filter list."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(
        Matchers({"zzz": "last", "aaa": "first"}), _no_window(), None
    ))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    term_keys = [list(f["term"].keys())[0] for f in filters if "term" in f]
    if len(term_keys) >= 2:
        assert term_keys == sorted(term_keys), (
            f"term clauses not sorted: {term_keys!r}"
        )


# ─── Body compilation — Native ────────────────────────────────────────────────


def test_native_selector_body_passthrough():
    """Native → the dict body from the JSON string is used as-is (plus size + window)."""
    native_body = {
        "query": {"bool": {"filter": [{"term": {"level": "error"}}]}},
    }
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Native(json.dumps(native_body)), _no_window(), None))
    body = _get_body(fake)
    # The filter from the native body must be present
    filters = body["query"]["bool"]["filter"]
    level_clauses = [f for f in filters if "term" in f and "level" in f["term"]]
    assert level_clauses, f"native body filter not preserved: {filters!r}"


def test_native_selector_size_is_capped():
    """Native body: size is set/capped by the adapter (not taken from the body)."""
    native_body = {"query": {"match_all": {}}, "size": 9999}
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Native(json.dumps(native_body)), _no_window(), Limit(max_records=200)))
    body = _get_body(fake)
    assert body.get("size") == 200, (
        f"expected size=200 (capped by limit), got {body.get('size')!r}"
    )


# ─── Window range merged into bool.filter ─────────────────────────────────────


def test_window_range_merged_with_gte_and_lte():
    """Window start+end → range clause on timestamp_field in bool.filter."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    window = TimeWindow(
        start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    _run(source.fetch_logs(Entity("pod"), window, None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    range_clauses = [f for f in filters if "range" in f]
    assert range_clauses, f"no range clause in filter: {filters!r}"
    range_body = range_clauses[0]["range"]
    assert "@timestamp" in range_body, f"@timestamp not in range: {range_body!r}"
    assert "gte" in range_body["@timestamp"], "gte missing from range"
    assert "lte" in range_body["@timestamp"], "lte missing from range"


def test_window_start_only_adds_gte():
    """Window with start only → only 'gte' in the range clause."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    window = TimeWindow(start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _run(source.fetch_logs(Entity("pod"), window, None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    range_clauses = [f for f in filters if "range" in f]
    assert range_clauses, f"no range clause for start-only window"
    range_ts = range_clauses[0]["range"]["@timestamp"]
    assert "gte" in range_ts, "gte missing from start-only range"
    assert "lte" not in range_ts, "unexpected lte in start-only range"


def test_window_end_only_adds_lte():
    """Window with end only → only 'lte' in the range clause."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    window = TimeWindow(end=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    _run(source.fetch_logs(Entity("pod"), window, None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    range_clauses = [f for f in filters if "range" in f]
    assert range_clauses, f"no range clause for end-only window"
    range_ts = range_clauses[0]["range"]["@timestamp"]
    assert "lte" in range_ts, "lte missing from end-only range"
    assert "gte" not in range_ts, "unexpected gte in end-only range"


def test_no_window_has_no_range_clause():
    """No window → no range clause in the filter list."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    range_clauses = [f for f in filters if "range" in f]
    assert not range_clauses, f"unexpected range clause: {range_clauses!r}"


def test_custom_timestamp_field():
    """timestamp_field option → range clause uses the custom field name."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake, timestamp_field="event.created")
    window = TimeWindow(start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _run(source.fetch_logs(Entity("pod"), window, None))
    body = _get_body(fake)
    filters = body["query"]["bool"]["filter"]
    range_clauses = [f for f in filters if "range" in f]
    assert range_clauses, "no range clause"
    assert "event.created" in range_clauses[0]["range"], (
        f"custom timestamp_field not used: {range_clauses[0]['range']!r}"
    )


# ─── Size cap ─────────────────────────────────────────────────────────────────


def test_size_defaults_to_500():
    """No limit → size defaults to 500."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    body = _get_body(fake)
    assert body.get("size") == 500, f"expected size=500, got {body.get('size')!r}"


def test_size_from_limit():
    """Limit(max_records=N) → size=N in body."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), Limit(max_records=42)))
    body = _get_body(fake)
    assert body.get("size") == 42, f"expected size=42, got {body.get('size')!r}"


def test_size_hard_capped_at_1000():
    """Limit(max_records > 1000) → size is hard-capped at 1000."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), Limit(max_records=5000)))
    body = _get_body(fake)
    assert body.get("size") == 1000, (
        f"expected size hard-capped at 1000, got {body.get('size')!r}"
    )


def test_size_exactly_1000_is_not_capped():
    """Limit(max_records=1000) → size=1000 (at the cap, not over)."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), Limit(max_records=1000)))
    body = _get_body(fake)
    assert body.get("size") == 1000


# ─── Script rejection (§4.7) — ZERO requests captured ─────────────────────────


def _make_script_native(script_body: dict) -> Native:
    """Wrap an ES body dict with a forbidden script key as a Native selector."""
    return Native(json.dumps(script_body))


def test_native_with_top_level_script_raises_before_request():
    """Native body with top-level 'script' key → AdapterError, zero requests."""
    body_with_script = {
        "query": {"match_all": {}},
        "script": {"source": "doc['price'].value * 2", "lang": "painless"},
    }
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(_make_script_native(body_with_script), _no_window(), None))
    assert len(fake.calls) == 0, (
        f"request was made despite script rejection: {fake.calls}"
    )


def test_native_with_nested_script_raises_before_request():
    """Native body with 'script' nested inside 'query' → AdapterError, zero requests."""
    body_with_nested = {
        "query": {
            "bool": {
                "filter": [
                    {"script": {"script": {"source": "evil"}}}
                ]
            }
        }
    }
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(_make_script_native(body_with_nested), _no_window(), None))
    assert len(fake.calls) == 0, (
        f"request was made despite nested script rejection: {fake.calls}"
    )


def test_native_with_list_buried_script_raises_before_request():
    """Native body with 'script' buried inside a list → AdapterError, zero requests."""
    body_with_list_buried = {
        "query": {
            "bool": {
                "should": [
                    {"match": {"message": "error"}},
                    {"script_score": {"query": {"match_all": {}}, "script": {"source": "score"}}},
                ]
            }
        }
    }
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(_make_script_native(body_with_list_buried), _no_window(), None))
    assert len(fake.calls) == 0, (
        f"request was made despite list-buried script rejection: {fake.calls}"
    )


def test_native_with_script_score_key_raises_before_request():
    """Native body with 'script_score' key → AdapterError, zero requests."""
    body_with_script_score = {
        "query": {
            "script_score": {
                "query": {"match_all": {}},
                "script": {"source": "Math.log(2 + doc['likes'].value)"},
            }
        }
    }
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(_make_script_native(body_with_script_score), _no_window(), None))
    assert len(fake.calls) == 0, (
        f"request made despite script_score rejection: {fake.calls}"
    )


def test_clean_native_body_makes_request():
    """Native body with no forbidden keys → request proceeds normally."""
    clean_body = {
        "query": {"bool": {"filter": [{"term": {"level": "error"}}]}},
    }
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Native(json.dumps(clean_body)), _no_window(), None))
    assert len(fake.calls) == 1, "expected exactly one request for clean native body"


# ─── Oversized body rejected pre-request ──────────────────────────────────────


def test_oversized_native_body_rejected_before_request():
    """Native body > 64 KB → AdapterError from enforce_body_limits, zero requests."""
    large_body = {"query": {"match_all": {}}, "data": "x" * 70_000}
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(Native(json.dumps(large_body)), _no_window(), None))
    assert len(fake.calls) == 0, (
        f"request made despite oversized body: {fake.calls}"
    )


# ─── RecursionError carry-in: deep-body → AdapterError from ESLogSource ───────


def _deep_native(depth: int) -> Native:
    """Native selector wrapping a depth-deep body as a JSON string.

    Uses depth=150 by default so that:
    - json.dumps in this test setup succeeds (150 is far below CPython's C-level
      json recursion limit, which is much higher than the Python stack limit)
    - The _reject_scripts depth guard fires at > 100 → AdapterError is raised
      before any request is made
    """
    body: dict = {"query": {"leaf": "value"}}
    for _ in range(depth):
        body = {"nested": body}
    return Native(json.dumps(body))


def test_deep_native_body_raises_adapter_error_not_recursion():
    """ESLogSource: deeply-nested native body (150 levels) → AdapterError (carry-in fix).

    The _reject_scripts depth guard fires at nesting > 100, so a 150-level body:
    - Triggers AdapterError (depth guard) before any request is made
    - Proves that ESLogSource wraps deep-body failures as AdapterError (not RecursionError)

    Note: The http-level tests use 1500-deep dicts (no json.dumps in setup).
    For Native selectors, we use 150 because json.dumps is called in test setup
    to build the JSON string; 150 is well below CPython's C-level json recursion
    limit but well above our 100-level depth guard.
    """
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(_deep_native(150), _no_window(), None))
    # Extra assertion: must NOT propagate RecursionError
    try:
        _run(source.fetch_logs(_deep_native(150), _no_window(), None))
    except AdapterError:
        pass  # correct — depth guard raised AdapterError
    except RecursionError:
        pytest.fail(
            "ESLogSource raised RecursionError from deep native body — "
            "the depth guard / RecursionError wrap is not reaching the adapter"
        )
    # The fake must have received NO requests (rejected pre-request)
    assert len(fake.calls) == 0, (
        f"requests were made despite deep-body rejection: {fake.calls}"
    )


# ─── PATH ALLOWLIST: every URL ends /_search ──────────────────────────────────


def test_all_urls_end_with_search_path():
    """Every URL the adapter requests ends /{index_pattern}/_search.

    Collect all URLs across multiple selector types and window/limit combos,
    then assert the full URL set is a subset of the single allowed endpoint.
    """
    from adapters.elasticsearch.logs import ESLogSource

    all_urls: list[str] = []

    class _UrlCapturingFakeSession(_FakeSession):
        def request(self, method, url, *, params=None, json=None, headers=None, **kw):
            all_urls.append(url)
            return super().request(method, url, params=params, json=json,
                                   headers=headers, **kw)

    allowed = {f"{_BASE_URL}/{_INDEX_PATTERN}/_search"}
    scenarios = [
        (Entity("pod"), _no_window(), None),
        (Entity("pod"), TimeWindow(start=datetime(2026, 1, 1, tzinfo=timezone.utc)), None),
        (Matchers({"app": "web"}), _no_window(), None),
        (Matchers({"app": "web"}), _no_window(), Limit(max_records=50)),
    ]

    for sel, win, lim in scenarios:
        fake = _UrlCapturingFakeSession(payload=_es_payload())
        source = ESLogSource(
            url=_BASE_URL,
            options={"index_pattern": _INDEX_PATTERN, "session_factory": fake},
        )
        _run(source.fetch_logs(sel, win, lim))

    unknown = [u for u in all_urls if u not in allowed]
    assert not unknown, (
        f"URLs outside /_search allowlist captured: {unknown!r}\n"
        f"Allowed: {sorted(allowed)}"
    )


def test_request_method_is_post():
    """ES queries use POST, never GET."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert fake.calls[0]["method"] == "POST", (
        f"expected POST, got {fake.calls[0]['method']!r}"
    )


# ─── Parsing — LogRecord production ───────────────────────────────────────────


def test_parse_produces_logrecords():
    """hits.hits[] → one LogRecord per hit."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "first log line"),
        _es_hit(_TS_2, "second log line"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert len(batch.records) == 2
    bodies = {r.body for r in batch.records}
    assert "first log line" in bodies
    assert "second log line" in bodies


def test_parse_timestamp_from_timestamp_field():
    """LogRecord.timestamp is read from @timestamp (or custom timestamp_field)."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "a log line"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.records[0].timestamp == _TS_1, (
        f"expected {_TS_1!r}, got {batch.records[0].timestamp!r}"
    )


def test_parse_severity_from_level_field():
    """LogRecord.severity is read from 'level' field when present."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "error log", level="ERROR"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.records[0].severity == "ERROR", (
        f"expected severity='ERROR', got {batch.records[0].severity!r}"
    )


def test_parse_severity_none_when_level_missing():
    """LogRecord.severity is None when 'level' field is absent in _source."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "no level field"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.records[0].severity is None, (
        f"expected severity=None, got {batch.records[0].severity!r}"
    )


def test_parse_attributes_exclude_message_field():
    """attributes does NOT include the message field (it's the body)."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "a log", kubernetes_namespace="prod"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    attrs = batch.records[0].attributes
    assert "message" not in attrs, f"message should not appear in attributes: {attrs!r}"


def test_parse_attributes_include_extra_fields():
    """attributes includes non-message _source fields."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "a log", pod_name="api-1", namespace="prod"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    attrs = batch.records[0].attributes
    assert attrs.get("pod_name") == "api-1", f"pod_name missing: {attrs!r}"
    assert attrs.get("namespace") == "prod", f"namespace missing: {attrs!r}"


def test_parse_attributes_include_index():
    """attributes includes 'index' key set to hits.hits[]._index."""
    fake = _FakeSession(payload=_es_payload(
        _es_hit(_TS_1, "a log", index="k8s-logs-2026.01.01"),
    ))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    attrs = batch.records[0].attributes
    assert attrs.get("index") == "k8s-logs-2026.01.01", (
        f"expected 'index' key in attributes: {attrs!r}"
    )


def test_parse_empty_hits_returns_empty_batch():
    """No hits → empty LogBatch."""
    from core.signals import LogBatch
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("no-such-pod"), _no_window(), None))
    assert isinstance(batch, LogBatch)
    assert batch.records == []


# ─── Provenance ───────────────────────────────────────────────────────────────


def test_provenance_adapter_is_elasticsearch():
    """provenance.adapter == 'elasticsearch'."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.provenance.adapter == "elasticsearch"


def test_provenance_grouping_attr_is_index():
    """provenance.grouping_attr == 'index' (records group by the index they came from)."""
    fake = _FakeSession(payload=_es_payload())
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.provenance.grouping_attr == "index"


# ─── HTTP 401 → HttpAdapterError ─────────────────────────────────────────────


def test_http_401_raises_http_adapter_error():
    """HTTP 401 from ES → HttpAdapterError propagated (is-a AdapterError)."""
    fake = _FakeSession(status=401, payload={})
    source = _make_source(fake)
    with pytest.raises(HttpAdapterError):
        _run(source.fetch_logs(Entity("pod"), _no_window(), None))


def test_http_401_is_adapter_error():
    """HttpAdapterError is-a AdapterError (tool catches it via AdapterError)."""
    fake = _FakeSession(status=401, payload={})
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(Entity("pod"), _no_window(), None))
