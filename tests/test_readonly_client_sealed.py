"""ReadOnlyK8sClient does not hand out the raw writable client (H28).

proxy._api returned the wrapped kubernetes API object, and from it every write
verb (proxy._api.delete_namespace(...)). __getattr__ only runs for missing
attributes, so the read-only guarantee held by convention only.
"""
import copy
import gc
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.readonly_client import (  # noqa: E402
    ReadOnlyCoreV1, ReadOnlyK8sClient, WriteOperationError, unwrap_readonly)


class _FakeApi:
    api_client = "raw-api-client"

    def list_namespace(self, **kwargs):
        return "namespaces"

    def delete_namespace(self, name, **kwargs):
        raise AssertionError("a write reached the raw client")


@pytest.mark.parametrize("name", [
    "_api", "_ReadOnlyK8sClient__api", "__dict__", "_anything", "api_client",
])
def test_private_and_non_verb_attributes_are_denied(name):
    proxy = ReadOnlyK8sClient(_FakeApi())
    with pytest.raises(AttributeError):
        getattr(proxy, name)


def test_vars_does_not_expose_the_raw_client():
    proxy = ReadOnlyK8sClient(_FakeApi())
    with pytest.raises(TypeError):
        vars(proxy)


def test_write_verbs_still_raise_write_operation_error():
    proxy = ReadOnlyK8sClient(_FakeApi())
    with pytest.raises(WriteOperationError):
        proxy.delete_namespace("x")


def test_read_verbs_still_work():
    assert ReadOnlyK8sClient(_FakeApi()).list_namespace() == "namespaces"


def test_unwrap_is_the_deliberate_escape_hatch():
    raw = _FakeApi()
    proxy = ReadOnlyK8sClient(raw)
    assert unwrap_readonly(proxy) is raw
    assert unwrap_readonly(raw) is raw  # not a proxy: returned unchanged


def test_wrap_is_idempotent_and_alias_is_identical():
    proxy = ReadOnlyCoreV1.wrap(_FakeApi())
    assert ReadOnlyK8sClient.wrap(proxy) is proxy
    assert ReadOnlyCoreV1 is ReadOnlyK8sClient


def test_dunder_protocol_still_works():
    proxy = ReadOnlyK8sClient(_FakeApi())
    assert isinstance(proxy, ReadOnlyK8sClient)
    assert type(proxy).__name__ == "ReadOnlyK8sClient"
    assert "ReadOnlyK8sClient" in repr(proxy)


def test_pickling_is_refused_and_does_not_carry_the_raw_client():
    proxy = ReadOnlyK8sClient(_FakeApi())
    with pytest.raises(TypeError):
        pickle.dumps(proxy)
    with pytest.raises(TypeError):
        proxy.__reduce_ex__(4)
    with pytest.raises(TypeError):
        proxy.__reduce__()


def test_copy_returns_the_same_proxy():
    proxy = ReadOnlyK8sClient(_FakeApi())
    assert copy.copy(proxy) is proxy
    assert copy.deepcopy(proxy) is proxy


def test_no_reference_from_the_proxy_reaches_the_raw_client():
    raw = _FakeApi()
    proxy = ReadOnlyK8sClient(raw)
    assert raw not in gc.get_referents(proxy)
    state = proxy.__getstate__() if hasattr(type(proxy), "__getstate__") else None
    assert state is None or raw not in (state.values() if isinstance(state, dict) else [state])


def test_uninitialised_proxy_raises_attribute_error_not_key_error():
    proxy = object.__new__(ReadOnlyK8sClient)
    assert not hasattr(proxy, "list_namespace")
    with pytest.raises(AttributeError):
        unwrap_readonly(proxy)


def test_custom_equality_in_a_subclass_cannot_cross_wire_clients():
    """The registry is keyed by identity, so __eq__/__hash__ do not matter."""
    class _Equal(ReadOnlyK8sClient):
        def __eq__(self, other):
            return True

        def __hash__(self):
            return 1

    raw_a, raw_b = _FakeApi(), _FakeApi()
    a, b = _Equal(raw_a), _Equal(raw_b)
    assert unwrap_readonly(a) is raw_a
    assert unwrap_readonly(b) is raw_b


def test_registry_entry_is_dropped_with_the_proxy():
    import core.readonly_client as rc
    before = len(rc._RAW_CLIENTS)
    proxy = ReadOnlyK8sClient(_FakeApi())
    assert len(rc._RAW_CLIENTS) == before + 1
    del proxy
    gc.collect()
    assert len(rc._RAW_CLIENTS) == before
