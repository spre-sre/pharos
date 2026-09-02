"""
Pure log anomaly-detection engine — no Kubernetes, no MCP, no network I/O.

Public interface
----------------
detect(logs: str, baseline_patterns=None, severity_threshold="medium") -> dict
    Detect anomalies in log data using error frequency, pattern repetition, and
    timestamp analysis.  The body is verbatim from the ``detect_log_anomalies``
    tool handler it was extracted from.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("lumino-mcp")


def detect(
    logs: str,
    baseline_patterns: Optional[List[str]] = None,
    severity_threshold: str = "medium",
) -> Dict[str, Any]:
    """
    Detect anomalies in log data using error frequency, pattern repetition, and timestamp analysis.

    Args:
        logs: Raw log content (newline-separated entries).
        baseline_patterns: Optional expected error patterns for comparison.
        severity_threshold: "low" (most sensitive), "medium", or "high" (least sensitive).

    Returns:
        Dict[str, Any]: Keys: anomaly_detected (bool), anomaly_details, analysis_summary.
    """
    logger.info(f"Starting log anomaly detection with severity threshold: {severity_threshold}")

    if not logs or logs.strip() == "":
        logger.warning("Empty or null logs provided for anomaly detection")
        return {
            "anomaly_detected": False,
            "anomaly_details": None,
            "analysis_summary": "No logs provided for analysis"
        }

    try:
        # Normalize escaped newlines from MCP/JSON transport (literal \n -> actual newline)
        import re as _re
        normalized_logs = _re.sub(
            r'\\n(?=\d{4}-\d{2}-\d{2}|level=|"level"|time=|"ts"|msg=|http:)',
            '\n', logs
        )
        if '\n' not in normalized_logs and '\\n' in normalized_logs:
            normalized_logs = normalized_logs.replace('\\n', '\n')

        # Parse logs into lines
        log_lines = [line.strip() for line in normalized_logs.split('\n') if line.strip()]
        total_lines = len(log_lines)

        if total_lines == 0:
            return {
                "anomaly_detected": False,
                "anomaly_details": None,
                "analysis_summary": "No valid log lines found"
            }

        logger.info(f"Analyzing {total_lines} log lines for anomalies")

        # Initialize anomaly detection results
        anomalies = []

        # Define severity thresholds
        thresholds = {
            "low": {"error_rate": 0.05, "warn_rate": 0.30, "repetition_rate": 0.3, "time_gap": 300},
            "medium": {"error_rate": 0.1, "warn_rate": 0.60, "repetition_rate": 0.5, "time_gap": 180},
            "high": {"error_rate": 0.2, "warn_rate": 0.90, "repetition_rate": 0.7, "time_gap": 60}
        }

        threshold_config = thresholds.get(severity_threshold, thresholds["medium"])

        # 1. Analyze error frequency patterns
        # First-match-wins: more specific patterns before generic ones
        error_pattern_map = {
            r"(?i)\b(out\s+of\s+memory|memory\s+limit|oom(?:killed)?)\b": "memory",
            r"(?i)\b(timeout)\b": "timeout",
            r"(?i)\b(connection\s+refused|connection\s+reset)\b": "connection",
            r"(?i)\b(permission\s+denied|access\s+denied|unauthorized)\b": "permission",
            r"(?i)\b(not\s+found|missing|invalid|corrupt)\b": "not_found",
            r"(?i)\b(error|exception|failed|fatal|panic|critical)\b": "error",
        }

        error_counts = {}
        error_lines = []
        warn_lines = 0

        for i, line in enumerate(log_lines):
            matched = False
            for pattern, label in error_pattern_map.items():
                if re.search(pattern, line):
                    error_lines.append((i, line))
                    error_counts[label] = error_counts.get(label, 0) + 1
                    matched = True
                    break
            if not matched and re.search(r"(?i)\bwarn(?:ing)?\b", line):
                warn_lines += 1

        unique_error_line_indices = set(line[0] for line in error_lines)
        error_rate = len(unique_error_line_indices) / total_lines
        if error_rate > threshold_config["error_rate"]:
            anomalies.append({
                "type": "high_error_rate",
                "severity": "high" if error_rate > 0.3 else "medium",
                "description": f"High error rate detected: {error_rate:.2%} ({len(unique_error_line_indices)}/{total_lines} lines)",
                "details": {
                    "error_rate": error_rate,
                    "error_patterns": error_counts,
                    "sample_errors": [line[1][:200] for line in error_lines[:5]]  # Truncate long lines
                }
            })

        warn_rate = warn_lines / total_lines if total_lines else 0
        if warn_rate > threshold_config["warn_rate"]:
            anomalies.append({
                "type": "high_warn_rate",
                "severity": "medium" if warn_rate > 0.5 else "low",
                "description": f"High warning rate detected: {warn_rate:.2%} ({warn_lines}/{total_lines} lines)",
                "details": {"warn_rate": warn_rate, "warn_count": warn_lines}
            })

        # 2. Detect repetitive log patterns (potential loops or spam)
        line_frequency = {}
        for line in log_lines:
            # Normalize line by removing timestamps and variable data
            normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', 'TIMESTAMP', line)
            normalized = re.sub(r'\b\d+\b', 'NUMBER', normalized)
            normalized = re.sub(r'\b[a-f0-9]{8,}\b', 'HASH', normalized)

            line_frequency[normalized] = line_frequency.get(normalized, 0) + 1

        # Find highly repetitive patterns
        for pattern, count in line_frequency.items():
            repetition_rate = count / total_lines
            if repetition_rate > threshold_config["repetition_rate"] and count > 10:
                anomalies.append({
                    "type": "repetitive_pattern",
                    "severity": "high" if repetition_rate > 0.8 else "medium",
                    "description": f"Highly repetitive log pattern detected: {repetition_rate:.2%} of logs ({count} occurrences)",
                    "details": {
                        "pattern": pattern[:200],
                        "occurrence_count": count,
                        "repetition_rate": repetition_rate
                    }
                })

        # 3. Analyze timestamp patterns for gaps or bursts
        timestamps = []
        for line in log_lines:
            # Extract timestamps
            timestamp_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
            if timestamp_match:
                try:
                    ts = datetime.fromisoformat(timestamp_match.group(1).replace('T', ' '))
                    timestamps.append(ts)
                except Exception:
                    continue

        if len(timestamps) > 2:
            # Calculate time gaps between consecutive log entries
            time_gaps = []
            for i in range(1, len(timestamps)):
                gap = (timestamps[i] - timestamps[i-1]).total_seconds()
                time_gaps.append(gap)

            # Detect unusual time gaps
            if time_gaps:
                avg_gap = sum(time_gaps) / len(time_gaps)
                max_gap = max(time_gaps)

                if max_gap > threshold_config["time_gap"] and max_gap > avg_gap * 10:
                    anomalies.append({
                        "type": "time_gap_anomaly",
                        "severity": "medium",
                        "description": f"Unusual time gap detected: {max_gap:.0f} seconds (avg: {avg_gap:.1f}s)",
                        "details": {
                            "max_gap_seconds": max_gap,
                            "average_gap_seconds": avg_gap,
                            "total_timestamps": len(timestamps)
                        }
                    })

                # Detect log bursts (many logs in short time)
                burst_threshold = 50  # logs per minute
                one_minute_windows = {}
                for ts in timestamps:
                    minute_key = ts.replace(second=0, microsecond=0)
                    one_minute_windows[minute_key] = one_minute_windows.get(minute_key, 0) + 1

                max_burst = max(one_minute_windows.values()) if one_minute_windows else 0
                if max_burst > burst_threshold:
                    anomalies.append({
                        "type": "log_burst",
                        "severity": "medium",
                        "description": f"Log burst detected: {max_burst} logs in one minute",
                        "details": {
                            "max_logs_per_minute": max_burst,
                            "burst_threshold": burst_threshold
                        }
                    })

        # 4. Check against baseline patterns if provided
        if baseline_patterns:
            baseline_set = set(baseline_patterns)
            current_patterns = set(error_counts.keys())

            new_patterns = current_patterns - baseline_set
            missing_patterns = baseline_set - current_patterns

            if new_patterns:
                anomalies.append({
                    "type": "new_error_patterns",
                    "severity": "medium",
                    "description": f"New error patterns not seen in baseline: {', '.join(list(new_patterns)[:5])}",
                    "details": {
                        "new_patterns": list(new_patterns),
                        "baseline_patterns": baseline_patterns
                    }
                })

            if missing_patterns and len(missing_patterns) > len(baseline_patterns) * 0.5:
                anomalies.append({
                    "type": "missing_expected_patterns",
                    "severity": "low",
                    "description": f"Expected patterns missing from logs: {', '.join(list(missing_patterns)[:5])}",
                    "details": {
                        "missing_patterns": list(missing_patterns)
                    }
                })

        # 5. Detect unusual log levels distribution
        log_levels = {"debug": 0, "info": 0, "warn": 0, "error": 0, "fatal": 0}
        for line in log_lines:
            line_lower = line.lower()
            if re.search(r'\b(debug|trace)\b', line_lower):
                log_levels["debug"] += 1
            elif re.search(r'\binfo\b', line_lower):
                log_levels["info"] += 1
            elif re.search(r'\b(warn|warning)\b', line_lower):
                log_levels["warn"] += 1
            elif re.search(r'\b(error|err)\b', line_lower):
                log_levels["error"] += 1
            elif re.search(r'\b(fatal|critical|panic)\b', line_lower):
                log_levels["fatal"] += 1

        total_leveled = sum(log_levels.values())
        if total_leveled > 0:
            error_plus_fatal = log_levels["error"] + log_levels["fatal"]
            severe_ratio = error_plus_fatal / total_leveled

            if severe_ratio > 0.5:  # More than 50% severe logs
                anomalies.append({
                    "type": "unusual_log_level_distribution",
                    "severity": "high",
                    "description": f"High proportion of severe logs: {severe_ratio:.2%}",
                    "details": {
                        "log_level_distribution": log_levels,
                        "severe_log_ratio": severe_ratio
                    }
                })

        # Compile results
        anomaly_detected = len(anomalies) > 0

        if anomaly_detected:
            # Sort anomalies by severity
            severity_order = {"high": 3, "medium": 2, "low": 1}
            anomalies.sort(key=lambda x: severity_order.get(x["severity"], 0), reverse=True)

            anomaly_details = {
                "total_anomalies": len(anomalies),
                "anomalies": anomalies,
                "log_statistics": {
                    "total_lines": total_lines,
                    "error_rate": error_rate,
                    "unique_patterns": len(line_frequency),
                    "timestamp_coverage": len(timestamps) / total_lines if total_lines > 0 else 0
                }
            }

            analysis_summary = f"Detected {len(anomalies)} anomalies in {total_lines} log lines. "
            analysis_summary += f"Highest severity: {anomalies[0]['severity']}. "
            analysis_summary += f"Primary issues: {', '.join([a['type'] for a in anomalies[:3]])}"
        else:
            anomaly_details = None
            analysis_summary = f"No anomalies detected in {total_lines} log lines. Log patterns appear normal."

        logger.info(f"Anomaly detection completed. Found {len(anomalies)} anomalies")

        return {
            "anomaly_detected": anomaly_detected,
            "anomaly_details": anomaly_details,
            "analysis_summary": analysis_summary
        }

    except Exception as e:
        logger.error(f"Error during log anomaly detection: {str(e)}", exc_info=True)
        return {
            "anomaly_detected": False,
            "anomaly_details": None,
            "analysis_summary": f"Analysis failed due to error: {str(e)}"
        }
