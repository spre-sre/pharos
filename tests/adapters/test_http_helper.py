"""Unit tests for src/adapters/http.py — the shared HTTP transport.

TDD order: tests written first, implementation follows.
All HTTP is faked via the session_factory injection seam (NOT module patching).
Auth env vars set via monkeypatch.setenv (NO raw tokens in test code).
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.errors import AdapterError
from adapters.http import (
    HttpAdapterError,
    auth_header,
    enforce_body_limits,
    http_json,
    _reject_scripts,
)


# ─── fake session infrastructure (session_factory injection seam) ─────────────
#
# Shape mirrors the case-29 _FakeAiohttpSession idiom from
# tests/characterization/cases.py:476-493, but is injected via session_factory
# (NOT module-level patching — the golden harness patches only server-module
# attrs and can never reach adapters.http.aiohttp).


class _FakeResponse:
    """Minimal aiohttp response stand-in: has .status and async .json()."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    async def json(self) -> dict:
        return self._payload


class _RaisingContext:
    """Async context manager that raises exc on __aenter__ (simulates connection errors)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *a: Any) -> None:
        pass


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in for http_json injection.

    Usage::
        fake = _FakeSession(status=200, payload={"data": 1})
        result = asyncio.run(http_json(..., session_factory=fake))
        assert fake.calls[0]["method"] == "GET"
        assert fake.captured_timeout is not None

    The instance IS the factory: ``session_factory=fake`` causes
    ``http_json`` to call ``fake(timeout=<ClientTimeout>)``, which captures
    the timeout and returns ``self`` as the session object.
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
        """Called as ``session_factory(timeout=...)`` — captures timeout, returns self."""
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
    ) -> "_FakeResponse | _RaisingContext":
        if self._raise_exc is not None:
            return _RaisingContext(self._raise_exc)
        call = {"method": method, "url": url, "params": params, "json": json}
        self.calls.append(call)
        return _FakeResponse(self._status, self._payload)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# ─── http_json: timeout enforcement ───────────────────────────────────────────


def test_http_json_timeout_always_passed_to_session_factory():
    """ClientTimeout is ALWAYS passed to the session factory — never None."""
    fake = _FakeSession(payload={"ok": True})
    _run(http_json(
        "GET", "http://example.com/path",
        headers={}, timeout_s=30.0, session_factory=fake,
    ))
    assert fake.captured_timeout is not None, (
        "session_factory was not called with a timeout argument"
    )


def test_http_json_timeout_total_matches_timeout_s():
    """The ClientTimeout.total must equal the timeout_s argument."""
    fake = _FakeSession(payload={})
    _run(http_json(
        "GET", "http://example.com/check",
        headers={}, timeout_s=45.0, session_factory=fake,
    ))
    assert hasattr(fake.captured_timeout, "total"), (
        f"captured_timeout has no .total attr: {type(fake.captured_timeout)}"
    )
    assert fake.captured_timeout.total == 45.0, (
        f"expected .total=45.0, got {fake.captured_timeout.total}"
    )


# ─── http_json: request forwarding ────────────────────────────────────────────


def test_http_json_threads_method_url_params_json_to_session():
    """http_json threads method, url, params, and json_body through to request()."""
    fake = _FakeSession(payload={"result": "ok"})
    _run(http_json(
        "POST", "http://example.com/api",
        headers={"X-Test": "1"},
        params={"q": "hello"},
        json_body={"key": "value"},
        timeout_s=10.0,
        session_factory=fake,
    ))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://example.com/api"
    assert call["params"] == {"q": "hello"}
    assert call["json"] == {"key": "value"}


def test_http_json_200_returns_parsed_json():
    """Successful 200 response → the parsed JSON dict is returned."""
    payload = {"hits": {"total": 5, "hits": []}}
    fake = _FakeSession(status=200, payload=payload)
    result = _run(http_json(
        "POST", "http://example.com/es/_search",
        headers={}, timeout_s=10.0, session_factory=fake,
    ))
    assert result == payload


# ─── http_json: error handling ────────────────────────────────────────────────


def test_http_json_500_raises_http_adapter_error_naming_url_and_status():
    """HTTP 500 → HttpAdapterError whose message names both url and status."""
    url = "http://es.example.com:9200/my-index/_search"
    fake = _FakeSession(status=500)
    with pytest.raises(HttpAdapterError) as exc_info:
        _run(http_json("POST", url, headers={}, timeout_s=5.0, session_factory=fake))
    msg = str(exc_info.value)
    assert url in msg, f"url {url!r} not in error message {msg!r}"
    assert "500" in msg, f"status 500 not in error message {msg!r}"


def test_http_json_400_raises_http_adapter_error():
    """HTTP 400 → HttpAdapterError (any status >= 400 is an error)."""
    url = "http://loki.example.com/loki/api/v1/query_range"
    fake = _FakeSession(status=400)
    with pytest.raises(HttpAdapterError) as exc_info:
        _run(http_json("GET", url, headers={}, timeout_s=5.0, session_factory=fake))
    assert "400" in str(exc_info.value)


def test_http_json_connection_error_wrapped_into_http_adapter_error():
    """An aiohttp.ClientError is wrapped into HttpAdapterError (not propagated raw)."""
    url = "http://unreachable.example.com/path"
    conn_error = aiohttp.ClientError("simulated connection refused")
    fake = _FakeSession(raise_exc=conn_error)
    with pytest.raises(HttpAdapterError) as exc_info:
        _run(http_json("GET", url, headers={}, timeout_s=5.0, session_factory=fake))
    assert isinstance(exc_info.value, HttpAdapterError)
    assert exc_info.value.__cause__ is conn_error, (
        "original exception must be chained as __cause__"
    )


# ─── auth_header: bearer env ─────────────────────────────────────────────────


def test_auth_header_bearer_env_resolves_token(monkeypatch):
    """bearer_env → Bearer token read from the named env var."""
    monkeypatch.setenv("MY_API_TOKEN", "s3cr3t-token-value")
    result = auth_header({"bearer_env": "MY_API_TOKEN"})
    assert result == {"Authorization": "Bearer s3cr3t-token-value"}


def test_auth_header_missing_bearer_env_raises_naming_the_var(monkeypatch):
    """Missing bearer_env var → HttpAdapterError that names the missing var."""
    monkeypatch.delenv("NONEXISTENT_TOKEN_VAR", raising=False)
    with pytest.raises(HttpAdapterError) as exc_info:
        auth_header({"bearer_env": "NONEXISTENT_TOKEN_VAR"})
    assert "NONEXISTENT_TOKEN_VAR" in str(exc_info.value), (
        f"missing var name not in error: {exc_info.value!r}"
    )


# ─── auth_header: basic env ───────────────────────────────────────────────────


def test_auth_header_basic_b64_correct(monkeypatch):
    """basic_user_env + basic_pass_env → correct Base64-encoded Basic auth."""
    monkeypatch.setenv("ES_USER", "admin")
    monkeypatch.setenv("ES_PASS", "hunter2")
    result = auth_header({"basic_user_env": "ES_USER", "basic_pass_env": "ES_PASS"})
    expected_b64 = base64.b64encode(b"admin:hunter2").decode()
    assert result == {"Authorization": f"Basic {expected_b64}"}


def test_auth_header_missing_basic_user_raises_naming_the_var(monkeypatch):
    """Missing basic_user_env var → HttpAdapterError naming the var."""
    monkeypatch.delenv("MISSING_USER_VAR", raising=False)
    monkeypatch.setenv("ES_PASS_PRESENT", "pass123")
    with pytest.raises(HttpAdapterError) as exc_info:
        auth_header({"basic_user_env": "MISSING_USER_VAR", "basic_pass_env": "ES_PASS_PRESENT"})
    assert "MISSING_USER_VAR" in str(exc_info.value), (
        f"missing var name not in error: {exc_info.value!r}"
    )


def test_auth_header_missing_basic_pass_raises_naming_the_var(monkeypatch):
    """Missing basic_pass_env var → HttpAdapterError naming the var."""
    monkeypatch.setenv("ES_USER_PRESENT", "admin")
    monkeypatch.delenv("MISSING_PASS_VAR", raising=False)
    with pytest.raises(HttpAdapterError) as exc_info:
        auth_header({"basic_user_env": "ES_USER_PRESENT", "basic_pass_env": "MISSING_PASS_VAR"})
    assert "MISSING_PASS_VAR" in str(exc_info.value), (
        f"missing var name not in error: {exc_info.value!r}"
    )


# ─── auth_header: empty / no-op ───────────────────────────────────────────────


def test_auth_header_empty_options_returns_empty_dict():
    """No auth options → empty dict (no header injected)."""
    assert auth_header({}) == {}


def test_auth_header_unrecognised_options_returns_empty_dict():
    """Options with no auth keys → empty dict."""
    assert auth_header({"timeout_s": 30, "url": "http://x.example.com"}) == {}


# ─── _reject_scripts ─────────────────────────────────────────────────────────


def test_reject_scripts_top_level_script_key_raises():
    """'script' key at the top level → AdapterError."""
    body = {"query": {"match_all": {}}, "script": {"source": "evil"}}
    with pytest.raises(AdapterError):
        _reject_scripts(body)


def test_reject_scripts_nested_dict_script_key_raises():
    """'script' key nested inside a sub-dict → AdapterError."""
    body = {"query": {"bool": {"filter": {"script": {"source": "evil"}}}}}
    with pytest.raises(AdapterError):
        _reject_scripts(body)


def test_reject_scripts_inside_list_raises():
    """'script' key inside a list element → AdapterError."""
    body = {
        "query": {
            "bool": {
                "should": [
                    {"match": {"field": "value"}},
                    {"script": {"source": "evil in list"}},
                ]
            }
        }
    }
    with pytest.raises(AdapterError):
        _reject_scripts(body)


def test_reject_scripts_script_score_key_raises():
    """'script_score' key at any depth → AdapterError."""
    body = {"query": {"script_score": {"query": {"match_all": {}}, "script": {"source": "score"}}}}
    with pytest.raises(AdapterError):
        _reject_scripts(body)


def test_reject_scripts_script_score_nested_in_list_raises():
    """'script_score' nested inside a list → AdapterError."""
    body = {"queries": [{"script_score": {"query": {"match_all": {}}}}, {"match_all": {}}]}
    with pytest.raises(AdapterError):
        _reject_scripts(body)


def test_reject_scripts_clean_body_passes():
    """Body with no forbidden keys → no exception raised."""
    body = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kubernetes.namespace": "prod"}},
                    {"range": {"@timestamp": {"gte": "now-1h"}}},
                ]
            }
        },
        "size": 100,
    }
    _reject_scripts(body)  # must not raise


def test_reject_scripts_empty_body_passes():
    """Empty dict → no exception."""
    _reject_scripts({})


def test_reject_scripts_empty_list_passes():
    """Empty list → no exception."""
    _reject_scripts([])


def test_reject_scripts_list_of_clean_dicts_passes():
    """List containing clean dicts → no exception."""
    _reject_scripts([{"term": {"field": "value"}}, {"match_all": {}}])


# ─── enforce_body_limits ─────────────────────────────────────────────────────


def test_enforce_body_limits_over_cap_raises():
    """Body whose JSON encoding exceeds 64 000 bytes → AdapterError."""
    large_body = {"data": "x" * 70_000}
    with pytest.raises(AdapterError):
        enforce_body_limits(large_body)


def test_enforce_body_limits_under_cap_passes():
    """Body well under 64 000 bytes → no exception."""
    small_body = {"query": "hello", "size": 100}
    enforce_body_limits(small_body)  # must not raise


def test_enforce_body_limits_exactly_at_cap_passes():
    """Body whose JSON is exactly max_bytes → no exception (boundary inclusive)."""
    # Build body that serializes to exactly 64 000 bytes.
    # json.dumps({"data": "x"*N}) = '{"data": "' + "x"*N + '"}'
    # len = len('{"data": "') + N + len('"}') = 10 + N + 2 = N + 12
    n = 64_000 - 12
    body = {"data": "x" * n}
    serialized = json.dumps(body)
    assert len(serialized) == 64_000
    enforce_body_limits(body)  # must not raise


def test_enforce_body_limits_one_over_cap_raises():
    """Body whose JSON is exactly 64 001 bytes → AdapterError."""
    n = 64_001 - 12
    body = {"data": "x" * n}
    serialized = json.dumps(body)
    assert len(serialized) == 64_001
    with pytest.raises(AdapterError):
        enforce_body_limits(body, max_bytes=64_000)


def test_enforce_body_limits_custom_max_bytes():
    """Custom max_bytes cap is honoured."""
    body = {"key": "value"}  # ~15 bytes serialized
    enforce_body_limits(body, max_bytes=100)  # should pass
    with pytest.raises(AdapterError):
        enforce_body_limits(body, max_bytes=5)  # 15 > 5 → should fail


# ─── type hierarchy ───────────────────────────────────────────────────────────


def test_http_adapter_error_is_subclass_of_adapter_error():
    """HttpAdapterError is a subclass of AdapterError."""
    err = HttpAdapterError("http failure")
    assert isinstance(err, AdapterError), (
        f"HttpAdapterError must be a subclass of AdapterError"
    )


def test_http_adapter_error_is_subclass_of_value_error():
    """HttpAdapterError (via AdapterError) is a ValueError — tools can catch it as ValueError."""
    err = HttpAdapterError("http failure")
    assert isinstance(err, ValueError)


# ─── RecursionError carry-in (mandatory Task-4 fix) ──────────────────────────
#
# A deep-but-small body (~1500 nesting, ~9 KB, UNDER the 64 KB byte cap)
# raised a bare RecursionError in both _reject_scripts and enforce_body_limits.
# RecursionError is a RuntimeError, NOT AdapterError/ValueError, so it escaped
# tool catches.  The fix: _reject_scripts gains a depth guard (raises AdapterError
# at depth > 100); enforce_body_limits wraps json.dumps in except RecursionError.
#
# Both functions MUST raise AdapterError (not RecursionError) for deep bodies.


def _deep_body(depth: int) -> dict:
    """Build a dict nested *depth* levels deep with a tiny leaf value."""
    body: dict = {"leaf": "value"}
    for _ in range(depth):
        body = {"nested": body}
    return body


def test_reject_scripts_deep_body_raises_adapter_error_not_recursion():
    """_reject_scripts: 1500-deep body → AdapterError, not RecursionError."""
    body = _deep_body(1500)
    # Must raise AdapterError (depth guard) rather than letting Python's
    # RecursionError bubble out (which is a RuntimeError, not AdapterError).
    with pytest.raises(AdapterError):
        _reject_scripts(body)
    # Extra check: must NOT propagate RecursionError
    try:
        _reject_scripts(body)
    except AdapterError:
        pass  # correct — depth guard raised AdapterError
    except RecursionError:
        pytest.fail(
            "_reject_scripts raised RecursionError (RuntimeError), "
            "not AdapterError — the depth guard is missing or broken"
        )


def test_enforce_body_limits_deep_body_raises_adapter_error_not_recursion():
    """enforce_body_limits: 100_000-deep body → AdapterError, never RecursionError.

    Depth 1500 sat exactly on CPython's C-recursion guard boundary
    (Py_C_RECURSION_LIMIT), which moved across 3.12.x patch releases — the
    test passed or failed depending on interpreter build. At 100_000 the
    outcome is deterministic on every supported build: json.dumps either hits
    the C recursion guard (wrapped to AdapterError) or, on a build with a
    huge limit, serializes past the 64 KB cap (also AdapterError). The
    invariant this test pins is the exception CONTRACT: callers always see
    AdapterError, never a bare RecursionError escaping the §4.7 guards.
    """
    body = _deep_body(100_000)
    with pytest.raises(AdapterError):
        enforce_body_limits(body)
    # Extra check: must NOT propagate RecursionError
    try:
        enforce_body_limits(body)
    except AdapterError:
        pass  # correct
    except RecursionError:
        pytest.fail(
            "enforce_body_limits raised RecursionError, not AdapterError — "
            "json.dumps RecursionError is not wrapped"
        )
