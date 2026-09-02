"""Tripwire: no stale identity literals in CI and branding files.

Scope (T3): .github/workflows/** only (recursive — catches future subdirs).
T4 widens the path list to cover the full .github/** tree (incl. ISSUE_TEMPLATE),
Containerfile, scripts/smoke_container.sh, pyproject.toml, server.json, .mcp.json,
and the five branding docs (README.md, SECURITY.md, CONTRIBUTING.md, docs/help.1,
docs/website/index.html).

Literal set after T4:
  lumino-mcp-server  — the old distribution slug
  LUMINO MCP Server  — all-caps product name (old)
  Lumino MCP Server  — mixed-case product name (old; see server.json:4/release.yml:40,42)
  lumino-dev         — the old .mcp.json dev server key

Retained strings that must NOT be in the forbidden set:
  'geored'  (the registry org — kept deliberately)
  'quay.io' (the registry host — kept deliberately)
  'LUMINO_' (env vars — DEFERRED, retained for backward compat)
  'lumino-mcp' (logger tree — DEFERRED, retained for backward compat)

EXCEPTIONS (R2b/R3 — allowlisted by exact relpath+substring):
  server.json:     "identifier": "lumino-mcp-server"     — published PyPI 0.9.3 name; the MCP
                   registry cross-checks identifier against the PyPI package.
"""

import pathlib

REPO = pathlib.Path(__file__).parent.parent

# Widen: full .github/** tree (workflows + ISSUE_TEMPLATE + PR template),
# Containerfile, scripts/smoke_container.sh, key config files, and the five
# branding docs (verified false-trip-free after T4 identity sweep).
_GITHUB_FILES = sorted((REPO / ".github").rglob("*"))
_CONFIG_FILES = [
    REPO / "Containerfile",
    REPO / "scripts" / "smoke_container.sh",
    REPO / "pyproject.toml",
    REPO / "server.json",
    REPO / ".mcp.json",
]
_BRANDING_DOCS = [
    REPO / "README.md",
    REPO / "SECURITY.md",
    REPO / "CONTRIBUTING.md",
    REPO / "docs" / "help.1",
    REPO / "docs" / "website" / "index.html",
]
PATHS = sorted(set(_GITHUB_FILES + _CONFIG_FILES + _BRANDING_DOCS))

# 'spre-sre' was removed from this list 2026-09-02: the team ratified
# github.com/spre-sre/pharos as the new home (SPRE-6613), so the org name is
# current identity, not stale. 'lumino-mcp-server' still catches stale repo
# references (e.g. spre-sre/lumino-mcp-server).
FORBIDDEN = [
    "lumino-mcp-server",
    "LUMINO MCP Server",
    "Lumino MCP Server",
    "Lumino MCP server",   # F4: lowercase-s variant slipped past the capital-S literal
    "lumino-dev",
]

# Exceptions: (relpath, substring) pairs where the literal is legitimately retained.
# Allowlisted by exact path+key with R2b/R3 justification (see module docstring).
_EXCEPTIONS: set[tuple[str, str]] = {
    # (pyproject.toml exception removed 2026-09-02 — renamed to pharos-mcp-server
    # in the spre-sre/pharos initial import)
    ("server.json", '"identifier": "lumino-mcp-server"'),  # R2b: PyPI pkg cross-check
    # The _comment_identifier field in server.json also contains the literal:
    ("server.json", '"_comment_identifier"'),
}


def test_no_stale_identity_in_ci():
    """Zero stale identity literals in CI, config, and branding files (T4-widened scope)."""
    assert PATHS, "No files found — directory layout has changed?"
    hits: list[str] = []
    for path in PATHS:
        if not path.is_file():
            continue
        relpath = str(path.relative_to(REPO))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # skip non-UTF-8 assets (e.g. binary blobs if ever added)
        for lineno, line in enumerate(text.splitlines(), 1):
            for literal in FORBIDDEN:
                if literal in line:
                    # Check exception list: if ANY exception substring is in this line
                    # AND it matches the file, skip.
                    if any(
                        relpath == exc_path and exc_sub in line
                        for exc_path, exc_sub in _EXCEPTIONS
                    ):
                        continue
                    hits.append(f"{relpath}:{lineno}: found {literal!r}")
    assert hits == [], "Stale identity literals found in CI/branding files:\n" + "\n".join(hits)
