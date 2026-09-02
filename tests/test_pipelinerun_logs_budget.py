"""Bug 2 (memory: pharos-tool-bugs-live-testing) — get_pipelinerun_logs
default token budget must fit the MCP client response cap.

Live numbers 2026-08-21: with max_token_budget=25000 the tool reported 97.5%
of budget used (19.5k estimated tokens, chars/3 heuristic on raw logs), but
the 61,006-char JSON response exceeded the client's 25k-token cap and spilled
to disk. Real ratio for JSON-escaped log text ≈ 2.4 chars/token, so chars/3
under-estimates by ~25%. The old default (120,000) therefore overflowed the
client cap on EVERY real pipeline.

Contract: the default max_token_budget is at most 18,000 — with the 80%
safety buffer and the ~25% estimator optimism that lands ≈17.6k real client
tokens, inside the 25k cap with headroom.
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_FILE = os.path.join(REPO_ROOT, "src", "server-mcp.py")


def _default_of(func_name, param_name):
    with open(SERVER_FILE) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            args = node.args
            params = args.args + args.kwonlyargs
            defaults = list(args.defaults) + list(args.kw_defaults)
            # align defaults to trailing positional params
            pos_with_defaults = args.args[len(args.args) - len(args.defaults):]
            for p, d in zip(pos_with_defaults, args.defaults):
                if p.arg == param_name and isinstance(d, ast.Constant):
                    return d.value
            for p, d in zip(args.kwonlyargs, args.kw_defaults):
                if p.arg == param_name and isinstance(d, ast.Constant):
                    return d.value
    raise AssertionError(f"{func_name}({param_name}=...) not found")


def test_default_budget_fits_client_cap():
    default = _default_of("get_pipelinerun_logs", "max_token_budget")
    assert default <= 18000, (
        f"default max_token_budget={default} overflows the 25k-token MCP "
        f"client cap (chars/3 estimator is ~25% optimistic on JSON-escaped "
        f"log text); must be <= 18000"
    )
