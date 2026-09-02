"""
Tests for KubeArchiveClient._extract_token_from_client().

Verifies that the bearer-prefix stripping is case-insensitive so that
tokens stored as 'bearer sha256~...' (lowercase, as some k8s clients do)
are handled the same as 'Bearer sha256~...' (capitalized).

Covers issue #158: case-insensitive Bearer prefix stripping.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest

# Add src/ to the path so we can import the module under test.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

# Import the class under test. The module has side-effects (logging setup),
# but the class itself is safe to instantiate with mocks.
from helpers.kubearchive_integration import KubeArchiveClient


def _make_client(api_key_dict: dict) -> KubeArchiveClient:
    """Build a KubeArchiveClient whose k8s_core_api exposes *api_key_dict*
    via ``k8s_core_api.api_client.configuration.api_key``."""
    config = SimpleNamespace(api_key=api_key_dict)
    api_client = SimpleNamespace(configuration=config)
    k8s_core_api = SimpleNamespace(api_client=api_client)

    discovery = MagicMock()  # endpoint_discovery — unused by the method
    return KubeArchiveClient(
        endpoint_discovery=discovery,
        k8s_core_api=k8s_core_api,
    )


# ------------------------------------------------------------------
# Acceptance-criteria tests for case-insensitive prefix stripping
# ------------------------------------------------------------------

class TestBearerPrefixStripping:
    """The method must strip *any* casing of 'bearer ' and return only the
    token payload."""

    def test_lowercase_bearer_prefix(self):
        """api_key='bearer sha256~token' -> 'sha256~token' (prefix stripped)."""
        c = _make_client({"authorization": "bearer sha256~token"})
        assert c._extract_token_from_client() == "sha256~token"

    def test_capitalized_bearer_prefix(self):
        """api_key='Bearer sha256~token' -> 'sha256~token' (no regression)."""
        c = _make_client({"authorization": "Bearer sha256~token"})
        assert c._extract_token_from_client() == "sha256~token"

    def test_uppercase_bearer_prefix(self):
        """api_key='BEARER sha256~token' -> 'sha256~token'."""
        c = _make_client({"authorization": "BEARER sha256~token"})
        assert c._extract_token_from_client() == "sha256~token"

    def test_mixed_case_bearer_prefix(self):
        """api_key='bEaReR sha256~token' -> 'sha256~token'."""
        c = _make_client({"authorization": "bEaReR sha256~token"})
        assert c._extract_token_from_client() == "sha256~token"


# ------------------------------------------------------------------
# Other paths through the method
# ------------------------------------------------------------------

class TestNoPrefixPassthrough:
    """When there is no 'bearer ' prefix the raw value is returned."""

    def test_no_prefix(self):
        """api_key='sha256~token' -> 'sha256~token' unchanged."""
        c = _make_client({"authorization": "sha256~token"})
        assert c._extract_token_from_client() == "sha256~token"


class TestBearerTokenKeyFallback:
    """When 'authorization' is absent, fall back to the 'BearerToken' key."""

    def test_bearer_token_key_fallback(self):
        c = _make_client({"BearerToken": "some-token"})
        assert c._extract_token_from_client() == "some-token"


class TestNoneReturns:
    """Cases that must return None rather than a token string."""

    def test_no_api_key_returns_none(self):
        """Neither 'authorization' nor 'BearerToken' present -> None."""
        c = _make_client({})
        assert c._extract_token_from_client() is None

    def test_no_k8s_core_api_returns_none(self):
        """self.k8s_core_api is None -> immediate None."""
        discovery = MagicMock()
        c = KubeArchiveClient(endpoint_discovery=discovery, k8s_core_api=None)
        assert c._extract_token_from_client() is None

    def test_exception_returns_none(self):
        """If config.api_key raises, the except block returns None."""
        k8s_core_api = MagicMock()
        # Make .api_client.configuration.api_key raise on access
        type(k8s_core_api.api_client.configuration).api_key = PropertyMock(
            side_effect=RuntimeError("boom"),
        )
        discovery = MagicMock()
        c = KubeArchiveClient(endpoint_discovery=discovery, k8s_core_api=k8s_core_api)
        assert c._extract_token_from_client() is None


# ------------------------------------------------------------------
# D1b: KUBEARCHIVE_TOKEN env var at priority 1.5 in _get_auth_token
# ------------------------------------------------------------------

class TestKubeArchiveTokenEnvVar:
    """_get_auth_token must read KUBEARCHIVE_TOKEN per-call (priority 1.5).

    Env-first design: unlike _get_k8s_bearer_token (server-mcp.py env-LAST),
    this is env-first because the likeliest real failure is a present-but-
    KubeArchive-unauthorized kubeconfig token — the operator must be able to
    override it without modifying the kubeconfig.
    """

    @pytest.mark.asyncio
    async def test_kubearchive_token_env_returned_when_set(self, monkeypatch):
        """D1b: KUBEARCHIVE_TOKEN env var is returned before any other source."""
        monkeypatch.setenv("KUBEARCHIVE_TOKEN", "env-test-token-abc123")
        c = _make_client({})  # _auth_token = None

        with (
            patch("helpers.kubearchive_integration.os.path.exists", return_value=False),
            patch.object(c, "_extract_token_from_client", return_value=None),
        ):
            result = await c._get_auth_token()

        assert result == "env-test-token-abc123"

    @pytest.mark.asyncio
    async def test_kubearchive_token_env_beats_client_token(self, monkeypatch):
        """D1b priority pin: env var wins even when _extract_token_from_client
        would return a real token.

        This pins the POSITION of the env read at priority 1.5 (after
        self._auth_token, before in-cluster/client/oc).  Moving the
        os.getenv block to the END of the chain leaves all other tests
        green but fails this one.

        Mutation: move env read to end of chain → this test fails; revert.
        """
        monkeypatch.setenv("KUBEARCHIVE_TOKEN", "env-override-token")
        c = _make_client({})  # _auth_token = None

        with (
            patch("helpers.kubearchive_integration.os.path.exists", return_value=False),
            # _extract_token_from_client returns a real (non-env) token:
            patch.object(c, "_extract_token_from_client", return_value="client-token-xyz"),
        ):
            result = await c._get_auth_token()

        # env token must win over the client token
        assert result == "env-override-token", (
            f"Expected env token 'env-override-token' to win over client token; "
            f"got {result!r} — env read is not at priority 1.5"
        )

    @pytest.mark.asyncio
    async def test_kubearchive_token_env_unset_chain_returns_none(self, monkeypatch):
        """D1b: when KUBEARCHIVE_TOKEN is not set the chain still returns None
        cleanly for the vanilla-k8s / no-token case."""
        monkeypatch.delenv("KUBEARCHIVE_TOKEN", raising=False)
        c = _make_client({})  # _auth_token = None

        with (
            patch("helpers.kubearchive_integration.os.path.exists", return_value=False),
            patch.object(c, "_extract_token_from_client", return_value=None),
            patch.object(c, "_is_openshift_cluster", return_value=False),
        ):
            result = await c._get_auth_token()

        assert result is None


# ------------------------------------------------------------------
# M4: subprocess.run spy — zero calls on the vanilla-k8s / no-token path
# ------------------------------------------------------------------

class TestNoSubprocessOnVanillaK8sPath:
    """After removing _create_local_dev_token, _get_auth_token must make ZERO
    subprocess.run calls for the vanilla-k8s / no-available-token case.

    Mutation (M4): re-add any subprocess.run call to the method → this spy
    fires and the test fails.
    """

    @pytest.mark.asyncio
    async def test_get_auth_token_no_subprocess_vanilla_k8s(self, monkeypatch):
        """M4 spy: subprocess.run call count must be 0."""
        monkeypatch.delenv("KUBEARCHIVE_TOKEN", raising=False)
        c = _make_client({})  # _auth_token = None

        spy_calls: list = []

        def _spy(*args, **kwargs):
            spy_calls.append((args, kwargs))
            return MagicMock(returncode=1, stdout="", stderr="")

        with (
            patch("helpers.kubearchive_integration.subprocess.run", side_effect=_spy),
            patch("helpers.kubearchive_integration.os.path.exists", return_value=False),
            patch.object(c, "_extract_token_from_client", return_value=None),
            patch.object(c, "_is_openshift_cluster", return_value=False),
        ):
            result = await c._get_auth_token()

        assert spy_calls == [], (
            f"_get_auth_token must not call subprocess.run for vanilla-k8s/no-token; "
            f"got {len(spy_calls)} call(s): {spy_calls!r}"
        )
        assert result is None


# ------------------------------------------------------------------
# M5: named-source branch coverage — ambient credentials must not bleed
# ------------------------------------------------------------------

class TestNamedSourceAuthTokenIsolation:
    """M5 mutant kill: the named-source early-return in _get_auth_token must
    be covered by a direct unit test.

    For a named source all three ambient credential sources — KUBEARCHIVE_TOKEN
    env, in-cluster SA token file, and oc whoami — must be invisible; only
    _extract_token_from_client() is consulted.

    Mutant (M5): remove the `if self._source:` early-return block in
    _get_auth_token → env check at priority 1.5 returns 'env-test-token'
    instead of None → test_named_source_ignores_ambient_credentials fails.
    """

    @pytest.mark.asyncio
    async def test_named_source_ignores_ambient_credentials(self, monkeypatch):
        """Named source + all ambient credentials active + no client token → None.

        All three ambient credential paths are armed:
          - KUBEARCHIVE_TOKEN env set to 'env-test-token'
          - SA file path is "present" (os.path.exists patched to True)
          - oc whoami stubbed to return 'oc-whoami-token'
          - _extract_token_from_client returns None (no k8s client token)

        Expected result: None — the only safe outcome when no per-source
        credential is available.

        Mutant kill: removing the `if self._source:` block causes the env check
        to be reached → returns 'env-test-token' → assertion fails.
        """
        monkeypatch.setenv("KUBEARCHIVE_TOKEN", "env-test-token")

        discovery = MagicMock()
        c = KubeArchiveClient(
            endpoint_discovery=discovery,
            k8s_auth_token=None,
            k8s_core_api=None,
            source="somecluster",
        )

        with (
            # SA file "present" — ambient credential that must stay invisible
            patch("helpers.kubearchive_integration.os.path.exists", return_value=True),
            # No k8s client token
            patch.object(c, "_extract_token_from_client", return_value=None),
            # oc whoami available — ambient credential that must stay invisible
            patch.object(c, "_is_openshift_cluster", return_value=True),
            patch.object(c, "_get_openshift_token", return_value="oc-whoami-token"),
        ):
            result = await c._get_auth_token()

        assert result is None, (
            f"Named source must not read ambient credentials (env/SA/oc-whoami); "
            f"got {result!r}. "
            "Restore the `if self._source:` early-return block in _get_auth_token."
        )

    @pytest.mark.asyncio
    async def test_named_source_returns_client_token_when_present(self, monkeypatch):
        """Named source: when _extract_token_from_client has a token, use it.

        Verifies step 1 (per-source client token) works for named sources while
        ambient paths remain blocked.  Also a mutant kill: removing the named-source
        branch causes env ('env-test-token') to win over the client token, failing
        the equality assertion.
        """
        monkeypatch.setenv("KUBEARCHIVE_TOKEN", "env-test-token")

        discovery = MagicMock()
        c = KubeArchiveClient(
            endpoint_discovery=discovery,
            k8s_auth_token=None,
            k8s_core_api=None,
            source="somecluster",
        )

        with patch.object(c, "_extract_token_from_client", return_value="client-token-xyz"):
            result = await c._get_auth_token()

        assert result == "client-token-xyz", (
            f"Named source must return the client-embedded token; got {result!r}. "
            "If env token 'env-test-token' was returned, the named-source branch "
            "is missing — ambient paths must not run for named sources."
        )

    @pytest.mark.asyncio
    async def test_default_source_ambient_chain_active(self, monkeypatch):
        """Positive control: source='' uses the ambient chain (KUBEARCHIVE_TOKEN wins).

        Verifies the ambient chain is not broken for the default source, and
        distinguishes named-source (None) from default-source ('env-test-token').
        """
        monkeypatch.setenv("KUBEARCHIVE_TOKEN", "env-test-token")

        discovery = MagicMock()
        c = KubeArchiveClient(
            endpoint_discovery=discovery,
            k8s_auth_token=None,
            k8s_core_api=None,
            source="",  # default source — uses full ambient chain
        )

        with (
            patch("helpers.kubearchive_integration.os.path.exists", return_value=False),
            patch.object(c, "_extract_token_from_client", return_value=None),
        ):
            result = await c._get_auth_token()

        assert result == "env-test-token", (
            f"Default source must use KUBEARCHIVE_TOKEN env via the ambient chain; "
            f"got {result!r}"
        )
