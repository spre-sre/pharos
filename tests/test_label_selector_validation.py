"""
tests/test_label_selector_validation.py

F-04 correctness pins for cleanup2b task 1.

F-04 — SILENT SELECTOR DROP: build_advanced_label_selector silently drops a
       malformed selector (empty value for not_equals/in/not_in), and because
       the resulting selector is empty it matches EVERY resource in the namespace
       — the caller gets everything back with no indication anything went wrong.

Steps 0-4: unit tests for the guard in helpers/utils.py, plus an integration
test through search_resources_by_labels that verifies the ValueError propagates
to a structured error response.
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

FAKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1"}
  name: fake
contexts:
- context: {cluster: fake, user: fake}
  name: fake
current-context: fake
users:
- name: fake
  user: {token: "fake-token"}
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once per module against a fake kubeconfig.

    Uses a distinct sys.modules key ("server_mcp_lsv") so this import
    coexists with other module-scoped server fixtures without collision.
    """
    _orig_kubeconfig = os.environ.get("KUBECONFIG")
    _orig_kubearchive = os.environ.get("KUBEARCHIVE_ENABLED")
    _orig_telemetry = os.environ.get("LUMINO_DISABLE_TELEMETRY")

    kubeconfig = tmp_path_factory.mktemp("kube_lsv") / "config"
    kubeconfig.write_text(FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_lsv", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_lsv"] = mod
    spec.loader.exec_module(mod)

    yield mod

    def _restore_env(key, original):
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    _restore_env("KUBECONFIG", _orig_kubeconfig)
    _restore_env("KUBEARCHIVE_ENABLED", _orig_kubearchive)
    _restore_env("LUMINO_DISABLE_TELEMETRY", _orig_telemetry)
    sys.modules.pop("server_mcp_lsv", None)
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


def _get_build_selector():
    """Import build_advanced_label_selector from helpers.utils at call time."""
    sys.path.insert(0, str(SRC))
    import importlib
    utils = importlib.import_module("helpers.utils")
    return utils.build_advanced_label_selector


# ── Step 0: F-04 RED — not_equals with empty string ─────────────────────────


def test_not_equals_empty_value_raises():
    """not_equals with value="" must raise ValueError (not silently return "").

    Pre-fix: returns "" (the match-all selector) — no exception.
    Post-fix: raises ValueError with a message mentioning non-empty string.
    """
    build = _get_build_selector()
    with pytest.raises(ValueError):
        build([{"key": "env", "value": "", "operator": "not_equals"}])


# ── Step 1: F-04 RED — in operator ──────────────────────────────────────────


def test_in_empty_value_raises():
    """in with value="" must raise ValueError (not silently return "").

    Pre-fix: returns "" (the match-all selector) — no exception.
    Post-fix: raises ValueError.
    """
    build = _get_build_selector()
    with pytest.raises(ValueError):
        build([{"key": "env", "value": "", "operator": "in"}])


def test_in_nonstring_value_raises():
    """in with a list value must raise ValueError.

    Pre-fix: returns 'env!=[\"a\", \"b\"]' — which is invalid and silent.
    Post-fix: raises ValueError.
    """
    build = _get_build_selector()
    with pytest.raises(ValueError):
        build([{"key": "env", "value": ["a", "b"], "operator": "in"}])


# ── Step 2: F-04 RED — not_in operator ──────────────────────────────────────


def test_not_in_empty_value_raises():
    """not_in with value="" must raise ValueError.

    Pre-fix: returns "" (the match-all selector) — no exception.
    Post-fix: raises ValueError.
    """
    build = _get_build_selector()
    with pytest.raises(ValueError):
        build([{"key": "env", "value": "", "operator": "not_in"}])


def test_not_in_nonstring_raises():
    """not_in with a list value must raise ValueError.

    Pre-fix: returns '' (silent drop) — no exception.
    Post-fix: raises ValueError.
    """
    build = _get_build_selector()
    with pytest.raises(ValueError):
        build([{"key": "env", "value": ["a", "b"], "operator": "not_in"}])


# ── Step 3: F-04 RED — integration through search_resources_by_labels ────────


@pytest.mark.asyncio
async def test_search_resources_malformed_selector_returns_structured_error(
    server, monkeypatch
):
    """A malformed selector propagates to a structured error response.

    Verifies that ValueError from build_advanced_label_selector is caught
    by the outer except handler and surfaces as a structured error dict
    with error_details[0]["error_code"] == "SYSTEM_ERROR" and a message
    mentioning "non-empty string value".

    Pre-fix: empty selector matches all pods; tool returns resources with no
             error_details — the caller silently gets all resources back.
    Post-fix: tool returns a dict with error_details containing SYSTEM_ERROR.
    """
    monkeypatch.setattr(server, "k8s_core_api", MagicMock())
    monkeypatch.setattr(server, "k8s_apps_api", MagicMock())

    result = await server.search_resources_by_labels(
        resource_types=["pods"],
        label_selectors=[{"key": "env", "value": "", "operator": "not_equals"}],
        namespaces=["build-service"],
    )

    assert isinstance(result, dict), (
        f"Expected dict response, got {type(result).__name__}: {result!r}"
    )
    error_details = result.get("error_details", [])
    assert len(error_details) > 0, (
        f"Expected error_details to be non-empty; got result: {result!r}"
    )
    assert error_details[0]["error_code"] == "SYSTEM_ERROR", (
        f"Expected SYSTEM_ERROR, got {error_details[0].get('error_code')!r}; "
        f"full error: {error_details[0]}"
    )
    assert "non-empty string value" in error_details[0]["error_message"], (
        f"Expected 'non-empty string value' in error_message; "
        f"got {error_details[0].get('error_message')!r}"
    )


# ── Step 4: F-04 GREEN regression — valid selectors still work ───────────────


def test_valid_not_equals_selector_passes():
    """not_equals with a non-empty string value must NOT raise.

    Verifies the guard does not reject valid input.
    """
    build = _get_build_selector()
    result = build([{"key": "env", "value": "production", "operator": "not_equals"}])
    assert result == "env!=production", (
        f"Expected 'env!=production', got {result!r}"
    )
