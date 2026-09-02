"""Shared option validator for the OTLP adapter (spec §4.2.1, phase 5).

``validate_otlp_options`` is the single source of truth for OTLP source
configuration.  It is called from both the server-mcp factory (T2) and
from main.py before the receiver thread is started (T4).

Validation rules
  ``ring_capacity``   MANDATORY int > 0; no default.
  ``max_body_bytes``  MANDATORY int > 0; no default.
  ``max_record_bytes``OPTIONAL  int > 0; default 65 536 (64 KiB).
  ``signals``         OPTIONAL  list; only ``["logs"]`` is accepted in v1
                      (§4.2.1 item 5b); default ``["logs"]``.

All validation failures raise :exc:`core.errors.AdapterError` and include
the offending key name in the message so the caller can surface it directly.
"""
from __future__ import annotations

from collections.abc import Mapping

from core.errors import AdapterError

# Keys that are mandatory and must be positive integers with no default.
_MANDATORY_POS_INT: tuple[str, ...] = ("ring_capacity", "max_body_bytes")

_DEFAULT_MAX_RECORD_BYTES: int = 65_536


def _require_pos_int(key: str, source_name: str, value: object) -> int:
    """Validate that *value* is a positive int (not bool) and return it.

    Raises :exc:`AdapterError` naming *source_name* and *key* on failure.

    Note: parameter renamed from ``name`` to ``key`` (Task-2 carry nit).
    """
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterError(
            f"{source_name}: '{key}' must be a positive integer, "
            f"got {type(value).__name__!r}"
        )
    if value <= 0:
        raise AdapterError(
            f"{source_name}: '{key}' must be > 0, got {value!r}"
        )
    return value


def validate_otlp_options(name: str, options: Mapping) -> dict:
    """Validate *options* for an OTLP source named *name*.

    Parameters
    ----------
    name:
        The source name (used in error messages).
    options:
        Raw options mapping from the YAML config or programmatic caller.

    Returns
    -------
    dict
        A clean, validated copy of the options with defaults applied.

    Raises
    ------
    AdapterError
        On any validation failure.  The message always names the offending key.
    """
    out: dict = {}

    # Mandatory positive-int keys.
    for key in _MANDATORY_POS_INT:
        if key not in options:
            raise AdapterError(
                f"{name}: otlp source requires '{key}' (missing)"
            )
        out[key] = _require_pos_int(key, name, options[key])

    # Optional max_record_bytes with default.
    mrb_raw = options.get("max_record_bytes", _DEFAULT_MAX_RECORD_BYTES)
    out["max_record_bytes"] = _require_pos_int("max_record_bytes", name, mrb_raw)

    # signals — only ["logs"] accepted in v1 (§4.2.1 item 5b).
    # list() copy prevents aliasing the default mutable list (Task-2 carry nit).
    signals = list(options.get("signals", ["logs"]))
    if signals != ["logs"]:
        raise AdapterError(
            f"{name}: otlp adapter only supports signals=['logs'] in v1 "
            f"(§4.2.1 item 5b — metrics/traces/spans deferred); "
            f"got {signals!r}"
        )
    out["signals"] = signals

    return out
