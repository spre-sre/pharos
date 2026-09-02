"""Node and cluster resource forecasting helpers."""
import asyncio
import functools
import logging
from datetime import datetime
from typing import Any, Dict, List

from core.readonly_client import ReadOnlyK8sClient
from helpers.utils import (
    _get_active_node_names,
    _is_node_active,
    _NODE_LISTING_EXECUTOR,
    calculate_forecast_intervals,
    list_nodes_bounded,
    parse_time_period,
    simple_linear_forecast,
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


async def _analyze_node_resources_new(trend_period: str, forecast_horizon: str, log, *, query_fn, core_api) -> List[Dict]:
    """Analyze node-level resource utilization using Prometheus query method."""
    try:
        from datetime import timedelta

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

        # Calculate time range for trend analysis
        end_time = datetime.now()
        start_time = end_time - parse_time_period(trend_period)

        # Convert to ISO format for the query method
        start_time_iso = start_time.isoformat() + "Z"
        end_time_iso = end_time.isoformat() + "Z"

        forecasts = []
        filtered_count = 0
        forecast_points = calculate_forecast_intervals(forecast_horizon)

        # Node CPU usage query - aggregate to avoid series explosion from pod restarts
        cpu_query = 'max by (instance) (100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))'

        try:
            cpu_result = await query_fn(
                query=cpu_query,
                query_type="range",
                start_time=start_time_iso,
                end_time=end_time_iso,
                step="300s",
                limit=100  # Limit to top 100 nodes
            )

            if cpu_result.get("status") == "success" and cpu_result.get("data"):
                for metric in cpu_result["data"]:
                    node = metric.get('metric', {}).get('instance', 'unknown')

                    # Filter out nodes that are no longer active
                    if not _is_node_active(node, active_nodes):
                        filtered_count += 1
                        continue

                    values = [float(point[1]) for point in metric.get('values', [])]

                    if values:
                        forecast_result = simple_linear_forecast(values, forecast_points)
                        current_usage = values[-1] if values else 0

                        # Predict exhaustion time
                        predicted_exhaustion = None
                        if forecast_result['growth_rate'] > 0:
                            # Calculate when it might reach 90%
                            points_to_90 = (90 - current_usage) / forecast_result['growth_rate']
                            if points_to_90 > 0:
                                exhaustion_time = end_time + timedelta(minutes=5 * points_to_90)
                                predicted_exhaustion = exhaustion_time.isoformat()

                        forecasts.append({
                            'resource_type': 'cpu',
                            'resource_identifier': {'node': node, 'metric': 'cpu_utilization_percent'},
                            'current_usage': {'value': current_usage, 'unit': 'percent'},
                            'predicted_exhaustion': predicted_exhaustion,
                            'growth_rate': {'value': forecast_result['growth_rate'], 'unit': 'percent_per_5min'},
                            'contributing_factors': ['workload_scaling', 'baseline_usage_trend']
                        })
        except Exception as e:
            log.warning(f"Error fetching CPU metrics: {str(e)}")

        # Node memory usage query - aggregate by instance to avoid series explosion from pod restarts
        memory_query = 'max by (instance) ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)'

        try:
            memory_result = await query_fn(
                query=memory_query,
                query_type="range",
                start_time=start_time_iso,
                end_time=end_time_iso,
                step="300s",
                limit=100  # Limit to top 100 nodes
            )

            if memory_result.get("status") == "success" and memory_result.get("data"):
                for metric in memory_result["data"]:
                    node = metric.get('metric', {}).get('instance', 'unknown')

                    # Filter out nodes that are no longer active
                    if not _is_node_active(node, active_nodes):
                        filtered_count += 1
                        continue

                    values = [float(point[1]) for point in metric.get('values', [])]

                    if values:
                        forecast_result = simple_linear_forecast(values, forecast_points)
                        current_usage = values[-1] if values else 0

                        predicted_exhaustion = None
                        if forecast_result['growth_rate'] > 0:
                            points_to_90 = (90 - current_usage) / forecast_result['growth_rate']
                            if points_to_90 > 0:
                                exhaustion_time = end_time + timedelta(minutes=5 * points_to_90)
                                predicted_exhaustion = exhaustion_time.isoformat()

                        forecasts.append({
                            'resource_type': 'memory',
                            'resource_identifier': {'node': node, 'metric': 'memory_utilization_percent'},
                            'current_usage': {'value': current_usage, 'unit': 'percent'},
                            'predicted_exhaustion': predicted_exhaustion,
                            'growth_rate': {'value': forecast_result['growth_rate'], 'unit': 'percent_per_5min'},
                            'contributing_factors': ['memory_leaks', 'workload_growth', 'cache_usage']
                        })
        except Exception as e:
            log.warning(f"Error fetching memory metrics: {str(e)}")

        # Node disk usage query - filter out kubelet pod volumes and aggregate by instance/mountpoint
        # to avoid series explosion from node-exporter pod restarts
        disk_query = '''max by (instance, mountpoint) (
            (1 - (node_filesystem_avail_bytes{fstype!="tmpfs", mountpoint!~"/var/lib/kubelet/pods.*|/run/.*"}
                / node_filesystem_size_bytes{fstype!="tmpfs", mountpoint!~"/var/lib/kubelet/pods.*|/run/.*"})) * 100
        )'''

        try:
            disk_result = await query_fn(
                query=disk_query,
                query_type="range",
                start_time=start_time_iso,
                end_time=end_time_iso,
                step="300s",
                limit=200  # Limit disk filesystems to top 200
            )

            if disk_result.get("status") == "success" and disk_result.get("data"):
                for metric in disk_result["data"]:
                    node = metric.get('metric', {}).get('instance', 'unknown')

                    # Filter out nodes that are no longer active
                    if not _is_node_active(node, active_nodes):
                        filtered_count += 1
                        continue

                    mountpoint = metric.get('metric', {}).get('mountpoint', 'unknown')
                    values = [float(point[1]) for point in metric.get('values', [])]

                    if values:
                        forecast_result = simple_linear_forecast(values, forecast_points)
                        current_usage = values[-1] if values else 0

                        predicted_exhaustion = None
                        if forecast_result['growth_rate'] > 0:
                            points_to_90 = (90 - current_usage) / forecast_result['growth_rate']
                            if points_to_90 > 0:
                                exhaustion_time = end_time + timedelta(minutes=5 * points_to_90)
                                predicted_exhaustion = exhaustion_time.isoformat()

                        forecasts.append({
                            'resource_type': 'disk',
                            'resource_identifier': {'node': node, 'mountpoint': mountpoint, 'metric': 'disk_utilization_percent'},
                            'current_usage': {'value': current_usage, 'unit': 'percent'},
                            'predicted_exhaustion': predicted_exhaustion,
                            'growth_rate': {'value': forecast_result['growth_rate'], 'unit': 'percent_per_5min'},
                            'contributing_factors': ['log_growth', 'cache_accumulation', 'temporary_files']
                        })
        except Exception as e:
            log.warning(f"Error fetching disk metrics: {str(e)}")

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

        # Get current cluster resource usage via Prometheus
        cpu_usage_percent = 0
        memory_usage_percent = 0

        try:
            # Cluster CPU usage
            cpu_usage_result = await query_fn(
                'avg(100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))'
            )
            if cpu_usage_result.get("status") == "success" and cpu_usage_result.get("data"):
                data = cpu_usage_result["data"]
                if data and len(data) > 0 and 'value' in data[0]:
                    cpu_usage_percent = float(data[0]['value'])
        except Exception as e:
            log.warning(f"Could not fetch cluster CPU usage: {str(e)}")

        try:
            # Cluster memory usage
            memory_usage_result = await query_fn(
                'avg(100 - (avg by (instance) (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)'
            )
            if memory_usage_result.get("status") == "success" and memory_usage_result.get("data"):
                data = memory_usage_result["data"]
                if data and len(data) > 0 and 'value' in data[0]:
                    memory_usage_percent = float(data[0]['value'])
        except Exception as e:
            log.warning(f"Could not fetch cluster memory usage: {str(e)}")

        # Determine overall health
        overall_health = "healthy"
        if cpu_usage_percent > 80 or memory_usage_percent > 80:
            overall_health = "degraded"
        elif cpu_usage_percent > 90 or memory_usage_percent > 90:
            overall_health = "critical"

        # Identify most constrained resources
        constrained_resources = []
        if cpu_usage_percent > 70:
            constrained_resources.append(f"CPU ({cpu_usage_percent:.1f}%)")
        if memory_usage_percent > 70:
            constrained_resources.append(f"Memory ({memory_usage_percent:.1f}%)")

        return {
            "overall_health": overall_health,
            "total_nodes": total_nodes,
            "total_cpu_cores": total_cpu,
            "total_memory_gb": round(total_memory / (1024**3), 1),
            "current_cpu_usage": f"{cpu_usage_percent:.1f}%",
            "current_memory_usage": f"{memory_usage_percent:.1f}%",
            "most_constrained_resources": constrained_resources,
            "fastest_growing_consumers": [],  # Would need historical analysis
            "capacity_runway": {
                "cpu_runway_days": max(0, int((90 - cpu_usage_percent) / max(0.1, cpu_usage_percent / 30))),
                "memory_runway_days": max(0, int((90 - memory_usage_percent) / max(0.1, memory_usage_percent / 30)))
            }
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
