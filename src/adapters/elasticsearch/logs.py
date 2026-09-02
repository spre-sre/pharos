"""Elasticsearch LogSource adapter (spec SS4.7 + phase-4 plan Task 4).

``ESLogSource`` implements the LogSource protocol by querying Elasticsearch
over its HTTP API, using ONLY the read-only ``/{index}/_search`` endpoint.

Security (spec SS4.7):
- ONLY ``/{index_pattern}/_search`` is ever requested (enforced by construction
  — the URL is built once in __init__ and never modified).
- ``_reject_scripts`` runs PRE-REQUEST on every body, rejecting ``"script"``
  and ``"script_score"`` keys at any nesting depth (including inside Native
  bodies).  Zero requests are made when rejection fires.
- ``enforce_body_limits`` also runs PRE-REQUEST to cap body size at 64 KB.
- Auth via env-var references only (bearer_env / basic_*_env in options).
- Raw tokens must NEVER appear in options or config.

Native selector (contrast to Loki):
- Loki Native is a raw string interpolated into a LogQL query — injection
  escaping is required (plan F4).
- ES Native is a DICT BODY from ``json.loads(selector.query)`` — JSON
  encoding structurally neutralizes injection because the body is parsed as
  a typed structure, not concatenated into a string.  A ``"script"`` key is
  still rejected by ``_reject_scripts``.

Entity selector (plan F5):
- Default ``entity_query="term"`` produces a term query on a keyword-mapped
  field (e.g. ``kubernetes.pod_name`` is assumed keyword-mapped in the
  Elasticsearch index template).
- ``entity_query="match"`` is an escape hatch for analyzed (full-text) fields,
  but it over-matches tokenized content — use only when the field is analyzed
  and exact-match semantics are not required.

Grouping:
- ``provenance.grouping_attr = "index"``
- Every ``LogRecord.attributes`` gains an ``"index"`` key set to
  ``hit._index`` so that ``_logbatch_to_legacy_envelope`` groups records
  by the index they came from.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from adapters.http import HttpAdapterError, auth_header, enforce_body_limits, http_json  # noqa: F401
from adapters.http import _reject_scripts
from core.errors import AdapterError
from core.selector import Entity, Limit, Matchers, Native, TimeWindow
from core.signals import LogBatch, LogRecord, Provenance

# ── §4.7 Native-passthrough bounded-compute guarantee — PHASE 4 STATUS ──────
#
# PARTIAL guarantee in phase 4 (Log-only v1).  What IS enforced:
#   - _reject_scripts runs PRE-REQUEST: "script" and "script_score" keys are
#     rejected at any nesting depth before a single byte reaches the server.
#   - enforce_body_limits caps the serialized body at 64 KB pre-request.
#   - A 30-second ClientTimeout is always set (never None).
#   - Only /{index_pattern}/_search is ever requested (URL built once in
#     __init__; never string-concatenated elsewhere).
#
# What is NOT YET enforced (deferred to phase 4b with query_metrics/aggs
# generalization):
#   - Aggregation bucket-count capping.  A Native body may contain an
#     arbitrarily large "aggs" subtree.  In Log-only mode the _search
#     response's aggregation results are DISCARDED by the parser
#     (_parse_es_response only walks hits.hits), so a Native agg body is
#     pure server-side compute cost — it produces zero output records.
#     The risk is bounded by the 64 KB body cap and the 30-second timeout,
#     but there is no explicit bucket-count ceiling.
#   - Size-field injection in Native bodies: if the caller includes "size"
#     in a Native body it is overwritten by ESLogSource._build_body, capping
#     at 1 000.  This is enforced, but it relies on dict key overwrite rather
#     than an explicit pre-request allowlist validation of Native keys.
#
# Tracking: deferred per phase-4 plan §4.7 / Task 5 carry-in note.
#           Revisit when ES Event/Metric selectors ship (phase 4b).
# ─────────────────────────────────────────────────────────────────────────────

_ENTITY_FIELD_NOTE = (
    "entity_field is assumed keyword-mapped in the ES index template; "
    "use entity_query='match' if the field is analyzed (full-text) — "
    "match over-matches tokenized content (plan F5)"
)


class ESLogSource:
    """LogSource backed by an Elasticsearch HTTP API.

    Parameters
    ----------
    url:
        Base URL of the Elasticsearch instance (e.g.
        ``http://es.example.com:9200``).  Must be non-empty.
    options:
        Dict of options read from source config:

        ``index_pattern`` (str, **required**):
            The index or pattern to search, e.g. ``k8s-logs-*``.  Used
            directly in the URL: ``/{index_pattern}/_search``.

        ``entity_field`` (str, default ``"kubernetes.pod_name"``):
            The ES field name for Entity selectors.  Assumed to be
            keyword-mapped (not analyzed) — see plan F5.

        ``timestamp_field`` (str, default ``"@timestamp"``):
            The ES field holding the record timestamp; used for range
            filtering and ``LogRecord.timestamp``.

        ``message_field`` (str, default ``"message"``):
            The ES field holding the log body; used for ``LogRecord.body``
            and excluded from ``LogRecord.attributes``.

        ``level_field`` (str, default ``"level"``):
            The ES field holding the severity level; used for
            ``LogRecord.severity``.  Missing field → ``severity=None``.

        ``entity_query`` (str, default ``"term"``):
            Clause type for Entity selectors: ``"term"`` (exact, keyword)
            or ``"match"`` (analyzed, full-text escape hatch).

        ``timeout_s`` (float, default ``30.0``):
            HTTP request timeout in seconds.

        ``session_factory`` (callable | None):
            Injectable test seam (see adapters.http.http_json docstring).
            In production this is left as None and aiohttp.ClientSession
            is used.

        ``bearer_env``, ``basic_user_env``, ``basic_pass_env``:
            Env-var reference names for auth (see adapters.http.auth_header).
    """

    def __init__(self, url: str, options: Dict[str, Any]) -> None:
        if not url:
            raise AdapterError(
                "elasticsearch source requires a non-empty url option"
            )
        index_pattern = options.get("index_pattern")
        if not index_pattern:
            raise AdapterError(
                "elasticsearch source requires the index_pattern option "
                "(e.g. 'k8s-logs-*')"
            )
        self._search_url = url.rstrip("/") + f"/{index_pattern}/_search"
        self._entity_field: str = options.get("entity_field", "kubernetes.pod_name")
        self._timestamp_field: str = options.get("timestamp_field", "@timestamp")
        self._message_field: str = options.get("message_field", "message")
        self._level_field: str = options.get("level_field", "level")
        self._entity_query: str = options.get("entity_query", "term")
        self._timeout_s: float = float(options.get("timeout_s", 30.0))
        self._session_factory = options.get("session_factory")  # None → real aiohttp
        self._auth_options: Dict[str, Any] = {
            k: options[k]
            for k in ("bearer_env", "basic_user_env", "basic_pass_env")
            if k in options
        }

    def _headers(self) -> Dict[str, str]:
        """Build request headers (auth if configured)."""
        return auth_header(self._auth_options)

    async def fetch_logs(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        limit: Optional[Limit],
    ) -> LogBatch:
        """Fetch log records from Elasticsearch for the given selector and window.

        Parameters
        ----------
        selector:
            Entity, Matchers, or Native (ES body dict passthrough).
        window:
            Optional time bounds.  Presence of start and/or end adds a
            ``range`` clause on ``timestamp_field`` to ``bool.filter``.
        limit:
            Optional result cap.  ``limit.max_records`` is used as ES
            ``size``; default is 500; hard cap is 1 000.

        Returns
        -------
        LogBatch
            Parsed log records with ``_source`` attributes and an ``"index"``
            grouping key.

        Raises
        ------
        AdapterError
            On pre-request rejection (script key, oversized body, deep nesting).
        HttpAdapterError
            On HTTP status >= 400 or connection failure.
        """
        size = min(
            (limit.max_records if limit is not None and limit.max_records is not None
             else 500),
            1000,
        )

        body = self._build_body(selector, window, size)

        # PRE-REQUEST security checks (order matters: size first, then scripts).
        enforce_body_limits(body)
        _reject_scripts(body)

        raw = await http_json(
            "POST",
            self._search_url,
            headers=self._headers(),
            json_body=body,
            timeout_s=self._timeout_s,
            session_factory=self._session_factory,
        )

        records = _parse_es_response(
            raw,
            timestamp_field=self._timestamp_field,
            message_field=self._message_field,
            level_field=self._level_field,
        )

        return LogBatch(
            records=records,
            provenance=Provenance(
                adapter="elasticsearch",
                query={"url": self._search_url, "size": size},
                grouping_attr="index",
            ),
        )

    def _build_body(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        size: int,
    ) -> Dict[str, Any]:
        """Build the Elasticsearch request body for the given selector.

        For Entity and Matchers: a ``bool.filter`` body is constructed.
        For Native: the JSON string is parsed to a dict and used as-is;
          window range and size are merged in.
        """
        if isinstance(selector, Native):
            body = json.loads(selector.query)
        elif isinstance(selector, Entity):
            entity_clause = {self._entity_query: {self._entity_field: selector.name_or_pattern}}
            body = {
                "query": {
                    "bool": {
                        "filter": [entity_clause],
                    }
                }
            }
        elif isinstance(selector, Matchers):
            # Build sorted term clauses for determinism.
            filter_clauses: List[Dict[str, Any]] = [
                {"term": {field: value}}
                for field, value in sorted(selector.terms.items())
            ]
            body = {
                "query": {
                    "bool": {
                        "filter": filter_clauses,
                    }
                }
            }
        else:
            raise AdapterError(
                f"ESLogSource: unsupported selector type {type(selector).__name__!r}"
            )

        # Always set/cap size.
        body["size"] = size

        # Merge window range into bool.filter when bounds are present.
        if window is not None and (window.start is not None or window.end is not None):
            range_inner: Dict[str, str] = {}
            if window.start is not None:
                range_inner["gte"] = window.start.isoformat()
            if window.end is not None:
                range_inner["lte"] = window.end.isoformat()
            range_clause = {"range": {self._timestamp_field: range_inner}}

            # Ensure query.bool.filter exists.
            query = body.setdefault("query", {})
            bool_clause = query.setdefault("bool", {})
            filter_list = bool_clause.setdefault("filter", [])
            if isinstance(filter_list, list):
                filter_list.append(range_clause)
            else:
                # filter was a dict (must clause) — wrap it
                bool_clause["filter"] = [filter_list, range_clause]

        return body


def _parse_es_response(
    raw: dict,
    *,
    timestamp_field: str,
    message_field: str,
    level_field: str,
) -> List[LogRecord]:
    """Parse an Elasticsearch _search JSON response into a LogRecord list.

    Expected shape::

        {
          "hits": {
            "hits": [
              {
                "_index": "k8s-logs-2026.01.01",
                "_source": {
                  "@timestamp": "2026-01-01T00:00:00+00:00",
                  "message": "log line text",
                  "level": "INFO",
                  ... (arbitrary fields)
                }
              },
              ...
            ]
          }
        }

    Returns
    -------
    list[LogRecord]
        One record per hit.  ``attributes`` contains all ``_source`` fields
        EXCEPT the message field, PLUS ``"index"`` set to ``hit._index``.
    """
    records: List[LogRecord] = []

    try:
        hits = raw["hits"]["hits"]
    except (KeyError, TypeError):
        return records

    for hit in hits:
        source: Dict[str, Any] = hit.get("_source", {})
        index: str = hit.get("_index", "")

        timestamp = source.get(timestamp_field)
        body = source.get(message_field, "")
        severity = source.get(level_field)  # None if absent

        # Attributes: all _source fields minus the message field, plus "index".
        attributes = {k: v for k, v in source.items() if k != message_field}
        attributes["index"] = index

        records.append(LogRecord(
            timestamp=timestamp,
            body=str(body),
            severity=severity,
            attributes=attributes,
        ))

    return records
