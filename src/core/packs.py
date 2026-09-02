"""Knowledge-pack loader (spec phase-2d).

A Pack is a frozen value object bundling a named set of runbooks (and optional
label-key lists).  load_packs() deserialises one YAML file per requested name
from the repo-root packs/ directory, validates the top-level keys, and returns
a name-sorted dict of Pack objects.

IMPORT-TIME INERT: no filesystem I/O until load_packs() is called.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml

_PACKS_DIR = Path(__file__).resolve().parents[2] / "packs"
_ALLOWED_TOP_LEVEL = {"name", "runbooks", "labels"}


@dataclass(frozen=True)
class Pack:
    name: str
    runbooks: dict   # key -> {title, steps|url|reference, estimated_time?, severity}
    labels: dict     # e.g. {"trace_commit": [...], "trace_pr": [...], "trace_pr_fallback": [...]} (konflux only)


def load_packs(names: Sequence[str], root: Path = _PACKS_DIR) -> dict[str, Pack]:
    """yaml.safe_load each <root>/<name>.yaml; validate top-level keys ⊆ {name, runbooks, labels};
    name field must equal filename stem; unknown file -> ValueError naming it; returns name-sorted dict."""
    result: dict[str, Pack] = {}
    for name in names:
        path = root / f"{name}.yaml"
        if not path.exists():
            raise ValueError(f"pack file not found: {name}.yaml")
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}
        unknown = set(raw) - _ALLOWED_TOP_LEVEL
        if unknown:
            raise ValueError(
                f"unknown top-level keys in {path.name}: {', '.join(sorted(unknown))}"
            )
        pack_name = raw.get("name", name)
        if pack_name != name:
            raise ValueError(
                f"pack name {pack_name!r} does not match filename stem {name!r}"
            )
        result[name] = Pack(
            name=pack_name,
            runbooks=raw.get("runbooks") or {},
            labels=raw.get("labels") or {},
        )
    return dict(sorted(result.items()))
