"""Node and cluster resource forecasting helpers."""
import asyncio
import functools
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.readonly_client import ReadOnlyK8sClient
from helpers.utils import (
    _get_active_node_names,
    _is_node_active,
    _NODE_LISTING_EXECUTOR,
    list_nodes_bounded,
    parse_time_period,
)

logger = logging.getLogger("lumino-mcp")


async def get_active_node_names_bounded(core_api,
                                        request_timeout: float = 30.0):
    """Caller-bounded async dispatch of _get_active_node_names.

    Same rationale as list_nodes_bounded (re-review MAJOR-3): _request_timeout
    alone does not bound the caller because urllib3 retries read timeouts.
    A timeout degrades to None — the caller's existing "filter disabled"
    path — instead of holding the forecaster for minutes.

    Resolves _get_active_node_names through THIS module's globals at call
    time, preserving the established test monkeypatch surface
    (test_readonly_forecaster_tracer patches it on this module).

    Dispatched to _NODE_LISTING_EXECUTOR, not the shared default executor
    (bug 7): the same isolation rationale as list_nodes_bounded — an
    abandoned worker against a wedged apiserver must not exhaust the pool
    every other tool call site draws from.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(_get_active_node_names, core_api)
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_NODE_LISTING_EXECUTOR, call),
            timeout=request_timeout * 1.5 + 1)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("Active-node lookup exceeded %.0fs; degrading to "
                       "unfiltered node set", request_timeout * 1.5 + 1)
        return None


# Usage at or above this percent counts as exhausted.
EXHAUSTION_PERCENT = 90.0
_FIVE_MINUTES = 300.0

# (resource_type, PromQL, result limit, contributing factors)
_NODE_QUERIES = (
    ('cpu',
     # aggregate to avoid series explosion from pod restarts
     'max by (instance) (100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
     100,
     ['workload_scaling', 'baseline_usage_trend']),
    ('memory',
     'max by (instance) ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)',
     100,
     ['memory_leaks', 'workload_growth', 'cache_usage']),
    ('disk',
     # filter out kubelet pod volumes and aggregate by instance/mountpoint
     '''max by (instance, mountpoint) (
            (1 - (node_filesystem_avail_bytes{fstype!="tmpfs", mountpoint!~"/var/lib/kubelet/pods.*|/run/.*"}
                / node_filesystem_size_bytes{fstype!="tmpfs", mountpoint!~"/var/lib/kubelet/pods.*|/run/.*"})) * 100
        )''',
     200,
     ['log_growth', 'cache_accumulation', 'temporary_files']),
)


def _series_points(metric: Dict[str, Any]) -> List[Tuple[float, float]]:
    """(unix seconds, value) pairs of a range series, skipping NaN/unparsable."""
    points = []
    for point in metric.get('values', []):
        try:
            ts, value = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(ts) and math.isfinite(value):
            points.append((ts, value))
    return points


def _usage_trend(metric: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Current usage, growth per 5 minutes and predicted exhaustion of a series.

    prometheus_query returns at most 50 downsampled points per series (the
    first and newest always kept), so the trend is fitted on the sample
    timestamps, never on the sample index. The current value is the newest
    finite sample.
    """
    points = _series_points(metric)
    if not points:
        return None
    current = points[-1][1]

    slope_per_second = None
    if len(points) >= 3 and len({ts for ts, _ in points}) >= 2:
        from scipy.stats import linregress
        slope = linregress([ts for ts, _ in points], [v for _, v in points]).slope
        if math.isfinite(slope):
            slope_per_second = float(slope)

    predicted_exhaustion = None
    if slope_per_second and slope_per_second > 0 and current < EXHAUSTION_PERCENT:
        seconds = (EXHAUSTION_PERCENT - current) / slope_per_second
        predicted_exhaustion = (now + timedelta(seconds=seconds)).isoformat()

    return {
        'current': current,
        'growth_per_5min': None if slope_per_second is None else slope_per_second * _FIVE_MINUTES,
        'predicted_exhaustion': predicted_exhaustion,
    }


async def _analyze_node_resources_new(trend_period: str, forecast_horizon: str, log, *, query_fn, core_api) -> List[Dict]:
    """Analyze node-level resource utilization using Prometheus query method."""
    try:
        # Get currently active nodes to filter out historical/terminated nodes
        # (off-loop AND caller-bounded — see get_active_node_names_bounded)
        active_nodes = await get_active_node_names_bounded(core_api)
        if active_nodes is None:
            log.warning("Active-node lookup failed (degraded apiserver?) — "
                        "node filter disabled; forecasts may include "
                        "terminated/historical nodes")
            active_nodes = set()
        else:
            log.info(f"Found {len(active_nodes)} active nodes from Kubernetes API")

        # Time range for trend analysis, in UTC (Prometheus reads "+00:00")
        end_time = datetime.now(timezone.utc)
        start_time = end_time - parse_time_period(trend_period)

        forecasts = []
        filtered_count = 0

        for resource_type, query, limit, factors in _NODE_QUERIES:
            try:
                result = await query_fn(
                    query=query,
                    query_type="range",
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                    step="300s",
                    limit=limit,
                )
                if result.get("status") != "success" or not result.get("data"):
                    continue

                for metric in result["data"]:
                    labels = metric.get('metric', {})
                    node = labels.get('instance', 'unknown')

                    # Filter out nodes that are no longer active
                    if not _is_node_active(node, active_nodes):
                        filtered_count += 1
                        continue

                    trend = _usage_trend(metric, end_time)
                    if trend is None:
                        continue

                    identifier = {'node': node}
                    if resource_type == 'disk':
                        identifier['mountpoint'] = labels.get('mountpoint', 'unknown')
                    identifier['metric'] = f'{resource_type}_utilization_percent'

                    forecasts.append({
                        'resource_type': resource_type,
                        'resource_identifier': identifier,
                        'current_usage': {'value': trend['current'], 'unit': 'percent'},
                        'predicted_exhaustion': trend['predicted_exhaustion'],
                        'growth_rate': {'value': trend['growth_per_5min'], 'unit': 'percent_per_5min'},
                        'contributing_factors': list(factors),
                    })
            except Exception as e:
                log.warning(f"Error fetching {resource_type} metrics: {str(e)}")

        if filtered_count > 0:
            log.info(f"Filtered out {filtered_count} metrics from inactive/historical nodes")

        return forecasts

    except Exception as e:
        log.error(f"Error analyzing node resources: {str(e)}")
        return []


