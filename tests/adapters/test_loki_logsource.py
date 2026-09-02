"""Unit tests for src/adapters/loki/logs.py -- LokiLogSource.

TDD order: tests written FIRST, implementation follows.
All HTTP is faked via the session_factory injection seam (NOT module patching).

Design notes (from plan/phase4 Task 3):
- Entity -> {<kind-or-entity_label>=~"<pattern>"} with backslash then quote
  escaped in pattern (regex metacharacters remain LIVE by design -- only
  quote-injection escaping)
- Matchers -> {k="v",...} sorted, values escaped identically
- Native -> raw query string passthrough
- windowed -> query_range + ns-epoch start/end params
- unwindowed -> query
- direction=backward (documented divergence from default forward)
- grouping_attr="stream" with literal "stream" key in attributes (sorted k=v
  join) so that _logbatch_to_legacy_envelope groups by stream correctly (F2)
- session_factory injected into LokiLogSource via options["session_factory"]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.errors import AdapterError
from core.selector import Entity, Limit, Matchers, Native, TimeWindow
from adapters.http import HttpAdapterError


# ─── fake session infrastructure (mirrors test_http_helper.py idiom) ──────────


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

    Captures all calls for assertion; returns canned payloads.
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


def _loki_payload(streams: list[dict]) -> dict:
    """Build a Loki query_range/query response payload."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": streams,
        },
    }


def _stream(labels: dict, values: list[tuple[str, str]]) -> dict:
    """Build a single stream entry: {stream: labels, values: [[ns, line], ...]}."""
    return {
        "stream": labels,
        "values": [[ts_ns, line] for ts_ns, line in values],
    }


def _make_source(fake: _FakeSession, **opts) -> Any:
    """Build a LokiLogSource with the given fake session injected."""
    from adapters.loki.logs import LokiLogSource

    options = {"session_factory": fake, **opts}
    return LokiLogSource(url="http://loki.example.com", options=options)


def _no_window() -> TimeWindow:
    return TimeWindow(start=None, end=None)


# ─── LogQL compilation — Entity ───────────────────────────────────────────────


def test_entity_selector_uses_default_entity_label():
    """Entity with no kind → uses entity_label (default 'pod') in LogQL."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("my-pod"), _no_window(), None))
    assert len(fake.calls) == 1
    q = fake.calls[0]["params"]["query"]
    assert 'pod=~"my-pod"' in q, f"expected pod=~... in query, got: {q!r}"


def test_entity_selector_uses_kind_when_present():
    """Entity with kind specified → uses kind as label in LogQL."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("my-svc", kind="service"), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    assert 'service=~"my-svc"' in q, f"expected service=~... in query, got: {q!r}"


def test_entity_selector_uses_custom_entity_label():
    """entity_label option overrides default 'pod' when kind is absent."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake, entity_label="container")
    _run(source.fetch_logs(Entity("app-container"), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    assert 'container=~"app-container"' in q, f"expected container=~... in query, got: {q!r}"


def test_entity_query_has_braces():
    """Entity LogQL starts with '{' and ends with '}'."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("my-pod"), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    assert q.startswith("{") and q.endswith("}"), f"query not braced: {q!r}"


# ─── LogQL compilation — Matchers ─────────────────────────────────────────────


def test_matchers_selector_builds_equality_labels():
    """Matchers → {k="v",...} sorted by key."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(
        Matchers({"app": "frontend", "env": "prod"}), _no_window(), None
    ))
    q = fake.calls[0]["params"]["query"]
    assert 'app="frontend"' in q, f"expected app= in query, got: {q!r}"
    assert 'env="prod"' in q, f"expected env= in query, got: {q!r}"


def test_matchers_selector_sorted_by_key():
    """Matchers keys appear in sorted order in the LogQL."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(
        Matchers({"zzz": "last", "aaa": "first"}), _no_window(), None
    ))
    q = fake.calls[0]["params"]["query"]
    pos_aaa = q.find("aaa=")
    pos_zzz = q.find("zzz=")
    assert pos_aaa < pos_zzz, f"keys not sorted in query: {q!r}"


