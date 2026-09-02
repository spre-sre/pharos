"""Task 5: cosmetic fixes — scope echo names the source; replicas never None."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_default_scope_uses_source_name(source_code):
    """Pin via source text: the default-scope literal must use
    `source or "current"`, not a hardcoded "current"."""
    assert '"clusters": [source or "current"]' in source_code


def test_effective_replicas_helper():
    from types import SimpleNamespace
    from helpers.resource_topology import _effective_replicas

    dep = lambda status, spec: SimpleNamespace(
        status=SimpleNamespace(replicas=status),
        spec=SimpleNamespace(replicas=spec))
    assert _effective_replicas(dep(3, 5)) == 3
    assert _effective_replicas(dep(None, 5)) == 5
    assert _effective_replicas(dep(None, None)) == 1


def test_replica_detail_never_prints_none():
    from helpers import resource_topology
    src = Path(resource_topology.__file__).read_text()
    assert "{deployment.status.replicas} replicas" not in src
    assert "_effective_replicas" in src
