import inspect

import pytest

from .cases import CASES
from .conftest import apply_determinism
from .golden_utils import assert_matches_golden

pytestmark = pytest.mark.characterization


@pytest.fixture(autouse=True)
def deterministic(server, monkeypatch, tmp_path):
    """Write-once determinism harness. Order matters: runs before each case.

    1. Seed RNGs: run_monte_carlo_simulation uses random.gauss
       (helpers/utils.py:2286), synthetic metrics too (utils.py:2754).
    2. Reset session caches mutated *inside* tools, otherwise the first case
       to populate them poisons every later case (order-dependent goldens):
       _namespace_cache (server-mcp.py:382), _prometheus_endpoint_cache
       (server-mcp.py:379). Verify both shapes at those lines on first run.
    3. Redirect HOME to tmp: ml_persistence writes ~/.lumino/models via
       Path.home(); phase 0 must not touch the real home (and stale local
       models must not leak into goldens).
    4. Strict-fake ALL seven client globals: an unpatched global would fall
       through to the real client against the fake kubeconfig (127.0.0.1:1)
       and leak nondeterministic connection-error text into goldens. Cases
       override the ones they need via ToolCase.patches.
    """
    apply_determinism(server, monkeypatch, tmp_path)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
@pytest.mark.asyncio
async def test_golden(case, server, monkeypatch):
    for attr, replacement in case.patches.items():
        monkeypatch.setattr(server, attr, replacement)
    fn = getattr(server, case.name)
    result = fn(**case.kwargs)
    if inspect.isawaitable(result):
        result = await result
    assert_matches_golden(case.case_id, result)