def test_matchers_single_term():
    """Single Matcher → {k="v"}."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Matchers({"namespace": "prod"}), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    assert 'namespace="prod"' in q, f"expected namespace=... in query, got: {q!r}"


# ─── LogQL compilation — Native ───────────────────────────────────────────────


def test_native_selector_passthrough():
    """Native → raw LogQL query string forwarded verbatim."""
    raw_logql = '{job="varlogs", level="error"} |= "timeout"'
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Native(raw_logql), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    assert q == raw_logql, f"expected verbatim query, got: {q!r}"


# ─── Injection negative (F4 from plan) ────────────────────────────────────────


def test_entity_pattern_with_double_quote_escaped():
    """Entity pattern containing '"' → backslash-escaped before interpolation.

    Pins the EXACT compiled LogQL string so that a wrong-order escape mutant
    (quote-before-backslash) fails: 'evil"pod' correct → {pod=~"evil\\"pod"}
    while the mutant produces {pod=~"evil\\\\"pod"} (double-escaped backslash
    plus an unescaped close-quote, a genuine LogQL breakout).
    """
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity('evil"pod'), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    # Exact string: backslash escapes the double-quote, nothing else changes.
    # LogQL in memory: {pod=~"evil\"pod"}
    assert q == '{pod=~"evil\\"pod"}', (
        f"expected exact escaped query, got: {q!r}"
    )


def test_entity_pattern_with_backslash_escaped():
    """Entity pattern containing '\\' → backslash doubled first.

    Backslash is escaped before double-quote.  For a pure-backslash input the
    escape order does not matter, but the EXACT output is pinned so any change
    to the escaping function is caught immediately.
    LogQL in memory: {pod=~"pod\\\\with\\\\backslash"}
    """
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod\\with\\backslash"), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    # Each literal backslash becomes \\ inside the LogQL string.
    assert q == '{pod=~"pod\\\\with\\\\backslash"}', (
        f"expected exact escaped query, got: {q!r}"
    )


def test_entity_pattern_combined_injection_well_formed():
    """Entity pattern with both '\\' and '"' → well-formed LogQL (both escaped).

    This is the key mutant-killing test: 'a\\b"c' (a, backslash, b, quote, c).
    Correct order (backslash first then quote):
      '\\' → '\\\\', then '"' → '\\"'  → a\\b\\"c  → {pod=~"a\\\\b\\"c"}
    Wrong order (quote first then backslash):
      '"' → '\\"',   then '\\' → '\\\\' → a\\\\b\\\\"c → {pod=~"a\\\\b\\\\"c"}
    Only the exact-match assertion distinguishes them.
    """
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity('a\\b"c'), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    # Exact LogQL in memory: {pod=~"a\\b\"c"}
    assert q == '{pod=~"a\\\\b\\"c"}', (
        f"expected exact escaped query, got: {q!r}"
    )


def test_matchers_value_with_double_quote_escaped():
    """Matchers value containing '"' → escaped before interpolation.

    Pins the EXACT compiled LogQL string.
    LogQL in memory: {env="prod\\"evil"}  (backslash then double-quote)
    Under the wrong-order mutant: {env="prod\\\\"evil"}  (double-backslash + close-quote).
    """
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Matchers({"env": 'prod"evil'}), _no_window(), None))
    q = fake.calls[0]["params"]["query"]
    # Exact LogQL in memory: {env="prod\"evil"}
    assert q == '{env="prod\\"evil"}', (
        f"expected exact escaped query in Matchers value, got: {q!r}"
    )


# ─── Windowed vs unwindowed — endpoint routing ────────────────────────────────


def test_unwindowed_uses_query_endpoint():
    """No time window → GET /loki/api/v1/query."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("my-pod"), _no_window(), None))
    url = fake.calls[0]["url"]
    assert url.endswith("/loki/api/v1/query"), f"unexpected endpoint: {url!r}"


def test_windowed_uses_query_range_endpoint():
    """Time window present → GET /loki/api/v1/query_range."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    window = TimeWindow(
        start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    _run(source.fetch_logs(Entity("my-pod"), window, None))
    url = fake.calls[0]["url"]
    assert url.endswith("/loki/api/v1/query_range"), f"unexpected endpoint: {url!r}"


def test_start_only_window_uses_query_range():
    """Window with start only → query_range."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    window = TimeWindow(start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _run(source.fetch_logs(Entity("my-pod"), window, None))
    url = fake.calls[0]["url"]
    assert url.endswith("/loki/api/v1/query_range"), f"unexpected endpoint: {url!r}"


def test_end_only_window_uses_query_range():
    """Window with end only → query_range."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    window = TimeWindow(end=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc))
    _run(source.fetch_logs(Entity("my-pod"), window, None))
    url = fake.calls[0]["url"]
    assert url.endswith("/loki/api/v1/query_range"), f"unexpected endpoint: {url!r}"


# ─── Windowed params — ns-epoch start/end ─────────────────────────────────────


def test_windowed_start_param_is_ns_epoch():
    """Window start → start param as nanosecond epoch string."""
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), TimeWindow(start=dt), None))
    params = fake.calls[0]["params"]
    assert "start" in params, f"start param missing from: {params!r}"
    # nanosecond epoch for 2026-01-01T12:00:00Z
    expected_ns = int(dt.timestamp() * 1e9)
    assert str(params["start"]) == str(expected_ns), (
        f"expected ns epoch {expected_ns}, got {params['start']!r}"
    )


def test_windowed_end_param_is_ns_epoch():
    """Window end → end param as nanosecond epoch string."""
    dt = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), TimeWindow(end=dt), None))
    params = fake.calls[0]["params"]
    assert "end" in params, f"end param missing from: {params!r}"
    expected_ns = int(dt.timestamp() * 1e9)
    assert str(params["end"]) == str(expected_ns), (
        f"expected ns epoch {expected_ns}, got {params['end']!r}"
    )


def test_unwindowed_has_no_start_end_params():
    """No window → no start/end in params."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    params = fake.calls[0]["params"]
    assert "start" not in params, f"unexpected start param: {params!r}"
    assert "end" not in params, f"unexpected end param: {params!r}"


# ─── Limit forwarding ─────────────────────────────────────────────────────────


def test_limit_forwarded_as_limit_param():
    """Limit(max_records=N) → limit=N in GET params."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), Limit(max_records=42)))
    params = fake.calls[0]["params"]
    assert "limit" in params, f"limit param missing from: {params!r}"
    assert str(params["limit"]) == "42", f"limit not 42, got: {params['limit']!r}"


def test_no_limit_uses_default_100():
    """No Limit → limit defaults to 100."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    params = fake.calls[0]["params"]
    assert str(params.get("limit")) == "100", (
        f"expected default limit=100, got: {params.get('limit')!r}"
    )


def test_direction_is_backward():
    """direction=backward is always sent."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    params = fake.calls[0]["params"]
    assert params.get("direction") == "backward", (
        f"expected direction=backward, got: {params.get('direction')!r}"
    )


# ─── Parsing — LogRecord production ───────────────────────────────────────────

# Fixed nanosecond timestamps for deterministic parsing tests
_TS_NS_1 = "1735689600000000000"  # 2026-01-01T00:00:00Z
_TS_NS_2 = "1735693200000000000"  # 2026-01-01T01:00:00Z
_TS_NS_3 = "1735696800000000000"  # 2026-01-01T02:00:00Z


def _expected_iso(ts_ns: str) -> str:
    """Convert ns timestamp to ISO string for assertion."""
    ts_s = int(ts_ns) / 1e9
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).isoformat()


def test_parse_produces_logrecords():
    """Result entries → LogRecord instances with correct body."""
    stream_data = [
        _stream({"pod": "api-1", "namespace": "prod"}, [
            (_TS_NS_1, "INFO: server started"),
            (_TS_NS_2, "ERROR: something failed"),
        ])
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-1"), _no_window(), None))
    assert len(batch.records) == 2
    bodies = {r.body for r in batch.records}
    assert "INFO: server started" in bodies
    assert "ERROR: something failed" in bodies


def test_parse_timestamp_is_iso_from_ns():
    """LogRecord.timestamp is ISO-8601 string derived from nanosecond epoch."""
    stream_data = [
        _stream({"pod": "api-1"}, [(_TS_NS_1, "line one")])
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-1"), _no_window(), None))
    assert len(batch.records) == 1
    expected = _expected_iso(_TS_NS_1)
    assert batch.records[0].timestamp == expected, (
        f"expected {expected!r}, got {batch.records[0].timestamp!r}"
    )


def test_parse_severity_is_none():
    """LogRecord.severity is always None (Loki has no severity field)."""
    stream_data = [
        _stream({"pod": "api-1"}, [(_TS_NS_1, "ERROR line")])
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-1"), _no_window(), None))
    assert batch.records[0].severity is None


def test_parse_stream_labels_in_attributes():
    """Stream labels appear as individual keys in LogRecord.attributes."""
    stream_data = [
        _stream({"pod": "api-1", "namespace": "prod", "app": "web"}, [
            (_TS_NS_1, "some log line")
        ])
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-1"), _no_window(), None))
    attrs = batch.records[0].attributes
    assert attrs.get("pod") == "api-1"
    assert attrs.get("namespace") == "prod"
    assert attrs.get("app") == "web"


def test_parse_stream_key_present_in_attributes(  ):
    """attributes['stream'] is the sorted 'k=v,k=v' join of stream labels (F2).

    This is the key that grouping_attr='stream' uses to group records.
    Without this key every record would fall under the 'log' fallback group.
    """
    stream_data = [
        _stream({"pod": "api-1", "namespace": "prod"}, [(_TS_NS_1, "line")])
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-1"), _no_window(), None))
    attrs = batch.records[0].attributes
    assert "stream" in attrs, f"'stream' key missing from attributes: {attrs!r}"
    # sorted k=v,k=v
    stream_val = attrs["stream"]
    assert "namespace=prod" in stream_val
    assert "pod=api-1" in stream_val


def test_parse_stream_key_is_sorted():
    """attributes['stream'] value has labels in sorted key order."""
    stream_data = [
        _stream({"zzz": "last", "aaa": "first"}, [(_TS_NS_1, "line")])
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("zzz-pod"), _no_window(), None))
    stream_val = batch.records[0].attributes["stream"]
    pos_aaa = stream_val.find("aaa=")
    pos_zzz = stream_val.find("zzz=")
    assert pos_aaa < pos_zzz, f"stream key not sorted: {stream_val!r}"


def test_parse_multi_stream_two_distinct_stream_keys():
    """Two streams → two distinct 'stream' attribute values."""
    stream_data = [
        _stream({"pod": "api-1"}, [(_TS_NS_1, "line from api-1")]),
        _stream({"pod": "api-2"}, [(_TS_NS_2, "line from api-2")]),
    ]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-*"), _no_window(), None))
    stream_keys = {r.attributes["stream"] for r in batch.records}
    assert len(stream_keys) == 2, (
        f"expected 2 distinct stream keys, got {stream_keys!r}"
    )


# ─── Provenance ───────────────────────────────────────────────────────────────


def test_provenance_adapter_is_loki():
    """provenance.adapter == 'loki'."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.provenance.adapter == "loki"


def test_provenance_grouping_attr_is_stream():
    """provenance.grouping_attr == 'stream'."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert batch.provenance.grouping_attr == "stream"


def test_provenance_notes_mention_backward_direction():
    """provenance.notes mentions backward direction (documented divergence)."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    notes_text = " ".join(batch.provenance.notes)
    assert "backward" in notes_text.lower(), (
        f"expected 'backward' in provenance notes, got: {batch.provenance.notes!r}"
    )


# ─── Empty result → empty batch ───────────────────────────────────────────────


def test_empty_result_returns_empty_batch():
    """data.result=[] → LogBatch with empty records list."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("no-such-pod"), _no_window(), None))
    from core.signals import LogBatch
    assert isinstance(batch, LogBatch)
    assert batch.records == []


def test_stream_with_empty_values_returns_empty_batch():
    """Stream present but values=[] → empty records."""
    stream_data = [_stream({"pod": "api-1"}, [])]
    fake = _FakeSession(payload=_loki_payload(stream_data))
    source = _make_source(fake)
    batch = _run(source.fetch_logs(Entity("api-1"), _no_window(), None))
    assert batch.records == []


# ─── HTTP 500 → HttpAdapterError ─────────────────────────────────────────────


def test_http_500_raises_http_adapter_error():
    """HTTP 500 from Loki → HttpAdapterError propagated."""
    fake = _FakeSession(status=500, payload={})
    source = _make_source(fake)
    with pytest.raises(HttpAdapterError):
        _run(source.fetch_logs(Entity("pod"), _no_window(), None))


def test_http_500_is_adapter_error():
    """HttpAdapterError is-a AdapterError."""
    fake = _FakeSession(status=500, payload={})
    source = _make_source(fake)
    with pytest.raises(AdapterError):
        _run(source.fetch_logs(Entity("pod"), _no_window(), None))


# ─── Tenant header ────────────────────────────────────────────────────────────


def test_tenant_option_adds_scope_orgid_header():
    """tenant option → X-Scope-OrgID header in request."""
    captured_headers: list[dict] = []

    class _HeaderCapturingFakeSession(_FakeSession):
        def request(self, method, url, *, params=None, json=None, headers=None, **kw):
            captured_headers.append(headers or {})
            return super().request(method, url, params=params, json=json,
                                   headers=headers, **kw)

    fake = _HeaderCapturingFakeSession(payload=_loki_payload([]))
    from adapters.loki.logs import LokiLogSource
    source = LokiLogSource(
        url="http://loki.example.com",
        options={"tenant": "my-tenant", "session_factory": fake},
    )
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert captured_headers, "no request was made"
    assert captured_headers[0].get("X-Scope-OrgID") == "my-tenant", (
        f"X-Scope-OrgID header missing or wrong: {captured_headers[0]!r}"
    )


# ─── Endpoint allowlist (§4.7) ────────────────────────────────────────────────


def test_all_urls_are_loki_api_v1_only():
    """Every URL the adapter requests is under /loki/api/v1/ — no other paths.

    Collect all URLs across all tests by running several scenarios through
    a single capturing fake, then assert the full URL set is a subset of
    {query, query_range} endpoints.
    """
    from adapters.loki.logs import LokiLogSource

    all_urls: list[str] = []

    class _UrlCapturing(_FakeSession):
        def request(self, method, url, *, params=None, json=None, headers=None, **kw):
            all_urls.append(url)
            return super().request(method, url, params=params, json=json,
                                   headers=headers, **kw)

    base = "http://loki.example.com"
    allowed = {
        f"{base}/loki/api/v1/query",
        f"{base}/loki/api/v1/query_range",
    }

    scenarios = [
        (Entity("pod"), _no_window()),
        (Entity("pod"), TimeWindow(start=datetime(2026, 1, 1, tzinfo=timezone.utc))),
        (Matchers({"app": "web"}), _no_window()),
        (Native('{job="test"}'), _no_window()),
    ]

    for sel, win in scenarios:
        fake = _UrlCapturing(payload=_loki_payload([]))
        source = LokiLogSource(url=base, options={"session_factory": fake})
        _run(source.fetch_logs(sel, win, None))

    unknown = [u for u in all_urls if u not in allowed]
    assert not unknown, (
        f"URLs outside the allowlist captured: {unknown!r}\n"
        f"Allowed: {sorted(allowed)}"
    )


# ─── HTTP method is GET ────────────────────────────────────────────────────────


def test_request_method_is_get():
    """Loki queries use GET, never POST."""
    fake = _FakeSession(payload=_loki_payload([]))
    source = _make_source(fake)
    _run(source.fetch_logs(Entity("pod"), _no_window(), None))
    assert fake.calls[0]["method"] == "GET", (
        f"expected GET, got {fake.calls[0]['method']!r}"
    )
