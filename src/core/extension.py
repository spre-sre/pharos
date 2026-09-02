"""Extension mechanism for Lumino (phase 2d).

Provides:
- INTREE_EXTENSIONS: canonical tuple of built-in extension names.
- DetectContext: frozen dataclass passed to every extension's detect().
- Extension: structural Protocol that extension packages implement.
- ToolRegistry: facade that extensions register tools through; deliberately
  hides the raw FastMCP object and late-binds all server attributes so the
  test harness can monkeypatch them per-case without rebuilding the facade.
- _InstanceView: lightweight stateless per-source view of ToolRegistry (D9).
  Resolves the same 5 properties as ToolRegistry but bound to a named instance.
- InstanceResolutionError: raised by _InstanceView when _resolve_k8s returns
  an error dict; extension tools catch this and return the dict.
- detect_and_register(): async core of the auto-detection flow.
- activate_extensions(): sync startup runner that iterates extensions in
  sorted order and dispatches on their configured mode.

Import discipline: this module must NOT import core.config — Task 2 makes
core/config.py import INTREE_EXTENSIONS from here, which would create a cycle.
Only core.config_types and core.registry are referenced, and only under
TYPE_CHECKING so they are erased at runtime.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Tuple
from typing import Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.config_types import ResolvedConfig
    from core.registry import AdapterRegistry

# The only extension-discovery source in phase 2d.  Sorted alphabetically.
INTREE_EXTENSIONS: Tuple[str, ...] = ("konflux", "openshift", "tekton")


# ── D9 Instance resolution error ─────────────────────────────────────────────

class InstanceResolutionError(Exception):
    """Raised by _InstanceView when _resolve_k8s returns an error dict.

    Carries the full structured error dict from _resolve_k8s so that extension
    tools can return it unchanged (rather than wrapping it in str()).

    Extension tools catch this at the top level and return error_dict directly.
    """

    def __init__(self, error_dict: Dict[str, Any]) -> None:
        self.error_dict = error_dict
        super().__init__(repr(error_dict))


@dataclass(frozen=True)
class DetectContext:
    """Immutable context passed to each extension's detect() method.

    discover_api_groups: async callable that performs a single cheap read of
    /apis (via the RO client) and returns the set of registered API groups.
    Results are expected to be cached by the caller so multiple extensions
    pay the I/O cost only once.
    """

    config: "ResolvedConfig"
    adapters: "AdapterRegistry"
    instance: str  # default "kubernetes" in phase 2d; per-instance in 2e
    discover_api_groups: Callable[[str], Awaitable[frozenset]]


@runtime_checkable
class Extension(Protocol):
    """Structural protocol every extension package must satisfy."""

    name: str

    async def detect(self, ctx: DetectContext) -> bool: ...

    def register(self, reg: "ToolRegistry") -> None: ...


class _InstanceView:
    """Lightweight stateless per-source view of ToolRegistry (D9).

    Exposes the same 5 properties that extension tools consume, but resolves
    them against a named kubernetes instance rather than the server's default
    globals.  Resolution is ALWAYS at access time (late-binding) — never stored
    at construction — so that connect_cluster updates and test monkeypatches are
    visible to subsequent accesses without rebuilding the view.

    Resolution failures (unknown/disconnected source) raise InstanceResolutionError
    carrying the error dict from _resolve_k8s; extension tools catch this at their
    top level and return the dict.

    query_prometheus passes kwargs through so Task 4's bearer_token= extension
    can add new kwargs without rewriting the view.
    """

    def __init__(self, registry: "ToolRegistry", source: str) -> None:
        self._registry = registry
        self._source = source

    def _resolve_clients(self) -> Any:
        """Call _resolve_k8s on the server; raise InstanceResolutionError on failure."""
        fn = getattr(self._registry._server, "_resolve_k8s")
        clients, err = fn(self._source)
        if err is not None:
            raise InstanceResolutionError(err)
        return clients

    @property
    def k8s_core_api(self) -> Any:
        return self._resolve_clients().core_api

    @property
    def k8s_apps_api(self) -> Any:
        return self._resolve_clients().apps_api

    @property
    def k8s_custom_api(self) -> Any:
        return self._resolve_clients().custom_api

    @property
    def query_prometheus(self) -> Any:
        """Return a wrapper that late-binds this instance's clients as kwargs.

        kwargs-forwarding shape: Task 4 extends with bearer_token= without
        rewriting this view.
        """
        registry = self._registry
        source = self._source

        async def _query_with_instance_clients(query: str, timeout: int = 30, **kwargs) -> Any:
            fn = getattr(registry._server, "_execute_prometheus_query_internal")
            resolve = getattr(registry._server, "_resolve_k8s")
            clients, err = resolve(source)
            if err is not None:
                raise InstanceResolutionError(err)
            kwargs.setdefault("custom_api", clients.custom_api)
            kwargs.setdefault("core_api", clients.core_api)
            # Task 4: inject per-instance bearer token; never consult the default chain for
            # named sources (spec: default chain is seam-pinned to source="" only).
            # _instance_tokens.get(source) returns None for cert-auth and unregistered
            # instances — both → token_unavailable in _execute_prometheus_query_internal.
            _instance_tokens = getattr(registry._server, "_instance_tokens", {})
            kwargs.setdefault("bearer_token", _instance_tokens.get(source))
            kwargs.setdefault("source", source)
            return await fn(query, timeout=timeout, **kwargs)

        return _query_with_instance_clients

    @property
    def detect_tekton_namespaces(self) -> Any:
        """Resolves the server's Tekton namespace detector at access time."""
        return getattr(self._registry._server, "detect_tekton_namespaces")

    @property
    def query_archived_plrs(self) -> Any:
        """Late-bound archived-PLR fetcher, pinned to this view's source.

        Wraps the server's _query_archived_plrs_for_trace (best-effort: [] on
        any unavailability) so tracer code calls one seam for live+archive.
        """
        registry = self._registry
        source = self._source

        async def _query_with_instance_source(namespace: str, **kwargs) -> Any:
            fn = getattr(registry._server, "_query_archived_plrs_for_trace")
            kwargs.setdefault("source", source)
            return await fn(namespace, **kwargs)

        return _query_with_instance_source

    def extension_active(self, name: str) -> bool:
        """Return True iff the named extension is 'active' on this instance.

        Reads _extension_states late-bound from the server so connect_cluster
        updates (which write new entries) are visible without rebuilding the view.
        The default when the key is absent is False (fail-closed).
        """
        states = getattr(self._registry._server, "_extension_states", {})
        return states.get((name, self._source)) == "active"


