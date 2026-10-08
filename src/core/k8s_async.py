"""Run blocking Kubernetes client calls off the event loop, with a timeout.

The kubernetes Python client is synchronous. Calling it inside ``async def``
blocks the whole MCP server (every client, /health) for the duration of the
HTTP request, and with no ``_request_timeout`` a hung apiserver blocks it
forever. ``k8s_call`` runs the call with a request timeout on a bounded
thread pool, so a fan-out over many namespaces queues instead of exhausting
the default executor — also when a deadline cancels the await while the
worker thread is still running.

There is one pool per cluster (keyed by the API server host of the client the
call goes to), so a slow or stuck cluster fills only its own pool and never
delays calls to another cluster. Pools live for the process lifetime: at most
MAX_CONCURRENT_CALLS threads per API server host ever used (bounded by the
configured and connected clusters).

    pods = await k8s_call(core_api.list_namespaced_pod, namespace="team-a")
    ok = await k8s_offload(sync_helper_that_sets_its_own_timeouts, core_api, arg)

tests/test_no_blocking_k8s_calls.py fails if a Kubernetes call in an
``async def`` bypasses these helpers again.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, TypeVar

try:
    from core.readonly_client import ReadOnlyK8sClient
except ImportError:  # imported as src.core.k8s_async (repo root on sys.path)
    from src.core.readonly_client import ReadOnlyK8sClient

T = TypeVar("T")

# Seconds for one Kubernetes API request (urllib3 total timeout).
DEFAULT_TIMEOUT: float = 30.0
# Pod logs can be large and slow to stream.
LOG_TIMEOUT: float = 120.0
# Kubernetes calls running at once per cluster. Dedicated pools (not the
# default executor) bound the threads themselves: a cancelled await cannot
# free a slot while its thread is still blocked in a request.
MAX_CONCURRENT_CALLS: int = 8

_DEFAULT_KEY = "default"
# Attributes that hold a Kubernetes API object on helper classes
# (KubeArchive discovery/client, registries) passed to k8s_offload.
_API_ATTRS = ("api_client", "k8s_core_api", "core_api", "k8s_custom_api", "custom_api")

_pools: Dict[str, ThreadPoolExecutor] = {}
_pools_lock = threading.Lock()


def _api_host(obj: Any) -> Optional[str]:
    try:
        host = obj.api_client.configuration.host
    except Exception:
        return None
    if not isinstance(host, str) or not host:
        return None
    return host.rstrip("/").lower()


def _unwrap(obj: Any) -> Any:
    """The real API object behind a ReadOnlyK8sClient, read directly (no
    attribute access through the wrapper, so read-only spies see no extra
    lookups)."""
    if isinstance(obj, ReadOnlyK8sClient):
        return object.__getattribute__(obj, "_api")
    return obj


def _host_of(obj: Any, _depth: int = 0) -> Optional[str]:
    """Normalized API server host behind an API object, a bound API method,
    a ReadOnlyK8sClient wrapper, or a helper object holding one; else None."""
    if obj is None or _depth > 2:
        return None
    obj = _unwrap(obj)
    host = _api_host(obj) or _api_host(getattr(obj, "__self__", None))
    if host:
        return host
    for name in _API_ATTRS:
        try:
            inner = getattr(obj, name)
        except Exception:
            continue
        if inner is not obj:
            host = _host_of(inner, _depth + 1)
            if host:
                return host
    return None


def _pool(key: Optional[str]) -> ThreadPoolExecutor:
    key = key or _DEFAULT_KEY
    pool = _pools.get(key)
    if pool is None:
        with _pools_lock:
            pool = _pools.get(key)
            if pool is None:
                pool = _pools[key] = ThreadPoolExecutor(
                    max_workers=MAX_CONCURRENT_CALLS, thread_name_prefix=f"k8s-call-{len(_pools)}")
    return pool


def _key_for(fn: Callable[..., Any], args: tuple) -> Optional[str]:
    return _host_of(getattr(fn, "__self__", None)) or (_host_of(args[0]) if args else None)


async def k8s_offload(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Await ``fn(*args, **kwargs)`` on the pool of the cluster it talks to.

    For sync helpers that make Kubernetes calls and set their own
    ``_request_timeout``; plain API methods go through :func:`k8s_call`. The
    cluster is found from ``fn``'s ``self`` or its first argument; otherwise
    the shared default pool is used.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(_pool(_key_for(fn, args)), call)


async def k8s_call(fn: Callable[..., T], /, *args: Any, timeout: float = DEFAULT_TIMEOUT, **kwargs: Any) -> T:
    """Await ``fn(*args, **kwargs, _request_timeout=timeout)`` on its cluster's pool."""
    return await k8s_offload(fn, *args, _request_timeout=timeout, **kwargs)
