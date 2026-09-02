"""Task-6 regression: TERMINAL_FAILURE_PR_STATUSES shared constant.

Three assertions:
1. The constant is importable from helpers.constants with exact frozenset membership.
2. helpers.__init__ re-exports it (so callers use ``from helpers import …``).
3. The inline list ``["Failed", "Error", "CouldntGetTask"]`` no longer exists in
   server-mcp.py; the constant name appears in manage_prediction_training_data instead.
"""
import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))


def test_constant_importable_from_helpers_constants():
    """TERMINAL_FAILURE_PR_STATUSES is in helpers.constants with correct membership."""
    from helpers.constants import TERMINAL_FAILURE_PR_STATUSES  # noqa: PLC0415

    assert isinstance(TERMINAL_FAILURE_PR_STATUSES, frozenset)
    assert TERMINAL_FAILURE_PR_STATUSES == frozenset({"Failed", "Error", "CouldntGetTask"})


def test_constant_reexported_from_helpers():
    """helpers package re-exports TERMINAL_FAILURE_PR_STATUSES."""
    from helpers import TERMINAL_FAILURE_PR_STATUSES  # noqa: PLC0415

    assert TERMINAL_FAILURE_PR_STATUSES == frozenset({"Failed", "Error", "CouldntGetTask"})


def test_constant_in_helpers_all():
    """TERMINAL_FAILURE_PR_STATUSES is listed in helpers.__all__."""
    import helpers  # noqa: PLC0415

    assert "TERMINAL_FAILURE_PR_STATUSES" in helpers.__all__


def test_inline_list_absent_from_server_mcp():
    """The inline literal list must NOT appear in server-mcp.py."""
    server_src = (REPO / "src" / "server-mcp.py").read_text()
    assert '["Failed", "Error", "CouldntGetTask"]' not in server_src, (
        "Inline list still present in server-mcp.py — replace with TERMINAL_FAILURE_PR_STATUSES"
    )


def test_constant_name_in_manage_prediction_training_data():
    """manage_prediction_training_data uses TERMINAL_FAILURE_PR_STATUSES (not the inline list)."""
    server_src = (REPO / "src" / "server-mcp.py").read_text()

    # Find the function body by locating the def and scanning forward
    fn_start = server_src.find("async def manage_prediction_training_data(")
    assert fn_start != -1, "manage_prediction_training_data not found in server-mcp.py"

    # Find next top-level async def after this one to bound the search
    fn_end = server_src.find("\nasync def ", fn_start + 1)
    if fn_end == -1:
        fn_end = len(server_src)

    fn_body = server_src[fn_start:fn_end]
    assert "TERMINAL_FAILURE_PR_STATUSES" in fn_body, (
        "TERMINAL_FAILURE_PR_STATUSES not found inside manage_prediction_training_data — "
        "did you forget to replace the inline list?"
    )
