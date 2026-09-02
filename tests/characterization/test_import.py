from .conftest import registered_tool_names


def test_module_imports_and_registers_tools(server):
    names = registered_tool_names(server)
    assert len(names) >= 39, f"expected >=39 tools, got {len(names)}: {sorted(names)}"
    assert "list_namespaces" in names
    assert "prometheus_query" in names


def test_k8s_clients_constructed(server):
    # With a valid (fake) kubeconfig the module-level clients must exist,
    # otherwise most tools short-circuit and goldens would be vacuous.
    assert server.k8s_core_api is not None
    assert server.k8s_custom_api is not None
