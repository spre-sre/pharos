import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.readonly_client import ReadOnlyCoreV1, WriteOperationError


@pytest.fixture
def wrapped():
    api = MagicMock()
    api.read_namespaced_pod_log.return_value = "logs"
    return api, ReadOnlyCoreV1(api)


def test_read_and_list_pass_through(wrapped):
    api, ro = wrapped
    assert ro.read_namespaced_pod_log("p", "ns") == "logs"
    ro.list_namespaced_pod("ns")
    api.list_namespaced_pod.assert_called_once_with("ns")


@pytest.mark.parametrize("verb", [
    "create_namespaced_pod", "patch_namespaced_config_map",
    "delete_namespaced_pod", "replace_namespaced_secret",
])
def test_write_verbs_blocked(wrapped, verb):
    _, ro = wrapped
    with pytest.raises(WriteOperationError):
        getattr(ro, verb)


def test_connect_subresources_blocked(wrapped):
    # connect_get_namespaced_pod_exec is a GET by HTTP verb but executes
    # arbitrary commands - blocked despite the read-ish name.
    _, ro = wrapped
    with pytest.raises(WriteOperationError):
        ro.connect_get_namespaced_pod_exec


def test_non_verb_attributes_denied(wrapped):
    # deliberate design: .api_client access must fail loudly, not bypass
    _, ro = wrapped
    with pytest.raises(AttributeError):
        ro.api_client


def test_idempotent_wrap():
    api = MagicMock()
    ro = ReadOnlyCoreV1(api)
    assert ReadOnlyCoreV1.wrap(ro) is ro
    assert isinstance(ReadOnlyCoreV1.wrap(api), ReadOnlyCoreV1)


def test_generalized_client_passes_customobjects_reads():
    """ReadOnlyK8sClient forwards CustomObjectsApi-shaped read verbs."""
    from core.readonly_client import ReadOnlyK8sClient
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {"items": []}
    ro = ReadOnlyK8sClient.wrap(api)
    assert ro.list_namespaced_custom_object(group="tekton.dev") == {"items": []}
    ro.get_namespaced_custom_object(name="x")
    ro.list_cluster_custom_object(group="config.openshift.io")
    api.get_namespaced_custom_object.assert_called_once_with(name="x")


def test_generalized_client_blocks_customobjects_writes():
    """CustomObjectsApi write verbs raise WriteOperationError, never reach the api."""
    from core.readonly_client import ReadOnlyK8sClient
    api = MagicMock()
    ro = ReadOnlyK8sClient.wrap(api)
    for verb in ("create_namespaced_custom_object", "patch_namespaced_custom_object",
                 "delete_namespaced_custom_object", "replace_cluster_custom_object"):
        with pytest.raises(WriteOperationError):
            getattr(ro, verb)
    api.assert_not_called()


def test_alias_identity_is_load_bearing():
    """ReadOnlyCoreV1 IS ReadOnlyK8sClient (same object): wrap() idempotency and
    the RecordingReadOnly spy subclass depend on this identity."""
    from core.readonly_client import ReadOnlyK8sClient
    assert ReadOnlyCoreV1 is ReadOnlyK8sClient
    api = MagicMock()
    ro = ReadOnlyK8sClient.wrap(api)
    assert ReadOnlyCoreV1.wrap(ro) is ro  # idempotent across both names
