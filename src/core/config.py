"""Config loading (spec SS4.6).  IMPORT-TIME INERT: load_config() touches the
filesystem ONLY when given an explicit path or LUMINO_CONFIG env; absent both,
it returns a built-in profile object with zero I/O."""
from __future__ import annotations

import os
from typing import Mapping, Optional

import yaml

from core.config_types import ResolvedConfig, SourceConfig
from core.extension import INTREE_EXTENSIONS
from core.profiles import BUILTIN_PROFILES

_ALLOWED_TOP_LEVEL = {"profile", "sources", "extensions"}
# PyYAML (YAML 1.1) parses bare `off`/`on` as booleans; map them back to the
# canonical string form the extensions spec uses.
_EXT_BOOL = {True: "on", False: "off"}
_EXT_MODES = {"on", "off", "auto"}


def load_config(path: Optional[str] = None,
                env: Optional[Mapping[str, str]] = None) -> ResolvedConfig:
    env = os.environ if env is None else env
    path = path or env.get("LUMINO_CONFIG")
    if path:
        return _load_yaml(path)
    profile = env.get("LUMINO_PROFILE", "konflux")
    if profile not in BUILTIN_PROFILES:
        raise ValueError(
            f"unknown LUMINO_PROFILE {profile!r}; "
            f"built-ins: {', '.join(sorted(BUILTIN_PROFILES))}")
    return BUILTIN_PROFILES[profile]


def _load_yaml(path: str) -> ResolvedConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    unknown = set(raw) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    sources = {
        name: SourceConfig(
            adapter=body.get("adapter", name),
            enabled=body.get("enabled", True),
            options={k: v for k, v in body.items()
                     if k not in ("adapter", "enabled")},
        )
        for name, body in (raw.get("sources") or {}).items()
    }
    extensions = {
        k: (_EXT_BOOL[v] if isinstance(v, bool) else str(v))
        for k, v in (raw.get("extensions") or {}).items()
    }
    # Validate extension names and modes (Task 2: banana fix)
    for name, mode in sorted(extensions.items()):
        if name not in INTREE_EXTENSIONS:
            raise ValueError(
                f"unknown extension {name!r}; known: {', '.join(INTREE_EXTENSIONS)}")
        if mode not in _EXT_MODES:
            raise ValueError(
                f"invalid mode {mode!r} for extension {name!r}; "
                f"allowed: auto, off, on")
    return ResolvedConfig(
        profile=raw.get("profile", "custom"),
        sources=sources,
        extensions=extensions,
    )
