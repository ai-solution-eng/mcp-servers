"""Pytest fixtures shared by the K8S-MCP adversarial suite.

Installs the kubernetes stubs BEFORE `server` is imported anywhere and
provides the module-under-test plus the fake-kubectl bin dir used by the
exec/VirtualServices pipeline checks.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pytest  # noqa: E402

from test_namespace_policy import _install_kubernetes_stubs, _pop_policy_envs  # noqa: E402


@pytest.fixture(scope="module")
def server():
    """The server module under test, imported with kubernetes stubbed."""
    _install_kubernetes_stubs()
    _pop_policy_envs()
    os.environ.pop("K8S_MCP_EXEC_ENABLED", None)
    import server as server_module

    yield server_module


@pytest.fixture(scope="session")
def fakebin(tmp_path_factory):
    """A directory holding a fake `kubectl` that echoes its argv.

    Putting it first on PATH keeps pipeline checks from touching a real
    cluster while still exercising the real subprocess path.
    """
    bin_dir = tmp_path_factory.mktemp("exec-fakebin")
    script = bin_dir / "kubectl"
    script.write_text("#!/bin/sh\nprintf 'ARGV:'; for a in \"$@\"; do printf ' [%s]' \"$a\"; done; printf '\\n'\n")
    script.chmod(0o755)
    return str(bin_dir)
