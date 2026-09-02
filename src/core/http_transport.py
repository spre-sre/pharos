"""HTTP transport helpers for lumino-mcp-server (phase 2f).

This module is stdlib + starlette only — it MUST NOT import server-mcp or any
module that triggers server-mcp's side-effects.

Task 1 surface: resolve_transport only.
Task 2 surface: resolve_http_serving, verify_bearer, BearerASGIMiddleware.
"""
from __future__ import annotations

import hmac
from typing import Mapping, Optional

_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_transport(env: Mapping[str, str]) -> str:
    """Return 'stdio' or 'streamable-http' (D1).

    Precedence:
      1. LUMINO_TRANSPORT if set — validated; ValueError on anything else.
      2. k8s auto-detect — KUBERNETES_NAMESPACE or K8S_NAMESPACE set
         → 'streamable-http'.
      3. Default → 'stdio'.
    """
    explicit = env.get("LUMINO_TRANSPORT")
    if explicit is not None:
        if explicit not in ("stdio", "streamable-http"):
            raise ValueError(
                f"LUMINO_TRANSPORT={explicit!r} is invalid; "
                "expected 'stdio' or 'streamable-http'"
            )
        return explicit

    if env.get("KUBERNETES_NAMESPACE") or env.get("K8S_NAMESPACE"):
        return "streamable-http"

    return "stdio"


def resolve_http_serving(host: str, token: Optional[str]) -> str:
    """Return 'refuse' | 'serve_authed' | 'serve_open' (D3 fail-closed).

    non-localhost (host not in {'127.0.0.1','localhost','::1'}) + no/empty token -> 'refuse'
    token present (any host)                                                      -> 'serve_authed'
    localhost + no token                                                          -> 'serve_open'
    """
    if token:
        return "serve_authed"
    if host in _LOCALHOST_HOSTS:
        return "serve_open"
    return "refuse"


def verify_bearer(auth_header: Optional[str], expected_token: str) -> bool:
    """Return True iff header == f'Bearer {expected_token}' via hmac.compare_digest.

    False for None/malformed/wrong. Never raises, never logs.
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    presented = auth_header[len("Bearer "):]
    return hmac.compare_digest(presented.encode(), expected_token.encode())


class BearerASGIMiddleware:
    """Pure ASGI wrapper (R1 — NOT Starlette add_middleware).

    __init__(app, token, exempt_paths=frozenset({'/health'})).

    Round-1 F5 hardening: passes through ONLY 'lifespan' scopes and exempt-path
    http scopes; any OTHER non-http scope (websocket etc.) raises RuntimeError —
    no non-HTTP transport is ever auth-exempt (latent bypass for the phase-5 OTLP
    reuse otherwise). For http scopes: exempt path -> pass through; else check
    'authorization' via verify_bearer -> pass through, or send 401 with headers
    [WWW-Authenticate: Bearer, content-type: application/json] and body
    b'{"error":"unauthorized"}'.

    Never logs the token or the presented header.

    INVARIANT (documented for OTLP reuse): only constructed on serve_authed
    (truthy token) — verify_bearer('Bearer ', '') would return True on an empty
    expected token, so the constructor caller must never pass an empty token.
    """

    def __init__(self, app, token: str, exempt_paths=frozenset({"/health"})):
        self._app = app
        self._token = token
        self._exempt = exempt_paths

    async def __call__(self, scope, receive, send):
        # F5: lifespan passes through; exempt http paths pass through; any other
        # non-http scope (websocket etc.) is REJECTED — never auth-exempt.
        if scope["type"] == "lifespan" or (
            scope["type"] == "http" and scope.get("path") in self._exempt
        ):
            return await self._app(scope, receive, send)
        if scope["type"] != "http":
            raise RuntimeError(
                f"unsupported scope for authed transport: {scope['type']}"
            )
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        if verify_bearer(headers.get("authorization"), self._token):
            return await self._app(scope, receive, send)
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", b"Bearer"),
                (b"content-type", b"application/json"),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"error":"unauthorized"}',
        })