class ToolRegistry:
    """Facade that extensions use to register tools.

    Design constraints:
    - NEVER exposes the raw FastMCP object.  The mcp instance is stored with
      Python name-mangling (self.__mcp) so 'mcp' and '_mcp' are not
      reachable as attributes from extension code.
    - All server-module attributes are accessed via properties (late binding)
      so the session-scoped golden harness can monkeypatch server attrs per
      test case without rebuilding the facade.
    """

    def __init__(
        self,
        server_module: Any,
        mcp: Any,
        config: "ResolvedConfig",
        adapters: "AdapterRegistry",
        packs: Dict[str, Any],
    ) -> None:
        self._server = server_module
        self.__mcp = mcp  # name-mangled: _ToolRegistry__mcp
        self.config = config
        self.adapters = adapters
        self.packs = packs  # name -> Pack (Task 4; {} until then)
        self.registered: Dict[str, Any] = {}

    # ── Late-bound server properties ─────────────────────────────────────────

    @property
    def k8s_core_api(self) -> Any:
        return getattr(self._server, "k8s_core_api")

    @property
    def k8s_apps_api(self) -> Any:
        return getattr(self._server, "k8s_apps_api")

    @property
    def k8s_custom_api(self) -> Any:
        return getattr(self._server, "k8s_custom_api")

    @property
    def query_prometheus(self) -> Any:
        """Resolves the server's internal Prometheus executor; injects K8s clients at call time.

        Returns a wrapper that late-binds k8s_custom_api and k8s_core_api from
        the server's current globals at invocation time, so connect_cluster updates
        are visible to subsequent calls without rebuilding the facade.
        """
        server = self._server
        async def _query_with_clients(query: str, timeout: int = 30, **kwargs):
            fn = getattr(server, "_execute_prometheus_query_internal")
            kwargs.setdefault("custom_api", getattr(server, "k8s_custom_api", None))
            kwargs.setdefault("core_api", getattr(server, "k8s_core_api", None))
            return await fn(query, timeout=timeout, **kwargs)
        return _query_with_clients

    @property
    def detect_tekton_namespaces(self) -> Any:
        """Resolves the server's Tekton namespace detector at access time."""
        return getattr(self._server, "detect_tekton_namespaces")

    @property
    def query_archived_plrs(self) -> Any:
        """Late-bound archived-PLR fetcher for the default source (source="")."""
        server = self._server

        async def _query_default_source(namespace: str, **kwargs) -> Any:
            fn = getattr(server, "_query_archived_plrs_for_trace")
            kwargs.setdefault("source", "")
            return await fn(namespace, **kwargs)

        return _query_default_source

    # ── Per-instance view factory (D9) ────────────────────────────────────────

    def for_instance(self, source: str) -> "ToolRegistry | _InstanceView":
        """Return a per-source view (D9), or self when source is empty.

        When source is '' the default path is byte-preserved: no view is
        consulted, so monkeypatching server globals still reaches the tools.
        When source is non-empty, returns an _InstanceView that resolves all
        5 consumed properties via _resolve_k8s(source) at access time.
        """
        if not source:
            return self
        return _InstanceView(self, source)

    # ── Tool registration ─────────────────────────────────────────────────────

    def tool(self) -> Callable:
        """Decorator factory.  Reproduces the enhanced-decorator pipeline from
        server-mcp.py:432-459:

          logged = log_tool_execution(fn)   # wrap with execution logging
          mcp.add_tool(logged)              # register in FastMCP (bare add_tool
                                            # preserves the logging wrapper;
                                            # mcp.tool() would re-wrap)
          setattr(server, fn.__name__, logged)  # rebind so existing refs update
          self.registered[fn.__name__] = logged
          return logged

        log_tool_execution is fetched from the server module at decoration time
        (late binding) so unit tests can inject a fake via SimpleNamespace.
        """

        def decorator(fn: Callable) -> Callable:
            log_tool_execution = getattr(self._server, "log_tool_execution")
            logged = log_tool_execution(fn)
            self.__mcp.add_tool(logged)
            setattr(self._server, fn.__name__, logged)
            self.registered[fn.__name__] = logged
            return logged

        return decorator

    def register_server_tool(self, name: str) -> None:
        """Task 6 path: pull an existing server-module function by name and
        run it through the same wrap / add / rebind / record pipeline as
        the tool() decorator."""
        fn = getattr(self._server, name)
        log_tool_execution = getattr(self._server, "log_tool_execution")
        logged = log_tool_execution(fn)
        self.__mcp.add_tool(logged)
        setattr(self._server, name, logged)
        self.registered[name] = logged


