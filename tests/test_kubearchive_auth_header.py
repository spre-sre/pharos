"""
Tests for _normalize_bearer_token helper in kubearchive_integration.py.

Verifies that regardless of how a token arrives (bare, prefixed with
"Bearer ", prefixed with "bearer " (lowercase), trailing newline, or
surrounded by whitespace), the Authorization header built from the
normalized token is always exactly "Bearer <bare-token>".

Covers the live bug: when config.api_key.get('BearerToken') already
contains "Bearer <token>", the header became "Bearer Bearer <token>"
(3 words) causing HTTP 400 from the KubeArchive API.
"""

import sys
from pathlib import Path

import pytest

# Add src/ to the path so we can import helpers directly.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from helpers.kubearchive_integration import _normalize_bearer_token


# ---------------------------------------------------------------------------
# Core normalization behaviour
# ---------------------------------------------------------------------------

class TestNormalizeBearerToken:
    """_normalize_bearer_token must produce a bare token in all cases."""

    @pytest.mark.parametrize("raw, expected", [
        ("abc",              "abc"),
        ("Bearer abc",       "abc"),
        ("bearer abc",       "abc"),
        ("abc\n",            "abc"),
        (" Bearer abc \n",   "abc"),
        ("BEARER abc",       "abc"),
        ("bEaReR abc",       "abc"),
        ("  abc  ",          "abc"),
    ])
    def test_normalizes_to_bare_token(self, raw, expected):
        assert _normalize_bearer_token(raw) == expected


# ---------------------------------------------------------------------------
# Header value constructed from normalized token
# ---------------------------------------------------------------------------

class TestAuthorizationHeader:
    """The Authorization header built from the normalized token must always
    be exactly 'Bearer <bare-token>' — two words, never three."""

    @pytest.mark.parametrize("raw_token", [
        "abc",
        "Bearer abc",
        "bearer abc",
        "abc\n",
        " Bearer abc \n",
    ])
    def test_header_value_is_exactly_bearer_bare_token(self, raw_token):
        normalized = _normalize_bearer_token(raw_token)
        header_value = f"Bearer {normalized}"
        # Must be exactly two whitespace-separated parts.
        parts = header_value.split()
        assert len(parts) == 2, (
            f"Header '{header_value}' has {len(parts)} parts; expected 2"
        )
        assert parts[0] == "Bearer"
        assert parts[1] == "abc"
