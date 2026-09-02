"""Canonical adapter-layer error types (spec SS4.7)."""
from __future__ import annotations


class AdapterError(ValueError):
    """Base for adapter-layer failures surfaced to tools as {"error": str}."""
