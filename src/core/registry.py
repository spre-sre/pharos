"""Adapter registry (spec SS4.2/SS4.4).  2a entries are declarative records;
adapter objects attach in 2b.  All enumerations are name-sorted (the test
suite runs PYTHONHASHSEED=0; insertion/hash order must never reach output)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.config_types import ResolvedConfig

ADAPTER_CAPABILITIES = {
    "kubernetes": ("Log", "Event", "Inventory"),
    "prometheus": ("Metric",),
    "kubearchive": ("Log", "Event"),
    "loki": ("Log",),
    "elasticsearch": ("Log",),  # TODO(4b): restore Event/Metric as they ship
    "file": ("Log",),
    "otlp": ("Log",),  # TODO(5b): restore Event/Metric as they ship
}


@dataclass(frozen=True)
class SourceEntry:
    name: str
    adapter: str
    capabilities: Tuple[str, ...]
    state: str
    default: bool = False  # phase 2e: explicit default anchor (NOT rendered by list_sources)


class AdapterRegistry:
    def __init__(self, entries: List[SourceEntry]):
        self._entries = {e.name: e for e in entries}

    def entries(self) -> List[SourceEntry]:
        return [self._entries[n] for n in sorted(self._entries)]

    def get(self, name: str) -> SourceEntry:
        try:
            return self._entries[name]
        except KeyError:
            raise KeyError(
                f"unknown source {name!r}; known sources are available via list_sources"
            ) from None

    def capable_of(self, capability: str) -> List[str]:
        return sorted(n for n, e in self._entries.items()
                      if capability in e.capabilities)

    def add_instance(self, entry: SourceEntry) -> None:
        """Append a runtime-discovered instance.  Raises ValueError on duplicate name."""
        if entry.name in self._entries:
            raise ValueError(
                f"duplicate source name {entry.name!r}; already registered")
        self._entries[entry.name] = entry

    def remove_instance(self, name: str) -> None:
        """Remove a runtime-added instance (rollback on dial failure).

        Idempotent: silently returns if the name is not present.
        Refuses to remove entries that carry default=True (those are
        build-time anchors, not runtime additions).
        """
        entry = self._entries.get(name)
        if entry is None:
            return  # already absent — idempotent
        if entry.default:
            raise ValueError(
                f"cannot remove default instance {name!r}; it is the "
                f"build-time anchor for its adapter family")
        del self._entries[name]

    def default_instance_of(self, adapter: str) -> Optional[str]:
        """The EXPLICITLY-MARKED default instance; falls back to sorted()[0]
        ONLY when no entry of that adapter carries default=True (pure-config
        registries built before the marker existed keep working)."""
        matches = [e for e in self._entries.values() if e.adapter == adapter]
        if not matches:
            return None
        # Prefer the entry explicitly marked default=True
        for e in matches:
            if e.default:
                return e.name
        # Back-compat: no marker present → sorted()[0]
        return sorted(e.name for e in matches)[0]

    def default_kubernetes_instance(self) -> Optional[str]:
        return self.default_instance_of("kubernetes")


def build_registry(cfg: ResolvedConfig) -> AdapterRegistry:
    # Pre-compute which kubernetes entry to mark default (sorted()[0] at build time).
    # Exactly ONE entry carries default=True per adapter (here: kubernetes only).
    # Runtime-added entries (add_instance) are NOT marked default=True, preserving
    # the build-time anchor even if a discovered name sorts earlier (round-1 F5).
    k8s_names_sorted = sorted(
        name for name, sc in cfg.sources.items()
        if sc.enabled and sc.adapter == "kubernetes"
    )
    default_k8s_name = k8s_names_sorted[0] if k8s_names_sorted else None

    entries = []
    for name, sc in cfg.sources.items():
        if not sc.enabled:
            continue
        if sc.adapter not in ADAPTER_CAPABILITIES:
            raise ValueError(
                f"unknown adapter type {sc.adapter!r} for source {name!r}; "
                f"known: {', '.join(sorted(ADAPTER_CAPABILITIES))}")
        entries.append(SourceEntry(
            name=name, adapter=sc.adapter,
            capabilities=ADAPTER_CAPABILITIES[sc.adapter],
            state="configured",
            default=(name == default_k8s_name)))
    return AdapterRegistry(entries)
