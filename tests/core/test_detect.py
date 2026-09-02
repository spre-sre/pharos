"""Unit tests for extension detect() methods (Task 6, phase 2d).

Six pure-async cases: each of the three in-tree extensions (tekton, openshift,
konflux) tested against a canned discover_api_groups result that includes or
excludes the sentinel API group.

No network I/O, no server import — all dependencies are stubbed via
SimpleNamespace.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.extension import DetectContext
from core.config_types import ResolvedConfig


def _make_ctx(groups: frozenset) -> DetectContext:
    """Return a DetectContext whose discover_api_groups always returns *groups*."""
    async def _discover(_instance: str) -> frozenset:
        return groups

    return DetectContext(
        config=ResolvedConfig(profile="test", extensions={}),
        adapters=None,
        instance="kubernetes",
        discover_api_groups=_discover,
    )


# ── Tekton ────────────────────────────────────────────────────────────────────

def test_tekton_detect_present():
    """detect() returns True when 'tekton.dev' is in the API group list."""
    from extensions.tekton import EXTENSION

    ctx = _make_ctx(frozenset({"apps", "tekton.dev", "batch"}))
    result = asyncio.run(EXTENSION.detect(ctx))
    assert result is True


def test_tekton_detect_absent():
    """detect() returns False when 'tekton.dev' is absent."""
    from extensions.tekton import EXTENSION

    ctx = _make_ctx(frozenset({"apps", "batch", "networking.k8s.io"}))
    result = asyncio.run(EXTENSION.detect(ctx))
    assert result is False


# ── OpenShift ─────────────────────────────────────────────────────────────────

def test_openshift_detect_present():
    """detect() returns True when 'config.openshift.io' is in the API group list."""
    from extensions.openshift import EXTENSION

    ctx = _make_ctx(frozenset({"config.openshift.io", "apps", "route.openshift.io"}))
    result = asyncio.run(EXTENSION.detect(ctx))
    assert result is True


def test_openshift_detect_absent():
    """detect() returns False when 'config.openshift.io' is absent."""
    from extensions.openshift import EXTENSION

    ctx = _make_ctx(frozenset({"apps", "batch", "tekton.dev"}))
    result = asyncio.run(EXTENSION.detect(ctx))
    assert result is False


# ── Konflux ───────────────────────────────────────────────────────────────────

def test_konflux_detect_present():
    """detect() returns True when 'appstudio.redhat.com' is in the API group list."""
    from extensions.konflux import EXTENSION

    ctx = _make_ctx(frozenset({"appstudio.redhat.com", "apps", "tekton.dev"}))
    result = asyncio.run(EXTENSION.detect(ctx))
    assert result is True


def test_konflux_detect_absent():
    """detect() returns False when 'appstudio.redhat.com' is absent."""
    from extensions.konflux import EXTENSION

    ctx = _make_ctx(frozenset({"apps", "batch", "config.openshift.io"}))
    result = asyncio.run(EXTENSION.detect(ctx))
    assert result is False
