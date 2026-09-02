#!/usr/bin/env python3
"""
LUMINO MCP Server - Main Entry Point

This module serves as the entry point for the LUMINO MCP (Model Context Protocol) server.
It imports and runs the MCP server with proper configuration for both local and
Kubernetes environments.
"""

import os
import sys
import logging
import importlib.util
import threading
import time
from pathlib import Path
from typing import Optional

# Add the src directory to the Python path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

import uvicorn

from core.http_transport import (  # noqa: E402 — after sys.path setup
    BearerASGIMiddleware,
    resolve_http_serving,
    resolve_transport,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("lumino-mcp-main")


def load_server_module():
    """Load src/server-mcp.py (hyphenated filename) as module "server_mcp".

    The module MUST be registered in sys.modules BEFORE exec_module: the
    extension-activation block at its module end resolves itself via
    sys.modules[__name__] (standard importlib pattern for spec-loaded
    modules; tests/characterization/conftest.py does the same).
    """
    spec = importlib.util.spec_from_file_location("server_mcp", src_path / "server-mcp.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp"] = module
    spec.loader.exec_module(module)
    return module


def _start_otlp_receiver(server_module) -> Optional[uvicorn.Server]:
    """Start the OTLP ingest receiver for the first enabled OTLP source.

    Reads LUMINO_OTLP_BIND_HOST, LUMINO_OTLP_BIND_PORT, and LUMINO_OTLP_TOKEN
    from the environment at call time.  Wires the receiver's LogRing into
    server_module._otlp_rings so the adapter factory finds the same ring object
    (push-to-fetch ring identity).

    Returns the uvicorn.Server object after confirming startup so that callers
    (e.g. tests) can shut it down via ``server.should_exit = True``.
    Returns None when no enabled OTLP sources are configured.

    Exits with status 1 on any configuration or startup error — same
    fail-closed semantics as the inline block this replaces.
    """
    # V7: collect only enabled=True OTLP sources (disabled sources must NOT
    # open a listener).
    _otlp_sources = [
        (name, sc)
        for name, sc in server_module._lumino_config.sources.items()
        if sc.adapter == "otlp" and sc.enabled is True
    ]

    if not _otlp_sources:
        return None

    if len(_otlp_sources) >= 2:
        logger.error(
            "refusing to start: %d enabled otlp sources declared; "
            "only one otlp source is supported (spec §4.2.1)",
            len(_otlp_sources),
        )
        sys.exit(1)

    otlp_name, otlp_sc = _otlp_sources[0]

    # Validate options — exit 1 on error, naming the offending key.
    from adapters.otlp.config import validate_otlp_options
    from core.errors import AdapterError
    try:
        otlp_opts = validate_otlp_options(otlp_name, dict(otlp_sc.options))
    except AdapterError as exc:
        logger.error("otlp source %r config error: %s", otlp_name, str(exc))
        sys.exit(1)

    # Resolve auth/bind decision for the OTLP ingest surface.
    # Reuses the same 2f machinery.  Log host+decision ONLY — never the
    # token (M6 / spec §4.7).
    otlp_host = os.environ.get("LUMINO_OTLP_BIND_HOST", "127.0.0.1")
    otlp_token = os.environ.get("LUMINO_OTLP_TOKEN")
    otlp_decision = resolve_http_serving(otlp_host, otlp_token)

    if otlp_decision == "refuse":
        logger.error(
            "refusing to start OTLP receiver: non-localhost bind (%s) requires "
            "LUMINO_OTLP_TOKEN (fail-closed, spec §4.2.1)",
            otlp_host,
        )
        sys.exit(1)

    # Build the ring and receiver app.  Use setdefault so the ring is stored in
    # server_module._otlp_rings under the source name — this is the join point
    # for push-to-fetch ring identity: the adapter factory (_build_otlp_source)
    # also calls setdefault on the SAME dict with the SAME key, so both sides
    # get the identical LogRing object.
    from adapters.otlp.rings import LogRing
    from adapters.otlp.receiver import build_receiver_app
    otlp_ring = server_module._otlp_rings.setdefault(
        otlp_name, LogRing(capacity=otlp_opts["ring_capacity"])
    )
    # token is None for serve_open (bare app), truthy for serve_authed
    # (BearerASGIMiddleware applied inside build_receiver_app).
    otlp_app = build_receiver_app(otlp_ring, otlp_opts, otlp_token)

    # MINOR-7: name the offending env var on int() failure.
    _otlp_port_str = os.environ.get("LUMINO_OTLP_BIND_PORT", "4318")
    try:
        otlp_port = int(_otlp_port_str)
    except ValueError:
        logger.error(
            "invalid LUMINO_OTLP_BIND_PORT %r: must be an integer port number",
            _otlp_port_str,
        )
        sys.exit(1)

    logger.info(
        "OTLP receiver on %s:%s (%s)", otlp_host, otlp_port, otlp_decision
    )

    # F2: log_config=None so the receiver never re-runs dictConfig;
    # access_log=False so uvicorn's access log handler does NOT write
    # to sys.stdout — under stdio transport sys.stdout IS the JSON-RPC
    # channel and any write corrupts it.
    otlp_config = uvicorn.Config(
        otlp_app,
        host=otlp_host,
        port=otlp_port,
        log_config=None,
        access_log=False,
    )
    otlp_server = uvicorn.Server(otlp_config)

    # Daemon thread — exits with the main process (D1: identical under
    # both stdio and streamable-http transports).
    otlp_thread = threading.Thread(
        target=otlp_server.run, daemon=True, name="otlp-receiver"
    )
    otlp_thread.start()

    # Poll until started or thread dies (F8 / M7 — uvicorn's internal
    # sys.exit is swallowed by threading.excepthook, so a dead thread
    # is the fast-path signal for a bind failure).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if otlp_server.started:
            break
        if not otlp_thread.is_alive():
            break
        time.sleep(0.05)

    if not otlp_server.started:
        logger.error(
            "OTLP receiver failed to start within deadline "
            "(port %s may be in use; thread alive: %s)",
            otlp_port,
            otlp_thread.is_alive(),
        )
        sys.exit(1)

    # F9: flag is set only after the poll confirms the server is up.
    server_module._otlp_listening = True
    return otlp_server


def main():
    """Main entry point for the LUMINO MCP Server."""
    logger.info("Starting LUMINO MCP Server...")

    try:
        server_mcp = load_server_module()

        # Get the MCP server instance
        mcp = server_mcp.mcp

        # ── OTLP receiver block — runs before transport dispatch (D1) ─────────
        _start_otlp_receiver(server_mcp)

        # ── transport dispatch ─────────────────────────────────────────────────
        transport = resolve_transport(os.environ)

        if transport == "streamable-http":
            app = mcp.streamable_http_app()          # includes /mcp + /health
            token = os.environ.get("LUMINO_HTTP_TOKEN")
            decision = resolve_http_serving(mcp.settings.host, token)
            if decision == "refuse":
                logger.error(
                    "refusing to serve streamable-http: non-localhost bind (%s) requires "
                    "LUMINO_HTTP_TOKEN (fail-closed, spec 4.9)", mcp.settings.host)
                sys.exit(1)
            if decision == "serve_authed":
                app = BearerASGIMiddleware(app, token)
            logger.info(
                "streamable-http on %s:%s (%s)",
                mcp.settings.host, mcp.settings.port, decision)
            uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port,
                        log_level=mcp.settings.log_level.lower())
        else:
            logger.info("Running stdio transport")
            mcp.run()

        logger.info("MCP server finished successfully")

    except Exception as e:
        logger.error(f"Failed to start LUMINO MCP Server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