# ── Async detection core ──────────────────────────────────────────────────────

async def detect_and_register(
    ext: "Extension",
    facade: ToolRegistry,
    ctx: DetectContext,
    timeout_s: float = 2.0,
) -> Tuple[str, List[str]]:
    """Shared auto-mode core (round-1 F1).

    Awaits ext.detect(ctx) with a timeout, then:
      True      -> ext.register(facade); return ('active', newly_registered)
      False     -> return ('not-detected: absent', [])
      Timeout   -> return ('not-detected: timeout', [])
      Exception -> return ('not-detected: error: <TypeName>', [])

    refresh_capabilities awaits this directly (already inside the MCP server
    loop); startup wraps it in asyncio.run() (no loop at import time).
    """
    before = set(facade.registered)
    try:
        detected = await asyncio.wait_for(ext.detect(ctx), timeout_s)
    except asyncio.TimeoutError:
        return ("not-detected: timeout", [])
    except Exception as exc:  # noqa: BLE001
        return (f"not-detected: error: {type(exc).__name__}", [])

    if not detected:
        return ("not-detected: absent", [])

    ext.register(facade)
    newly = sorted(set(facade.registered) - before)
    return ("active", newly)


# ── Sync startup runner ───────────────────────────────────────────────────────

def activate_extensions(
    config: "ResolvedConfig",
    extensions: List["Extension"],
    facade: ToolRegistry,
    ctx: DetectContext,
    timeout_s: float = 2.0,
) -> Dict[Tuple[str, str], str]:
    """SYNC, startup-only.

    Iterates extensions in alphabetical order and dispatches on the mode
    from config.extensions[ext.name]:

      'on'   -> ext.register(facade); state 'active'
      'off'  -> skip;                 state 'off'
      'auto' -> asyncio.run(detect_and_register(...)); state = result[0]
      other  -> state 'off'  (config validation in Task 2 rejects upstream)

    Returns {(ext_name, instance): state}.
    """
    instance = ctx.instance
    result: Dict[Tuple[str, str], str] = {}

    for ext in sorted(extensions, key=lambda e: e.name):
        mode = config.extensions.get(ext.name, "auto")

        if mode == "on":
            ext.register(facade)
            state = "active"
        elif mode == "off":
            state = "off"
        elif mode == "auto":
            state, _ = asyncio.run(detect_and_register(ext, facade, ctx, timeout_s))
        else:
            state = "off"

        result[(ext.name, instance)] = state

    return result
