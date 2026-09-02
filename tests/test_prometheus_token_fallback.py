"""Tests for _get_k8s_bearer_token() kubeconfig fallback (Method 2).

Root cause: the original Method 2 called _get_kube_config_loader() with no
filename argument — always raises AttributeError — and the subsequent
api_key read is always None with this kubernetes lib version.  Containers
without the `oc` binary therefore returned None from _get_k8s_bearer_token,
causing unauthenticated Prometheus queries (401).

These tests verify that Method 2 now reads the token directly from the
kubeconfig YAML, honouring multi-path $KUBECONFIG.
"""
import os
import sys
import subprocess
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from helpers.prometheus import _get_k8s_bearer_token


def _write_kubeconfig(path: Path, token: str) -> None:
    """Write a minimal kubeconfig with a single user whose token is *token*."""
    path.write_text(
        f"""\
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://api.example.com:6443
  name: test-cluster
contexts:
- context:
    cluster: test-cluster
    user: test-user
  name: test-context
current-context: test-context
users:
- name: test-user
  user:
    token: "{token}"
"""
    )


@pytest.mark.asyncio
async def test_kubeconfig_token_fallback_without_oc(tmp_path, monkeypatch):
    """Method 2 reads token from kubeconfig when oc is unavailable.

    Setup:
    - KUBECONFIG → a temp file with token "sha256~test-fallback-token"
    - subprocess.run raises FileNotFoundError (simulates missing oc binary)
    - PROMETHEUS_TOKEN / OPENSHIFT_TOKEN / OC_TOKEN are absent (Methods 4+5 blocked)
    - SA token file does not exist at the standard path (Method 4 blocked)

    Expected: _get_k8s_bearer_token() returns "sha256~test-fallback-token".
    """
    kubeconfig = tmp_path / "config"
    token = "sha256~test-fallback-token"
    _write_kubeconfig(kubeconfig, token)

    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.delenv("PROMETHEUS_TOKEN", raising=False)
    monkeypatch.delenv("OPENSHIFT_TOKEN", raising=False)
    monkeypatch.delenv("OC_TOKEN", raising=False)

    def _raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("oc: No such file or directory")

    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

    result = await _get_k8s_bearer_token()

    assert result == token, (
        f"Expected token '{token}' from kubeconfig; got {result!r}"
    )


@pytest.mark.asyncio
async def test_kubeconfig_multipath_and_missing_files(tmp_path, monkeypatch):
    """Method 2 honours multi-path $KUBECONFIG, skipping nonexistent files.

    KUBECONFIG = "<nonexistent>:<valid path>" — the nonexistent path must be
    silently skipped and the token must be read from the valid second path.
    """
    kubeconfig = tmp_path / "config"
    token = "sha256~multipath-token"
    _write_kubeconfig(kubeconfig, token)

    nonexistent = tmp_path / "does-not-exist"
    multipath = str(nonexistent) + os.pathsep + str(kubeconfig)

    monkeypatch.setenv("KUBECONFIG", multipath)
    monkeypatch.delenv("PROMETHEUS_TOKEN", raising=False)
    monkeypatch.delenv("OPENSHIFT_TOKEN", raising=False)
    monkeypatch.delenv("OC_TOKEN", raising=False)

    def _raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("oc: No such file or directory")

    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

    result = await _get_k8s_bearer_token()

    assert result == token, (
        f"Expected token '{token}' from second path in KUBECONFIG; got {result!r}"
    )


@pytest.mark.asyncio
async def test_malformed_context_entry_skipped_not_fatal(tmp_path, monkeypatch):
    """Finding 1: a context entry missing the 'context' key is skipped, not fatal.

    With unhardened dict comprehension (``c["context"]``), iterating a contexts
    list that contains an entry with 'name' but no 'context' key raises KeyError.
    The inner except swallows it and abandons the ENTIRE file — even when the
    current-context entry is valid and has a reachable token.  This test fails
    against that unhardened code because the function returns the env-var canary
    instead of the kubeconfig token.

    With hardened code (``c.get("context") or {}``), the bad entry maps to an
    empty dict, is treated as a non-matching context (falsy ctx), and the valid
    current-context entry is resolved correctly.

    PROMETHEUS_TOKEN is set as a canary: if the function returns it, the kubeconfig
    was abandoned prematurely (regression indicator).
    """
    kubeconfig = tmp_path / "config"
    # contexts list has a bad entry (no 'context' key) BEFORE the valid one
    kubeconfig.write_text(
        """\
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://api.example.com:6443
  name: test-cluster
contexts:
- name: bad-entry
- context:
    cluster: test-cluster
    user: good-user
  name: good-ctx
current-context: good-ctx
users:
- name: good-user
  user:
    token: "sha256~hardened-token"
"""
    )

    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv("PROMETHEUS_TOKEN", "sha256~env-fallback-canary")
    monkeypatch.delenv("OPENSHIFT_TOKEN", raising=False)
    monkeypatch.delenv("OC_TOKEN", raising=False)

    def _raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("oc: No such file or directory")

    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

    result = await _get_k8s_bearer_token()

    assert result == "sha256~hardened-token", (
        f"Expected kubeconfig token 'sha256~hardened-token'; got {result!r}. "
        "If 'sha256~env-fallback-canary' was returned, the malformed context entry "
        "caused the file to be abandoned (finding 1 not applied)."
    )
