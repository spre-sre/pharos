"""Format detection and line parsing for the file adapter.

Produces LogRecord field kwargs consumed by FileLogSource.  All output must
honour the RELPATH INVARIANT: no absolute path may appear in any field,
in particular ``attributes["file"]`` must always be the caller-supplied
relative path.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# ─── tunables ─────────────────────────────────────────────────────────────────

_SAMPLE_LINES: int = 20
_FORMAT_THRESHOLD: float = 0.6

# ─── klog ────────────────────────────────────────────────────────────────────

# klog header: {I|W|E|F}{MMDD} {HH:MM:SS.microseconds}
_KLOG_RE = re.compile(r"^[IWEF]\d{4} \d\d:\d\d:\d\d\.\d+")

_KLOG_SEVERITY: Dict[str, str] = {
    "I": "INFO",
    "W": "WARNING",
    "E": "ERROR",
    "F": "CRITICAL",
}

# ─── JSON field aliases ───────────────────────────────────────────────────────

# Ordered by priority (first match wins).
_TS_KEYS = ("ts", "time", "timestamp", "@timestamp")
_SEVERITY_KEYS = ("level", "severity", "lvl")
_BODY_KEYS = ("message", "msg", "body")


# ─── public API ───────────────────────────────────────────────────────────────


def detect_format(sample_lines: List[str]) -> str:
    """Classify *sample_lines* as ``"plain"``, ``"jsonlines"``, or ``"klog"``.

    Only the first :data:`_SAMPLE_LINES` non-blank lines are examined.
    A format is chosen when at least :data:`_FORMAT_THRESHOLD` (60 %) of the
    sampled lines match its signature; otherwise ``"plain"`` is returned.
    klog is tested before JSON so that klog files with embedded JSON messages
    are not misclassified.
    """
    candidates = [ln for ln in sample_lines[:_SAMPLE_LINES] if ln.strip()]
    if not candidates:
        return "plain"
    total = len(candidates)

    json_count = 0
    klog_count = 0
    for line in candidates:
        if _KLOG_RE.match(line):
            klog_count += 1
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                json_count += 1
        except (json.JSONDecodeError, ValueError):
            pass

    if klog_count / total >= _FORMAT_THRESHOLD:
        return "klog"
    if json_count / total >= _FORMAT_THRESHOLD:
        return "jsonlines"
    return "plain"


def parse_line(line: str, fmt: str, relpath: str) -> Dict[str, Any]:
    """Parse a single log line into LogRecord field kwargs.

    The returned dict always has keys: ``timestamp``, ``body``, ``severity``,
    ``attributes``.  ``attributes["file"]`` is always *relpath* — never an
    absolute path (RELPATH INVARIANT).

    For ``"jsonlines"``: a per-line JSON parse failure falls back silently to
    plain treatment rather than propagating an exception.
    """
    if fmt == "jsonlines":
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            return _parse_jsonlines(data, original_line=line, relpath=relpath)
        except (json.JSONDecodeError, ValueError):
            return _parse_plain(line, relpath)
    if fmt == "klog":
        return _parse_klog(line, relpath)
    return _parse_plain(line, relpath)


# ─── format-specific helpers ─────────────────────────────────────────────────


def _parse_plain(line: str, relpath: str) -> Dict[str, Any]:
    return {
        "timestamp": None,
        "body": line,
        "severity": None,
        "attributes": {"file": relpath},
    }


def _parse_jsonlines(
    data: Dict[str, Any], original_line: str, relpath: str
) -> Dict[str, Any]:
    """Extract structured fields from a parsed JSON object.

    Priority-ordered key aliases are tried for timestamp, severity, and body.
    Remaining (leftover) keys land in ``attributes`` alongside the mandatory
    ``file`` key.  Known-mapping keys are consumed and not duplicated.
    """
    consumed: set = set()

    # Timestamp: ts > time > timestamp > @timestamp
    timestamp: Optional[str] = None
    for key in _TS_KEYS:
        if key in data:
            timestamp = str(data[key])
            consumed.add(key)
            break

    # Severity: level > severity > lvl
    severity: Optional[str] = None
    for key in _SEVERITY_KEYS:
        if key in data:
            severity = str(data[key])
            consumed.add(key)
            break

    # Body: message > msg > body > whole original line
    body: str = original_line
    for key in _BODY_KEYS:
        if key in data:
            body = str(data[key])
            consumed.add(key)
            break

    # Leftover keys → attributes (relpath invariant: file is always relative)
    attributes: Dict[str, Any] = {k: v for k, v in data.items() if k not in consumed}
    attributes["file"] = relpath

    return {
        "timestamp": timestamp,
        "body": body,
        "severity": severity,
        "attributes": attributes,
    }


def _parse_klog(line: str, relpath: str) -> Dict[str, Any]:
    """Parse a klog-formatted line.

    klog format: ``{I|W|E|F}{MMDD} {HH:MM:SS.μs} {goroutine} {file:line}] {msg}``

    ``timestamp`` is the year-less header string verbatim (everything before
    the ``]`` separator).  ``body`` is the remainder after ``] ``.
    Falls back to plain if the line does not match the klog header regex.
    """
    if not _KLOG_RE.match(line):
        return _parse_plain(line, relpath)

    severity_letter = line[0]
    severity = _KLOG_SEVERITY.get(severity_letter, "INFO")

    bracket_idx = line.find("]")
    if bracket_idx != -1:
        header = line[:bracket_idx].strip()
        body = line[bracket_idx + 1 :].strip()
    else:
        header = line
        body = line

    return {
        "timestamp": header,
        "body": body,
        "severity": severity,
        "attributes": {"file": relpath},
    }
