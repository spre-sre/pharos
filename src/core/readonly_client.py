"""Structural read-only guarantee (spec SS4.7): write verbs do not exist here."""
from __future__ import annotations

import weakref


class WriteOperationError(RuntimeError):
    """A mutating or exec-capable API method was requested through the read-only client."""


_READ_PREFIXES = ("read_", "list_", "watch_", "get_")
_BLOCKED_PREFIXES = ("create_", "patch_", "delete_", "replace_", "connect_")


# proxy -> raw client; weak keys, so a dropped proxy does not keep its entry.
_RAW_CLIENTS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class ReadOnlyK8sClient:
    """Proxy over any kubernetes.client *Api exposing only read verbs.

    Covers CoreV1Api, CustomObjectsApi, AppsV1Api, BatchV1Api, StorageV1Api,
    AutoscalingV2Api — all share the same verb-prefix scheme.
    - connect_* blocked entirely (connect_get_namespaced_pod_exec is exec).
    - Non-verb attributes (api_client, ...) are DENIED by design: callers
      needing them must hold the raw client deliberately.
    - The wrapped client is not stored on the proxy: it lives in a
      module-level weak registry read by :func:`unwrap_readonly`, so
      ``proxy._api``, ``__dict__``, pickling state and the like do not
      reach it. Underscore names raise AttributeError; pickling raises
      TypeError; copy/deepcopy return the same proxy (it is immutable).
    """

    __slots__ = ("__weakref__",)

    def __init__(self, api):
        _RAW_CLIENTS[self] = api

    def __reduce__(self):  # object.__reduce_ex__ defers to it
        raise TypeError("ReadOnlyK8sClient cannot be pickled")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

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
            return getattr(_RAW_CLIENTS[self], name)
        raise AttributeError(
            f"ReadOnlyCoreV1 exposes only read verbs; {name!r} denied by design")


def unwrap_readonly(obj):
    """The raw client behind a ReadOnlyK8sClient (any other object unchanged).

    The one deliberate way past the read-only seal, for code that needs the
    client's configuration (e.g. the API server host in core.k8s_async);
    never call write verbs on the result.
    """
    if isinstance(obj, ReadOnlyK8sClient):
        return _RAW_CLIENTS[obj]
    return obj


# Back-compat alias: 15 pre-1d wrap sites, the spy subclass (tests/_readonly_spy.py),
# and the guard tripwires reference ReadOnlyCoreV1. Aliasing (not subclassing)
# preserves isinstance identity — wrap() idempotency and spy-of-spy depend on it.
# Denial messages deliberately keep the literal "ReadOnlyCoreV1" text: error-path
# strings are behavior under the golden byte-stability contract.
ReadOnlyCoreV1 = ReadOnlyK8sClient
