"""Credential reference parsing (kubeconfig:/secret:/env: refs)."""
from __future__ import annotations

import re


def _parse_credential_ref(ref: str):
    """Parse a credential reference string into (ok, data).

    Positive scheme match FIRST:
      kubeconfig:<path>#<context>  — exactly one '#', non-empty path and context
      secret:<path>                — non-empty path after 'secret:'
      env:<VAR>                    — [A-Z_][A-Z0-9_]* var name

    Non-matching input:
      if it looks raw (\\n present, or contains 'apiVersion:'/'-----BEGIN'/'token:',
      or >512 chars) → (False, {"code": "raw_credential_rejected"})
      otherwise       → (False, {"code": "unknown_ref_scheme"})

    Malformed but recognised scheme prefix →  (False, {"code": "bad_ref_grammar"})

    Returns:
      (True,  {"scheme": "kubeconfig"|"secret"|"env", "path"|"var": ..., "context": ...})
      (False, {"code": "raw_credential_rejected"|"unknown_ref_scheme"|"bad_ref_grammar"})
    """
    if ref.startswith("kubeconfig:"):
        rest = ref[len("kubeconfig:"):]
        parts = rest.split("#")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return False, {"code": "bad_ref_grammar"}
        return True, {"scheme": "kubeconfig", "path": parts[0], "context": parts[1]}

    if ref.startswith("secret:"):
        rest = ref[len("secret:"):]
        if not rest:
            return False, {"code": "bad_ref_grammar"}
        return True, {"scheme": "secret", "path": rest}

    if ref.startswith("env:"):
        rest = ref[len("env:"):]
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", rest):
            return False, {"code": "bad_ref_grammar"}
        return True, {"scheme": "env", "var": rest}

    # Non-matching: detect raw credential bodies
    if ("\n" in ref or "apiVersion:" in ref or "-----BEGIN" in ref
            or "token:" in ref or len(ref) > 512):
        return False, {"code": "raw_credential_rejected"}

    return False, {"code": "unknown_ref_scheme"}
