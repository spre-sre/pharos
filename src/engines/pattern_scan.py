"""
Pure log pattern-scan engine — no Kubernetes, no MCP, no network I/O.

Public interface
----------------
scan(log_text: str) -> dict
    Analyse raw log text and return error counts, patterns, categories and a
    plain-language summary.  The body is identical to the ``analyze_logs``
    tool handler it was extracted from; helpers stay in helpers.utils.
"""

import logging
from typing import Any, Dict

from helpers.utils import (
    categorize_errors,
    extract_error_patterns,
    generate_log_summary,
)

logger = logging.getLogger("lumino-mcp")


def scan(log_text: str) -> Dict[str, Any]:
    """
    Analyse log text and extract error patterns and insights.

    Args:
        log_text: Log content string (single entry, multiple lines, or full log file).

    Returns:
        Dict[str, Any]: Keys: error_count, error_patterns, categorized_errors, summary.
    """
    try:
        error_patterns = extract_error_patterns(log_text)
        error_categories = categorize_errors(log_text, error_patterns)

        return {
            "error_count": len(error_patterns),
            "error_patterns": error_patterns,
            "categorized_errors": error_categories,
            "summary": generate_log_summary(log_text, error_patterns, error_categories)
        }
    except Exception as e:
        logger.error(f"Error in analyze_logs: {e}", exc_info=True)
        return {
            "error_count": 0,
            "error_patterns": [],
            "categorized_errors": {},
            "summary": f"Analysis failed: {str(e)}"
        }