async def _analyze_cluster_capacity_new(core_api, log, *, query_fn) -> Dict[str, Any]:
    """Analyze overall cluster capacity and health using Prometheus query method."""
    try:
        core_api = ReadOnlyK8sClient.wrap(core_api)
        # Get current cluster resource allocation from Kubernetes API
        nodes = await list_nodes_bounded(core_api)

        total_cpu = 0
        total_memory = 0
        total_nodes = len(nodes.items)

        for node in nodes.items:
            if node.status and node.status.capacity:
                cpu_str = node.status.capacity.get('cpu', '0')
                memory_str = node.status.capacity.get('memory', '0Ki')

                # Parse CPU (cores)
                if 'm' in cpu_str:
                    total_cpu += int(cpu_str.replace('m', '')) / 1000
                else:
                    total_cpu += int(cpu_str)

                # Parse Memory (bytes)
                if memory_str.endswith('Ki'):
                    total_memory += int(memory_str[:-2]) * 1024
                elif memory_str.endswith('Mi'):
                    total_memory += int(memory_str[:-2]) * 1024 * 1024
                elif memory_str.endswith('Gi'):
                    total_memory += int(memory_str[:-2]) * 1024 * 1024 * 1024

        # Current cluster usage via Prometheus; None when it cannot be read
        # (never 0 %, which would read as an idle, healthy cluster)
        async def _cluster_percent(name: str, query: str) -> Optional[float]:
            try:
                result = await query_fn(query)
            except Exception as e:
                log.warning(f"Could not fetch cluster {name} usage: {str(e)}")
                return None
            data = result.get("data") if result.get("status") == "success" else None
            if not data or 'value' not in data[0]:
                log.warning(f"Could not fetch cluster {name} usage: "
                            f"{result.get('error') or 'no data returned'}")
                return None
            try:
                value = float(data[0]['value'])
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        cpu_usage_percent = await _cluster_percent(
            "CPU", 'avg(100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))')
        memory_usage_percent = await _cluster_percent(
            "memory", 'avg(100 - (avg by (instance) (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)')
        known = [u for u in (cpu_usage_percent, memory_usage_percent) if u is not None]

        # Determine overall health: the worst known reading decides; with a
        # reading missing, only a critical/degraded result is still certain
        if any(u > 90 for u in known):
            overall_health = "critical"
        elif any(u > 80 for u in known):
            overall_health = "degraded"
        elif len(known) < 2:
            overall_health = "unknown"
        else:
            overall_health = "healthy"

        # Identify most constrained resources
        constrained_resources = []
        if cpu_usage_percent is not None and cpu_usage_percent > 70:
            constrained_resources.append(f"CPU ({cpu_usage_percent:.1f}%)")
        if memory_usage_percent is not None and memory_usage_percent > 70:
            constrained_resources.append(f"Memory ({memory_usage_percent:.1f}%)")

        def _percent(value: Optional[float]) -> Optional[str]:
            return None if value is None else f"{value:.1f}%"

        return {
            "overall_health": overall_health,
            "total_nodes": total_nodes,
            "total_cpu_cores": total_cpu,
            "total_memory_gb": round(total_memory / (1024**3), 1),
            "current_cpu_usage": _percent(cpu_usage_percent),
            "current_memory_usage": _percent(memory_usage_percent),
            "data_source": {
                "cpu": "unavailable" if cpu_usage_percent is None else "prometheus",
                "memory": "unavailable" if memory_usage_percent is None else "prometheus",
            },
            "most_constrained_resources": constrained_resources,
            "fastest_growing_consumers": [],  # Would need historical analysis
            # A runway needs a usage trend; one instant reading has none
            "capacity_runway": {
                "cpu_runway_days": None,
                "memory_runway_days": None,
                "note": "Cluster runway is not computed from a single reading; "
                        "see predicted_exhaustion in the per-node forecasts",
            },
        }

    except Exception as e:
        log.error(f"Error analyzing cluster capacity: {str(e)}")
        return {
            "overall_health": "unknown",
            "total_nodes": 0,
            "total_cpu_cores": 0,
            "total_memory_gb": 0,
            "current_cpu_usage": "unknown",
            "current_memory_usage": "unknown",
            "most_constrained_resources": [],
            "fastest_growing_consumers": [],
            "capacity_runway": {}
        }
