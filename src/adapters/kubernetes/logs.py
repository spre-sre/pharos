"""Kubernetes LogSource: pod logs as canonical LogBatch (spec SS4.2).

NOTE: no MCP tool consumes this yet - phase 1b migrates tool bodies off the
legacy get_pod_logs envelope. Introduced now so engines/tests build against
the canonical type and the fetch contract is pinned early.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from core.readonly_client import ReadOnlyCoreV1
from core.selector import Entity, Limit, Matchers, Native, SelectorNotSupported, TimeWindow
from core.signals import LogBatch, LogRecord, Provenance
from helpers.utils import get_all_pod_logs

_SENTINELS = ("no_containers", "pod_error", "no_logs")


async def fetch_pod_logs(core_api, namespace: str, pod_name: str,
                         tail_lines: Optional[int] = None,
                         since_seconds: Optional[int] = None) -> LogBatch:
    ro = ReadOnlyCoreV1.wrap(core_api)
    result = await get_all_pod_logs(
        pod_name, namespace, k8s_core_api=ro,
        tail_lines=tail_lines, since_seconds=since_seconds)
    query = {"namespace": namespace, "pod_name": pod_name,
             "tail_lines": tail_lines, "since_seconds": since_seconds}
    result = result or {}
    sentinel_notes = tuple(
        f"{k}: {result[k]}" for k in _SENTINELS if k in result)
    if sentinel_notes:
        return LogBatch(records=[], provenance=Provenance(
            adapter="kubernetes", query=query, notes=sentinel_notes))
    records = [
        LogRecord(timestamp=None, body=line, attributes={"container": container})
        for container, text in result.items()
        for line in str(text).splitlines()
    ]
    return LogBatch(records=records, provenance=Provenance(
        adapter="kubernetes", query=query,
        truncated=tail_lines is not None))


class KubernetesLogSource:
    """Thin LogSource wrapper around :func:`fetch_pod_logs`.

    Binds a kubernetes core API client and a namespace at construction.
    Entity-only selector: ``name_or_pattern`` is treated as the pod name.
    :class:`~core.selector.Matchers` and :class:`~core.selector.Native` raise
    :exc:`~core.selector.SelectorNotSupported`.

    Window handling: if ``window.start`` is set and in the past,
    ``since_seconds`` is derived and forwarded to :func:`fetch_pod_logs`.
    ``window.end`` cannot be expressed as ``since_seconds`` and is silently
    ignored.  When ``window.start`` is ``None`` or refers to a future instant,
    ``since_seconds`` is ``None``.

    MCP tools do NOT use this class in phase 3 — it exists for the shared
    LogSource contract suite (spec §7) and as the kubernetes conformance point.
    """

    def __init__(self, core_api: Any, namespace: str) -> None:
        self._core_api = core_api
        self._namespace = namespace

    async def fetch_logs(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        limit: Optional[Limit],
    ) -> LogBatch:
        """Fetch pod logs for *selector* filtered by *window* and *limit*.

        Returns a :class:`~core.signals.LogBatch`.  An unknown pod name
        returns an empty batch (the kubernetes sentinel mechanism in
        :func:`fetch_pod_logs` converts ``pod_error`` / ``no_logs`` dicts
        to empty records rather than raising).
        """
        if isinstance(selector, Matchers):
            raise SelectorNotSupported(
                requested=type(selector).__name__, supported=("Entity",)
            )
        if isinstance(selector, Native):
            raise SelectorNotSupported(
                requested=type(selector).__name__, supported=("Entity",)
            )

        pod_name: str = selector.name_or_pattern

        # Derive since_seconds from window.start when available and in the past.
        # window.end is not expressible via kubernetes since_seconds and is
        # silently ignored (phase 4 may add covered-window tracking).
        since_seconds: Optional[int] = None
        if window is not None and window.start is not None:
            now = datetime.now(timezone.utc)
            start = window.start
            # Normalise naive start to UTC for comparison.
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            delta = (now - start).total_seconds()
            if delta > 0:
                since_seconds = int(delta)

        tail_lines: Optional[int] = (
            limit.max_records if limit is not None else None
        )

        return await fetch_pod_logs(
            self._core_api,
            self._namespace,
            pod_name,
            tail_lines=tail_lines,
            since_seconds=since_seconds,
        )
