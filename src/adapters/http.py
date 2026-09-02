"""Shared HTTP transport for remote log-source adapters (Loki, Elasticsearch).

Design (spec §4.7 + phase-4 Opus consult §C):
- ClientTimeout is ALWAYS set (never None) — request stalls cannot hang forever.
- HTTP status >= 400 surfaces as HttpAdapterError (url + status in message).
- aiohttp connection errors are caught and re-raised as HttpAdapterError.
- Auth is ALWAYS via env-var references (bearer_env / basic_user_env+basic_pass_env);
  raw tokens must never appear in config or option dicts.
- session_factory defaults to aiohttp.ClientSession and is the INJECTABLE TEST
  SEAM — the golden harness patches only server-module attrs and cannot reach
  adapters.http.aiohttp; inject a fake here in tests instead.
- _reject_scripts / enforce_body_limits MUST run before any request call so
  that script injection and oversized payloads are rejected pre-request.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Optional

import aiohttp

try:
    from core.errors import AdapterError
except ImportError:  # pragma: no cover — fallback when core.errors not on path
    AdapterError = ValueError  # type: ignore[assignment,misc]


class HttpAdapterError(AdapterError):
    """HTTP-level adapter failure: status >= 400 or aiohttp connection error."""


async def http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: float,
    session_factory: Any = None,
) -> dict:
    """Execute a single HTTP request and return the parsed JSON body.

    Parameters
    ----------
    method:
        HTTP verb (e.g. "GET", "POST").
    url:
        Full URL of the endpoint.
    headers:
        Request headers dict (already includes auth from auth_header()).
    params:
        URL query parameters (forwarded verbatim to aiohttp).
    json_body:
        Dict to serialize as the JSON request body; None for requests with no body.
    timeout_s:
        Total timeout in seconds — ALWAYS applied, never skipped.
    session_factory:
        Callable that accepts ``timeout=<ClientTimeout>`` and returns an async
        context manager yielding the session.  Defaults to
        ``aiohttp.ClientSession``.  Inject a fake in tests (do NOT patch the
        module-level ``aiohttp`` name — the golden harness cannot reach it).

    Raises
    ------
    HttpAdapterError
        On HTTP status >= 400 (message includes url + status).
    HttpAdapterError
        On any ``aiohttp.ClientError`` connection failure (chained as ``__cause__``).
    """
    if session_factory is None:
        session_factory = aiohttp.ClientSession

    timeout = aiohttp.ClientTimeout(total=timeout_s)

    try:
        async with session_factory(timeout=timeout) as session:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            ) as resp:
                if resp.status >= 400:
                    raise HttpAdapterError(
                        f"{url} returned status {resp.status}"
                    )
                return await resp.json()
    except HttpAdapterError:
        raise
    except aiohttp.ClientError as exc:
        raise HttpAdapterError(
            f"connection error reaching {url}: {exc}"
        ) from exc


def auth_header(options: Dict[str, Any]) -> Dict[str, str]:
    """Build the HTTP Authorization header from env-var references in *options*.

    Supported auth modes (checked in order):
    1. ``bearer_env`` — reads ``os.environ[options["bearer_env"]]`` and produces
       ``Authorization: Bearer <token>``.
    2. ``basic_user_env`` + ``basic_pass_env`` — reads both env vars, Base64-encodes
       ``user:pass``, and produces ``Authorization: Basic <b64>``.
    3. No recognised key → returns ``{}`` (no auth header added).

    A *missing* env var always raises :exc:`HttpAdapterError` that NAMES the
    variable, so configuration errors surface clearly before any request is made.

    Raises
    ------
    HttpAdapterError
        When a referenced env var is not set.
    """
    if bearer_env := options.get("bearer_env"):
        token = os.environ.get(bearer_env)
        if token is None:
            raise HttpAdapterError(
                f"bearer_env references env var {bearer_env!r} which is not set"
            )
        return {"Authorization": f"Bearer {token}"}

    user_env = options.get("basic_user_env")
    pass_env = options.get("basic_pass_env")
    if user_env and pass_env:
        user = os.environ.get(user_env)
        if user is None:
            raise HttpAdapterError(
                f"basic_user_env references env var {user_env!r} which is not set"
            )
        pw = os.environ.get(pass_env)
        if pw is None:
            raise HttpAdapterError(
                f"basic_pass_env references env var {pass_env!r} which is not set"
            )
        encoded = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    return {}


def _reject_scripts(body: Any, _depth: int = 0) -> None:
    """Recursively walk *body* and raise :exc:`AdapterError` on forbidden keys.

    Forbidden keys (anywhere in the tree): ``"script"``, ``"script_score"``.

    These keys are the primary Elasticsearch/Painless code-execution injection
    vector.  Rejection must happen PRE-REQUEST so that no malicious body ever
    reaches the server — do NOT call _reject_scripts after http_json.

    Parameters
    ----------
    body:
        The request body (dict or list at any nesting level, or a scalar leaf).
    _depth:
        Internal recursion depth counter.  Do not pass externally.

    Raises
    ------
    AdapterError
        When any ``"script"`` or ``"script_score"`` key is encountered at any
        depth in the tree, OR when the nesting depth exceeds 100 (a depth guard
        preventing RecursionError — bare RecursionError is a RuntimeError and
        would escape tool catches that only catch AdapterError/ValueError).
    """
    if _depth > 100:
        raise AdapterError(
            f"request body nesting depth exceeds 100 (§4.7 depth guard)"
        )
    if isinstance(body, dict):
        for key, value in body.items():
            if key in ("script", "script_score"):
                raise AdapterError(
                    f"forbidden key {key!r} in request body (§4.7 script-injection guard)"
                )
            _reject_scripts(value, _depth + 1)
    elif isinstance(body, list):
        for item in body:
            _reject_scripts(item, _depth + 1)
    # Scalar leaves (str, int, float, bool, None) are safe — no recursion needed.


def enforce_body_limits(body: Any, max_bytes: int = 64_000) -> None:
    """Raise :exc:`AdapterError` if the JSON-encoded *body* exceeds *max_bytes*.

    The serialized length check uses ``json.dumps`` to account for actual wire
    size, not the Python object size.  A body at exactly *max_bytes* passes;
    *max_bytes + 1* or larger raises.

    Parameters
    ----------
    body:
        Request body to check.
    max_bytes:
        Byte cap (default 64 000).

    Raises
    ------
    AdapterError
        When ``len(json.dumps(body)) > max_bytes``, OR when ``json.dumps``
        itself raises :exc:`RecursionError` due to extreme nesting depth.
        RecursionError is a RuntimeError and would escape tool catches that
        only handle AdapterError/ValueError; wrapping it here keeps all
        pre-request rejections under a single exception hierarchy.
    """
    try:
        serialized = json.dumps(body)
    except RecursionError as exc:
        raise AdapterError(
            "request body nesting is too deep for json.dumps (§4.7 depth guard)"
        ) from exc
    if len(serialized) > max_bytes:
        raise AdapterError(
            f"request body exceeds {max_bytes}-byte cap "
            f"({len(serialized)} bytes serialized)"
        )
