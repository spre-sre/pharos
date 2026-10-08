"""Node and cluster resource forecasting helpers."""
import asyncio
import functools
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from kubernetes.utils import parse_quantity

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
# Trends that reach EXHAUSTION_PERCENT later than this are never projected
# (the tool's forecast_horizon normally sets a shorter limit).
MAX_PROJECTION = timedelta(days=365)
# Newest samples averaged to decide "already exhausted" (one spike is not).
_RECENT_SAMPLES = 3
# A newest sample above the threshold is a spike when it is more than this
# many noise units (median absolute deviation from the trend) and at least
# _SPIKE_MIN_GAP percentage points above the trend value.
_SPIKE_NOISE_FACTOR = 3.0
_SPIKE_MIN_GAP = 5.0
# Newest samples fitted on their own when the whole-window trend has reached
# EXHAUSTION_PERCENT but the data have not: a ramp still rises there, a
# curve that levels off does not.
_LOCAL_SAMPLES = 10
# Newest samples flat and at most this many points under EXHAUSTION_PERCENT
# (and within their noise of it) count as at the threshold now.
_HOVER_MAX_GAP = 2.0

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


def _local_trend(points: List[Tuple[float, float]]) -> Optional[Tuple[float, float, float, float]]:
    """(slope per second, trend value at the newest sample, median absolute
    deviation from the trend, upper 95 % bound of the slope) of a few points."""
    if len(points) < 3 or len({ts for ts, _ in points}) < 2:
        return None
    from scipy.stats import theilslopes
    slope, intercept, _, high_slope = theilslopes([v for _, v in points], [ts for ts, _ in points])
    if not (math.isfinite(slope) and math.isfinite(intercept)):
        return None
    residuals = sorted(abs(v - (intercept + slope * ts)) for ts, v in points)
    return (float(slope), float(intercept + slope * points[-1][0]),
            float(residuals[len(residuals) // 2]), float(high_slope))


def _usage_trend(metric: Dict[str, Any], now: datetime,
                 horizon: timedelta = MAX_PROJECTION,
                 horizon_label: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Current usage, growth per 5 minutes and predicted exhaustion of a series.

    prometheus_query returns at most 50 downsampled points per series (the
    first and newest always kept), so the trend is fitted on the sample
    timestamps, never on the sample index, with the Theil-Sen estimator so
    that one outlier cannot create a trend. The current value is the newest
    finite sample (shown as current usage); the projection starts from the
    trend value at that sample (the level), so one sample cannot move it.
    A series is exhausted only when the data and the trend agree; a newest
    sample far above the level, beyond the series' noise, is a spike.
    """
    points = _series_points(metric)
    if not points:
        return None
    current = points[-1][1]

    slope_per_second = None
    level = None  # trend value at the newest sample
    noise = 0.0   # median absolute deviation of the samples from the trend
    if len(points) >= 3 and len({ts for ts, _ in points}) >= 2:
        from scipy.stats import theilslopes
        slope, intercept = theilslopes([v for _, v in points], [ts for ts, _ in points])[:2]
        if math.isfinite(slope) and math.isfinite(intercept):
            slope_per_second = float(slope)
            level = float(intercept + slope * points[-1][0])
            residuals = sorted(abs(v - (intercept + slope * ts)) for ts, v in points)
            noise = float(residuals[len(residuals) // 2])

    if horizon >= MAX_PROJECTION:
        horizon, horizon_label = MAX_PROJECTION, f'{MAX_PROJECTION.days}d'
    horizon_label = horizon_label or f'{horizon.total_seconds() / 3600:g}h'
    recent = [v for _, v in points[-_RECENT_SAMPLES:]]
    recent_mean = sum(recent) / len(recent)
    # A newest sample this far from the level is an outlier, not the trend
    outlier_gap = max(_SPIKE_NOISE_FACTOR * noise, _SPIKE_MIN_GAP)
    spike = current >= EXHAUSTION_PERCENT and (level is None or current - level > outlier_gap)
    dip = level is not None and level - current > outlier_gap

    predicted_exhaustion = None
    exhaustion_note = None
    if recent_mean >= EXHAUSTION_PERCENT or (
            current >= EXHAUSTION_PERCENT and level is not None and level >= EXHAUSTION_PERCENT):
        predicted_exhaustion = now.isoformat()
        exhaustion_note = (f'already exhausted: mean of the newest {len(recent)} sample(s) '
                           f'{recent_mean:.1f} %'
                           + (f', trend value {level:.1f} %' if level is not None else ''))
    elif spike:
        exhaustion_note = (f'not projected: newest sample {current:.1f} % is a spike'
                           + (f' above the trend value {level:.1f} %' if level is not None else '')
                           + f' (mean of the newest {len(recent)} sample(s) {recent_mean:.1f} %)')
    elif dip and level >= EXHAUSTION_PERCENT:
        exhaustion_note = (f'not projected: newest sample {current:.1f} % is far below the '
                           f'trend value {level:.1f} %')
    elif level is not None and level >= EXHAUSTION_PERCENT:
        # The whole-window trend says 90 %, the data are just under it:
        # decide on the newest samples alone
        local = _local_trend(points[-_LOCAL_SAMPLES:])
        if local is not None and local[0] > 0:
            local_slope, local_level, _, _ = local
            seconds = max(0.0, (EXHAUSTION_PERCENT - local_level) / local_slope)
            if seconds <= horizon.total_seconds():
                predicted_exhaustion = (now + timedelta(seconds=seconds)).isoformat()
                exhaustion_note = (f'trend value {level:.1f} %, newest samples {local_level:.1f} % '
                                   f'and rising')
            else:
                exhaustion_note = (f'not projected: the newest samples ({local_level:.1f} %) '
                                   f'rise too slowly to reach {EXHAUSTION_PERCENT:g} % within '
                                   f'the {horizon_label} horizon; usage is levelling off')
        elif (local is not None and local[3] >= 0
              and EXHAUSTION_PERCENT - local[1] <= min(2 * local[2], _HOVER_MAX_GAP)):
            # Hovering at the threshold: not clearly falling, and the newest
            # samples' trend value is within their noise of it
            predicted_exhaustion = now.isoformat()
            exhaustion_note = (f'at {EXHAUSTION_PERCENT:g} % now: trend value {level:.1f} %, newest '
                               f'samples {local[1]:.1f} % and flat')
        else:
            exhaustion_note = (f'not projected: the trend value {level:.1f} % is above the recent '
                               f'data (mean {recent_mean:.1f} %), which no longer rise')
    elif slope_per_second is None:
        exhaustion_note = 'not projected: no trend'
    elif slope_per_second <= 0:
        exhaustion_note = 'not projected: usage is not growing'
    else:
        seconds = (EXHAUSTION_PERCENT - level) / slope_per_second
        if seconds <= horizon.total_seconds():
            predicted_exhaustion = (now + timedelta(seconds=seconds)).isoformat()
        elif seconds <= MAX_PROJECTION.total_seconds():
            when = (f'{seconds / 3600:.1f} hours' if seconds < 2 * 86400
                    else f'{seconds / 86400:.1f} days')
            exhaustion_note = (f'not projected: reaches {EXHAUSTION_PERCENT:g} % in about '
                               f'{when}, beyond the {horizon_label} horizon')
        else:
            exhaustion_note = (f'not projected: {EXHAUSTION_PERCENT:g} % is not reached '
                               f'within {MAX_PROJECTION.days} days (projection limit)')

    trend = {
        'current': current,
        'growth_per_5min': None if slope_per_second is None else slope_per_second * _FIVE_MINUTES,
        'predicted_exhaustion': predicted_exhaustion,
    }
    if exhaustion_note:
        trend['exhaustion_note'] = exhaustion_note
    if slope_per_second is None:
        trend['growth_note'] = 'trend not computed: fewer than 3 samples at distinct times'
    return trend


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
        try:
            horizon, horizon_label = parse_time_period(forecast_horizon), forecast_horizon
        except Exception:
            log.warning(f"Invalid forecast_horizon {forecast_horizon!r}; projecting up to "
                        f"{MAX_PROJECTION.days} days")
            horizon, horizon_label = MAX_PROJECTION, None

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
            except Exception as e:
                log.warning(f"Error fetching {resource_type} metrics: {str(e)}")
                continue
            if result.get("status") != "success" or not result.get("data"):
                continue

            for metric in result["data"]:
                labels = metric.get('metric', {})
                node = labels.get('instance', 'unknown')

                # Filter out nodes that are no longer active
                if not _is_node_active(node, active_nodes):
                    filtered_count += 1
                    continue

                # One bad series must not drop the others
                try:
                    trend = _usage_trend(metric, end_time, horizon, horizon_label)
                except Exception as e:
                    log.warning(f"Could not compute {resource_type} trend for {node}: {str(e)}")
                    continue
                if trend is None:
                    continue

                identifier = {'node': node}
                if resource_type == 'disk':
                    identifier['mountpoint'] = labels.get('mountpoint', 'unknown')
                identifier['metric'] = f'{resource_type}_utilization_percent'

                growth_rate = {'value': trend['growth_per_5min'], 'unit': 'percent_per_5min'}
                if 'growth_note' in trend:
                    growth_rate['note'] = trend['growth_note']
                forecast = {
                    'resource_type': resource_type,
                    'resource_identifier': identifier,
                    'current_usage': {'value': trend['current'], 'unit': 'percent'},
                    'predicted_exhaustion': trend['predicted_exhaustion'],
                    'growth_rate': growth_rate,
                    'contributing_factors': list(factors),
                }
                if 'exhaustion_note' in trend:
                    forecast['exhaustion_note'] = trend['exhaustion_note']
                forecasts.append(forecast)

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
        # Get current cluster resource allocation from Kubernetes API; a
        # failed listing (e.g. no RBAC for nodes) or an unparsable capacity
        # leaves the affected values None and still reads usage from Prometheus
        total_nodes = total_cpu = total_memory = None
        nodes_error = None
        try:
            nodes = await list_nodes_bounded(core_api)
            total_nodes = len(nodes.items)
            cpu_sum = memory_sum = 0
            for node in nodes.items:
                capacity = (node.status.capacity if node.status else None) or {}
                cpu_sum += parse_quantity(capacity.get('cpu', '0'))
                memory_sum += parse_quantity(capacity.get('memory', '0'))
            total_cpu, total_memory = float(cpu_sum), float(memory_sum)
        except Exception as e:
            nodes_error = f"{type(e).__name__}: {e}"
            log.warning(f"Could not read node capacity: {nodes_error}")

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
            "total_memory_gb": None if total_memory is None else round(total_memory / (1024**3), 1),
            "current_cpu_usage": _percent(cpu_usage_percent),
            "current_memory_usage": _percent(memory_usage_percent),
            "data_source": {
                "nodes": ("kubernetes" if not nodes_error
                          else "partial" if total_nodes is not None else "unavailable"),
                "cpu": "unavailable" if cpu_usage_percent is None else "prometheus",
                "memory": "unavailable" if memory_usage_percent is None else "prometheus",
            },
            **({"nodes_error": nodes_error} if nodes_error else {}),
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
            "total_nodes": None,
            "total_cpu_cores": None,
            "total_memory_gb": None,
            "current_cpu_usage": None,
            "current_memory_usage": None,
            "data_source": {"nodes": "unavailable", "cpu": "unavailable", "memory": "unavailable"},
            "error": f"Cluster capacity analysis failed: {str(e)}",
            "most_constrained_resources": [],
            "fastest_growing_consumers": [],
            "capacity_runway": {}
        }
