"""Config value types.  Separate module so profiles.py and config.py can both
import them without a cycle."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class SourceConfig:
    adapter: str
    enabled: bool = True
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedConfig:
    profile: str
    sources: Dict[str, SourceConfig] = field(default_factory=dict)
    extensions: Dict[str, str] = field(default_factory=dict)
