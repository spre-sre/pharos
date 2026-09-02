"""Regression: the PRODUCTION module loader in main.py must register the module
in sys.modules BEFORE exec_module.

The phase-2d extension-activation block resolves its own module object via
sys.modules[__name__].  The test harness (tests/characterization/conftest.py)
and the phase-2d subprocess test both register "server_mcp" before exec_module,
so the suite stayed green while the real entry point (main.py) crashed at
startup with KeyError('server_mcp') — caught only by driving `main.py` itself.
This test exercises main.load_server_module() in a fresh interpreter, exactly
as production does.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1"}
  name: fake
contexts:
- context: {cluster: fake, user: fake}
  name: fake
current-context: fake
users:
- name: fake
  user: {token: "fake-token"}
"""

_CHILD_CODE = """\
import json
import main

mod = main.load_server_module()
print(json.dumps({
    "tool_count": len(mod.mcp._tool_manager._tools),
    "refresh_capabilities": "refresh_capabilities" in mod.mcp._tool_manager._tools,
    "extension_states": {name: state
                         for (name, _inst), state in mod._extension_states.items()},
}))
"""


def test_main_loader_registers_module_and_activates_extensions(tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(FAKE_KUBECONFIG)

    env = dict(os.environ)
    env.update({
        "KUBECONFIG": str(kubeconfig),
        "KUBEARCHIVE_ENABLED": "false",
        "LUMINO_DISABLE_TELEMETRY": "1",
        "PYTHONHASHSEED": "0",
    })
    env.pop("LUMINO_PROFILE", None)
    env.pop("LUMINO_CONFIG", None)

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_CODE],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"main.load_server_module() failed in a fresh interpreter "
        f"(the production startup path):\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["tool_count"] == 49  # 43 baseline (incl. disconnect_cluster) + 6 phase-2c canonical aliases
    assert payload["refresh_capabilities"] is True
    assert payload["extension_states"] == {
        "konflux": "active", "openshift": "active", "tekton": "active",
    }
