"""Canonical selector, window, and limit types (spec SS4.2) + the four Source
capability protocols (typed here in 2a; implemented by adapters from 2b on)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Protocol

CAPABILITIES = ("Log", "Event", "Metric", "Inventory")


@dataclass(frozen=True)
class Entity:
    """A named thing: pod, host, service.  Pattern-capable."""
    name_or_pattern: str
    kind: Optional[str] = None


@dataclass(frozen=True)
class Matchers:
    """Label/attribute matchers."""
    terms: Dict[str, str] = field(default_factory=dict)

    def __hash__(self):  # dict field is unhashable by default
        return hash(tuple(sorted(self.terms.items())))


@dataclass(frozen=True)
class Native:
    """Source-native query (PromQL, LogQL, ES body).  Subject to the
    per-adapter read-only allowlist (spec SS4.7)."""
    query: str


Selector = Entity | Matchers | Native


@dataclass(frozen=True)
class TimeWindow:
    start: Optional[datetime] = None
    end: Optional[datetime] = None


@dataclass(frozen=True)
class Limit:
    max_records: Optional[int] = None
    max_bytes: Optional[int] = None


class SelectorNotSupported(ValueError):
    """Raised by adapters for selector variants they don't implement."""

    def __init__(self, requested: str, supported: tuple):
        self.requested = requested
        self.supported = supported
        super().__init__(
            f"selector variant {requested!r} not supported; "
            f"supported: {', '.join(supported)}")


class CapabilityError(ValueError):
    """A tool was addressed at a source lacking a required capability."""


def make_capability_error(tool: str, requested_source: str,
                          capable_sources: list) -> Dict[str, Any]:
    """The canonical structured error every capability rejection returns
    (spec SS4.4).  Defined ONCE here; 2b+ tools must not re-invent it."""
    return {
        "error": f"source {requested_source!r} does not support tool {tool!r}",
        "tool": tool,
        "requested_source": requested_source,
        "capable_sources": sorted(capable_sources),
    }


class LogSource(Protocol):
    async def fetch_logs(self, selector: Selector, window: TimeWindow,
                         limit: Limit): ...


class EventSource(Protocol):
    async def fetch_events(self, selector: Selector, window: TimeWindow,
                           limit: Limit): ...


class MetricSource(Protocol):
    async def fetch_metrics(self, selector: Selector, window: TimeWindow): ...


class InventorySource(Protocol):
    async def fetch_inventory(self, selector: Selector): ...
