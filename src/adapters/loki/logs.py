"""Loki LogSource adapter (spec SS4.7 + phase-4 plan Task 3).

``LokiLogSource`` implements the LogSource protocol by querying Grafana Loki
over its HTTP API, using only the read-only endpoints
``/loki/api/v1/query`` and ``/loki/api/v1/query_range``.

Security (spec SS4.7):
- ONLY the two query endpoints above are ever requested (enforced by construction
  -- ``_LOKI_QUERY`` and ``_LOKI_QUERY_RANGE`` constants and no string concat
  outside __init__).
- Entity/Matchers patterns and values are NOT raw-interpolated: backslash is
  escaped first, then double-quote is escaped -- breaking the LogQL string
  literal is therefore impossible.  Regex metacharacters intentionally remain
  live (the caller controls the pattern semantics).
- Auth via env-var references only (bearer_env / basic_*_env in options).
- Raw tokens must NEVER appear in options or config.

Grouping (F2 from plan):
- provenance.grouping_attr = "stream".
- Every LogRecord.attributes gains a literal "stream" key whose value is the
  sorted "k=v,k=v" join of that stream's labels.  This is the key that
  ``_logbatch_to_legacy_envelope`` uses to group records; without it every
  record falls under the "log" fallback group.

Direction divergence:
- Loki defaults to forward (oldest first) but we request direction=backward
  (newest first) to match caller expectations from the kubernetes adapter.
  This divergence is recorded in provenance.notes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from adapters.http import HttpAdapterError, auth_header, http_json  # noqa: F401
from core.errors import AdapterError
from core.selector import Entity, Limit, Matchers, Native, TimeWindow
from core.signals import LogBatch, LogRecord, Provenance

_LOKI_QUERY = "/loki/api/v1/query"
_LOKI_QUERY_RANGE = "/loki/api/v1/query_range"

_DIRECTION_NOTE = (
    "direction=backward requested (newest-first); Loki default is forward; "
    "results may not be in strict chronological order when limit is applied"
)


def _escape_logql_value(value: str) -> str:
    """Escape a string value for safe interpolation inside a LogQL string literal.

    Steps (order matters):
    1. Escape backslash first (so the escape character itself becomes safe).
    2. Escape double-quote (prevents breaking the label-value string literal).

    Regex metacharacters (. * + ? [ ] ^ $ | { } ( ) are intentionally left
    as-is -- for Entity patterns they are part of the regexp semantics; for
    Matchers values they should be literal anyway but Loki equality labels do
    not use regexps so any metachar in a value is harmless inside "v".
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _compile_logql(selector: Any, entity_label: str) -> str:
    """Compile a Selector into a LogQL selector string.

    Parameters
    ----------
    selector:
        One of Entity, Matchers, or Native.
    entity_label:
        Label name to use for Entity selectors when no ``kind`` is specified.

    Returns
    -------
    str
        A LogQL stream-selector string, e.g. ``{pod=~"my-pod"}``.

    Notes
    -----
    Native selectors are forwarded verbatim -- the caller is responsible for
    the query contents.
    """
    if isinstance(selector, Native):
        return selector.query

    if isinstance(selector, Entity):
        label = selector.kind if selector.kind else entity_label
        escaped = _escape_logql_value(selector.name_or_pattern)
        return '{' + f'{label}=~"{escaped}"' + '}'

    if isinstance(selector, Matchers):
        # Sort by key for determinism (also satisfies the spec requirement).
        parts = [
            f'{k}="{_escape_logql_value(v)}"'
            for k, v in sorted(selector.terms.items())
        ]
        return "{" + ",".join(parts) + "}"

    raise AdapterError(
        f"LokiLogSource: unsupported selector type {type(selector).__name__!r}"
    )


def _to_ns_epoch(dt: datetime) -> int:
    """Convert a datetime to a nanosecond POSIX epoch integer."""
    return int(dt.timestamp() * 1_000_000_000)


def _is_windowed(window: Optional[TimeWindow]) -> bool:
    """Return True when the window imposes at least one time bound."""
    if window is None:
        return False
    return window.start is not None or window.end is not None


def _ns_to_iso(ts_ns: str) -> str:
    """Convert a Loki nanosecond epoch string to an ISO-8601 timestamp."""
    ts_s = int(ts_ns) / 1_000_000_000
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).isoformat()


def _stream_label_str(labels: Dict[str, str]) -> str:
    """Return a sorted 'k=v,k=v' string for the given label dict.

    This is the value stored as attributes["stream"] so that
    grouping_attr="stream" groups records by their stream correctly.
    """
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


