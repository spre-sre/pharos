"""AST pins for get_machine_config_pool_status node listing (server-mcp.py).

The include_node_details branch called `_ro_core.list_node()` synchronously —
and INSIDE the per-pool loop, so a 4-pool cluster paid the full node list four
times, on the event loop, with no client-side timeout. These tests pin the
fixed shape: exactly one awaited, bounded node fetch, hoisted out of the loop.

Same source-inspection idiom as tests/test_async_get_kubernetes_resource.py.
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_FILE = os.path.join(REPO_ROOT, "src", "server-mcp.py")


def _find_function(name="get_machine_config_pool_status"):
    with open(SERVER_FILE) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in server-mcp.py")


def _list_node_references(node):
    """All AST nodes inside `node` that reference list_node or list_nodes_bounded."""
    refs = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in ("list_node", "list_nodes_bounded"):
            refs.append(sub)
        elif isinstance(sub, ast.Name) and sub.id == "list_nodes_bounded":
            refs.append(sub)
    return refs


def test_exactly_one_node_fetch():
    fn = _find_function()
    calls = [
        sub for sub in ast.walk(fn)
        if isinstance(sub, ast.Call) and _list_node_references(sub.func)
    ]
    assert len(calls) == 1, (
        f"expected exactly one node fetch in get_machine_config_pool_status, "
        f"found {len(calls)}"
    )


def test_node_fetch_is_awaited():
    fn = _find_function()
    awaited = [
        sub for sub in ast.walk(fn)
        if isinstance(sub, ast.Await)
        and isinstance(sub.value, ast.Call)
        and _list_node_references(sub.value.func)
    ]
    assert awaited, (
        "the node fetch must be awaited (asyncio.to_thread / list_nodes_bounded), "
        "not a bare synchronous call on the event loop"
    )


def test_node_fetch_not_inside_a_loop():
    fn = _find_function()
    for sub in ast.walk(fn):
        if isinstance(sub, (ast.For, ast.While)):
            inside = [
                c for c in ast.walk(sub)
                if isinstance(c, ast.Call) and _list_node_references(c.func)
            ]
            assert not inside, (
                "node fetch must be hoisted out of the per-pool loop — "
                "one cluster-wide list, reused for every pool"
            )
