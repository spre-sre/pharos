"""Canonical signal types (spec SS4.1). Engines consume ONLY these."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Provenance:
    adapter: str
    query: Mapping[str, Any]
    requested_window: Optional[Tuple[str, str]] = None
    covered_window: Optional[Tuple[str, str]] = None
    truncated: bool = False
    notes: Tuple[str, ...] = ()
    grouping_attr: str = "file"


@dataclass(frozen=True)
class LogRecord:
    timestamp: Optional[str]
    body: str
    severity: Optional[str] = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LogBatch:
    records: list[LogRecord]
    provenance: Provenance

    @property
    def text(self) -> str:
        return "\n".join(r.body for r in self.records)

    @classmethod
    def from_text(cls, text: str, adapter: str, query: Mapping[str, Any],
                  **prov_kwargs: Any) -> "LogBatch":
        records = [LogRecord(timestamp=None, body=line)
                   for line in text.splitlines()]
        return cls(records=records,
                   provenance=Provenance(adapter=adapter, query=query,
                                         **prov_kwargs))


@dataclass(frozen=True)
class EventRecord:
    """One canonical event (spec SS4.1): kubernetes events, ES alert docs,
    and OTLP events all normalize here."""
    timestamp: Optional[str]
    type: str
    reason: str
    message: str
    involved_kind: str
    involved_name: str
    count: int


@dataclass(frozen=True)
class EventBatch:
    records: Tuple[EventRecord, ...]
    provenance: Provenance


@dataclass(frozen=True)
class InventoryItem:
    kind: str
    name: str
    namespace: str
    labels: Dict[str, str] = field(default_factory=dict)

    def __hash__(self):
        return hash((self.kind, self.name, self.namespace,
                     tuple(sorted(self.labels.items()))))


@dataclass(frozen=True)
class InventoryBatch:
    items: Tuple[InventoryItem, ...]
    provenance: Provenance