class LokiLogSource:
    """LogSource backed by a Grafana Loki HTTP API.

    Parameters
    ----------
    url:
        Base URL of the Loki instance (e.g. ``http://loki.example.com:3100``).
    options:
        Dict of options read from source config:

        ``entity_label`` (str, default "pod"):
            Loki label name to use when compiling Entity selectors with no
            ``kind`` specified.

        ``tenant`` (str | None):
            If set, sent as the ``X-Scope-OrgID`` header (Loki multi-tenancy).

        ``timeout_s`` (float, default 30.0):
            HTTP request timeout in seconds.

        ``session_factory`` (callable | None):
            Injectable test seam (see adapters.http.http_json docstring).
            In production this is left as None and aiohttp.ClientSession is used.

        ``bearer_env``, ``basic_user_env``, ``basic_pass_env``:
            Env-var reference names for auth (see adapters.http.auth_header).
    """

    def __init__(self, url: str, options: Dict[str, Any]) -> None:
        self._base_url = url.rstrip("/")
        self._query_url = self._base_url + _LOKI_QUERY
        self._query_range_url = self._base_url + _LOKI_QUERY_RANGE
        self._entity_label: str = options.get("entity_label", "pod")
        self._tenant: Optional[str] = options.get("tenant")
        self._timeout_s: float = float(options.get("timeout_s", 30.0))
        self._session_factory = options.get("session_factory")  # None -> real aiohttp
        self._auth_options: Dict[str, Any] = {
            k: options[k]
            for k in ("bearer_env", "basic_user_env", "basic_pass_env")
            if k in options
        }

    def _headers(self) -> Dict[str, str]:
        """Build request headers: auth (if configured) + optional tenant."""
        hdrs = auth_header(self._auth_options)
        if self._tenant:
            hdrs = {**hdrs, "X-Scope-OrgID": self._tenant}
        return hdrs

    async def fetch_logs(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        limit: Optional[Limit],
    ) -> LogBatch:
        """Fetch log records from Loki for the given selector and window.

        Parameters
        ----------
        selector:
            Entity, Matchers, or Native (LogQL passthrough).
        window:
            Optional time bounds.  Presence of start or end routes to
            ``query_range``; absence routes to ``query``.
        limit:
            Optional result cap.  ``limit.max_records`` is forwarded as the
            Loki ``limit`` param; default is 100.

        Returns
        -------
        LogBatch
            Parsed log records with stream-label attributes and a literal
            ``stream`` grouping key.

        Raises
        ------
        HttpAdapterError
            On HTTP status >= 400 or connection failure.
        AdapterError
            On unsupported selector type.
        """
        logql = _compile_logql(selector, self._entity_label)

        windowed = _is_windowed(window)
        url = self._query_range_url if windowed else self._query_url

        effective_limit = 100
        if limit is not None and limit.max_records is not None:
            effective_limit = limit.max_records

        params: Dict[str, Any] = {
            "query": logql,
            "limit": effective_limit,
            "direction": "backward",
        }

        if windowed and window is not None:
            if window.start is not None:
                params["start"] = _to_ns_epoch(window.start)
            if window.end is not None:
                params["end"] = _to_ns_epoch(window.end)

        raw = await http_json(
            "GET",
            url,
            headers=self._headers(),
            params=params,
            timeout_s=self._timeout_s,
            session_factory=self._session_factory,
        )

        records = _parse_loki_response(raw)

        return LogBatch(
            records=records,
            provenance=Provenance(
                adapter="loki",
                query={"logql": logql, "limit": effective_limit},
                grouping_attr="stream",
                notes=(_DIRECTION_NOTE,),
            ),
        )


def _parse_loki_response(raw: dict) -> List[LogRecord]:
    """Parse a Loki query/query_range JSON response into LogRecord list.

    Expected shape::

        {
          "status": "success",
          "data": {
            "result": [
              {
                "stream": {"pod": "api-1", ...},
                "values": [["<ns_epoch>", "<log_line>"], ...]
              },
              ...
            ]
          }
        }

    Returns
    -------
    list[LogRecord]
        One LogRecord per (stream, value) pair.  attributes contains all
        stream labels PLUS a literal "stream" key (sorted k=v join) used for
        grouping by _logbatch_to_legacy_envelope when grouping_attr="stream".
    """
    records: List[LogRecord] = []

    try:
        results = raw["data"]["result"]
    except (KeyError, TypeError):
        return records

    for stream_entry in results:
        stream_labels: Dict[str, str] = stream_entry.get("stream", {})
        values: List[Tuple[str, str]] = stream_entry.get("values", [])

        stream_key = _stream_label_str(stream_labels)

        # Build attributes: all stream labels + the literal "stream" key.
        base_attrs: Dict[str, str] = {**stream_labels, "stream": stream_key}

        for ts_ns, line in values:
            records.append(LogRecord(
                timestamp=_ns_to_iso(ts_ns),
                body=line,
                severity=None,
                attributes=base_attrs,
            ))

    return records
