"""Structural read-only guarantee (spec SS4.7): write verbs do not exist here."""
from __future__ import annotations


class WriteOperationError(RuntimeError):
    """A mutating or exec-capable API method was requested through the read-only client."""


_READ_PREFIXES = ("read_", "list_", "watch_", "get_")
_BLOCKED_PREFIXES = ("create_", "patch_", "delete_", "replace_", "connect_")


class ReadOnlyK8sClient:
    """Proxy over any kubernetes.client *Api exposing only read verbs.

    Covers CoreV1Api, CustomObjectsApi, AppsV1Api, BatchV1Api, StorageV1Api,
    AutoscalingV2Api — all share the same verb-prefix scheme.
    - connect_* blocked entirely (connect_get_namespaced_pod_exec is exec).
    - Non-verb attributes (api_client, ...) are DENIED by design: callers
      needing them must hold the raw client deliberately.
    - The wrapped client is sealed: ``proxy._api``, any other underscore
      name and ``__dict__`` raise AttributeError, so the raw (writable)
      client is reachable only through :func:`unwrap_readonly`.
    """

    def __init__(self, api):
        object.__setattr__(self, "_api", api)

    def __getattribute__(self, name: str):
        # Dunder protocol (__class__, __repr__, ...) stays; private names and
        # __dict__ would hand out the raw client, so they go to __getattr__.
        if name == "__dict__" or (name.startswith("_") and not
                                   (name.startswith("__") and name.endswith("__"))):
            raise AttributeError(
                f"ReadOnlyCoreV1 exposes only read verbs; {name!r} denied by design")
        return object.__getattribute__(self, name)

    @classmethod
    def wrap(cls, api) -> "ReadOnlyK8sClient":
        return api if isinstance(api, cls) else cls(api)

    def __getattr__(self, name: str):
        if name.startswith(_BLOCKED_PREFIXES):
            raise WriteOperationError(
                f"'{name}' is not available through ReadOnlyCoreV1 "
                f"(read-only client; spec SS4.7)")
        if name.startswith(_READ_PREFIXES):
            return getattr(object.__getattribute__(self, "_api"), name)
        raise AttributeError(
            f"ReadOnlyCoreV1 exposes only read verbs; {name!r} denied by design")


def unwrap_readonly(obj):
    """The raw client behind a ReadOnlyK8sClient (any other object unchanged).

    The one deliberate way past the read-only seal, for code that needs the
    client's configuration (e.g. the API server host in core.k8s_async);
    never call write verbs on the result.
    """
    if isinstance(obj, ReadOnlyK8sClient):
        return object.__getattribute__(obj, "_api")
    return obj


# Back-compat alias: 15 pre-1d wrap sites, the spy subclass (tests/_readonly_spy.py),
# and the guard tripwires reference ReadOnlyCoreV1. Aliasing (not subclassing)
# preserves isinstance identity — wrap() idempotency and spy-of-spy depend on it.
# Denial messages deliberately keep the literal "ReadOnlyCoreV1" text: error-path
# strings are behavior under the golden byte-stability contract.
ReadOnlyCoreV1 = ReadOnlyK8sClient
