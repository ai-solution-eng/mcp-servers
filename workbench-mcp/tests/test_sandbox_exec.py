"""Sandbox-exec v1 tests — no-network python execution pods behind sandbox_run.

Covers the three layers of the feature (ADDITIVE, DEFAULT OFF everywhere):

1. Fence + switch (server): the tool is registered ALWAYS (fleet
   refusals-are-answers doctrine) but refuses self-describingly when
   WORKBENCH_SANDBOX_EXEC is off; the file fence uses the SAME helpers as
   read_file/write_file (traversal/symlink escapes, missing file, the
   WORKBENCH_MAX_FILE_BYTES cap) with refusal shapes identical to the
   siblings.
2. Executor selection + exec (server): the kubernetes client is LAZY and the
   seam is the module-level ``_k8s_list_pods`` / ``_k8s_exec_stream``
   functions — tests stub THOSE (the repo's seam-fake pattern), never the
   kubernetes internals.  Least-loaded pick under the in-flight map, the
   structured busy response without executing, timeout → structured result
   with the map decremented, exception → map decremented (try/finally),
   403 → self-describing RBAC error, output-cap truncation marker.
3. Chart (helm): default values render NO executor resources (byte-identical
   default — asserted against a fresh render); enabled render HAS the
   hardened Deployment (automount off, no tolerations key, control-plane
   DoesNotExist affinity, no GPU keys anywhere, read-only rootfs, caps
   dropped, resources, spread constraint), the deny-all NetworkPolicy
   (policyTypes both, zero rules) and the same-namespace exec Role/RoleBinding.

Helm renders run through the real ``helm template`` binary (skipped when helm
is absent) — the same discipline as the pre/post release checks.

Run:  cd mcp_servers/workbench_mcp && SQLhandler/.venv312/bin/python -m pytest tests/test_sandbox_exec.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

import server
from server import WorkbenchError

HELM_DIR = Path(__file__).resolve().parent.parent / "helm"

EXEC_SCRIPT = "import sys\nprint('hello', sys.argv[1:])\n"


def call(fn, *args, **kwargs):
    """Invoke an async MCP tool synchronously (the decorator keeps the fn)."""
    return asyncio.run(fn(*args, **kwargs))


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def sandbox_off(monkeypatch):
    """The default posture: WORKBENCH_SANDBOX_EXEC unset (off) unless a test
    opts in — mirrors the chart default (sandboxExec: false)."""
    monkeypatch.delenv("WORKBENCH_SANDBOX_EXEC", raising=False)
    monkeypatch.delenv("WORKBENCH_SANDBOX_LABEL", raising=False)
    monkeypatch.delenv("WORKBENCH_SANDBOX_CONTAINER", raising=False)
    monkeypatch.delenv("WORKBENCH_SANDBOX_TIMEOUT_S", raising=False)
    monkeypatch.delenv("WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS", raising=False)
    monkeypatch.delenv("RELEASE_NAMESPACE", raising=False)


@pytest.fixture(autouse=True)
def clean_inflight():
    """A pristine in-flight map per test (module state otherwise leaks)."""
    with server._SANDBOX_LOCK:
        server._SANDBOX_INFLIGHT.clear()
    yield
    with server._SANDBOX_LOCK:
        server._SANDBOX_INFLIGHT.clear()


def audit_events(root):
    lines = (root / ".audit.jsonl").read_text().strip().splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# 1 · fence + switch
# ---------------------------------------------------------------------------


def test_sandbox_run_registered_but_refuses_when_disabled(root):
    """The tool exists in the served tool list even when disabled (fleet
    refusals-are-answers doctrine) — the call refuses self-describingly and
    names the env knob and the chart values key."""
    tools = asyncio.run(server.mcp.list_tools())
    assert "sandbox_run" in {t.name for t in tools}
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="WORKBENCH_SANDBOX_EXEC") as exc:
        call(server.sandbox_run, "ws", "s.py")
    assert "executors.enabled" in str(exc.value)
    # audited as a refusal
    assert any(e["event"] == "sandbox_run" and e["outcome"] == "refused" for e in audit_events(root))


def test_sandbox_run_enabled_but_no_pods_is_self_describing(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: [])
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="no executor pods Ready") as exc:
        call(server.sandbox_run, "ws", "s.py")
    assert "executors.enabled" in str(exc.value)
    assert any(e["event"] == "sandbox_run" and e["outcome"] == "error" for e in audit_events(root))


def test_sandbox_run_disabled_error_beats_discovery(root, monkeypatch):
    """The switch gates FIRST: with the env off, discovery is never reached
    (the executor stub would explode if called)."""

    def _boom(label, ns):
        raise AssertionError("discovery must not run while the switch is off")

    monkeypatch.setattr(server, "_k8s_list_pods", _boom)
    call(server.workspace_create, "ws")
    with pytest.raises(WorkbenchError, match="WORKBENCH_SANDBOX_EXEC"):
        call(server.sandbox_run, "ws", "s.py")


def test_sandbox_path_traversal_refused(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["p1"])
    call(server.workspace_create, "ws")
    for bad in ["../escape.py", "/etc/passwd", "sub/../../s.py"]:
        with pytest.raises(WorkbenchError, match="escapes the workspace"):
            call(server.sandbox_run, "ws", bad)
    # symlink escape too (the read_file refusal shape)
    os.symlink("/etc", str(root / "ws" / "sneaky"))
    with pytest.raises(WorkbenchError, match="escapes the workspace"):
        call(server.sandbox_run, "ws", "sneaky/passwd")


def test_sandbox_missing_file_refused(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    call(server.workspace_create, "ws")
    with pytest.raises(WorkbenchError, match="no such file"):
        call(server.sandbox_run, "ws", "ghost.py")


def test_sandbox_oversized_script_refused(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setenv("WORKBENCH_MAX_FILE_BYTES", "64")
    call(server.workspace_create, "ws")
    # The write cap cannot be bypassed by write_file either — plant the
    # oversized file directly (the sandbox fence is what is under test).
    big = root / "ws" / "big.py"
    big.write_text("x" * 100)
    with pytest.raises(WorkbenchError, match="cap is 64") as exc:
        call(server.sandbox_run, "ws", "big.py")
    assert "single file" in str(exc.value)


def test_sandbox_argv_non_list_refused(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["p1"])
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="argv must be a list"):
        call(server.sandbox_run, "ws", "s.py", argv="not-a-list")  # type: ignore[arg-type]
    with pytest.raises(WorkbenchError, match="argv must be a list"):
        call(server.sandbox_run, "ws", "s.py", argv=["-v", 7])  # type: ignore[list-item]


def test_sandbox_argv_nul_or_empty_token_refused(root, monkeypatch):
    """Fleet k8s-mcp exec screen: NUL/empty tokens would fail OPAQUELY at the
    API server — refuse self-describingly and keep them out of the audit."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["p1"])
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="no NUL bytes"):
        call(server.sandbox_run, "ws", "s.py", argv=["a\x00b"])
    with pytest.raises(WorkbenchError, match="non-empty"):
        call(server.sandbox_run, "ws", "s.py", argv=[""])
    audit = (root / ".audit.jsonl").read_text()
    assert "\x00" not in audit  # unscreenable token never reached the audit


def test_sandbox_argv_size_cap_refused(root, monkeypatch):
    """The whole argv rides one exec launch request and lands verbatim in the
    audit line — total-argv-bytes is capped with a self-describing refusal."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["p1"])
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    big = "x" * 9000
    with pytest.raises(WorkbenchError, match="argv is .* bytes; cap is"):
        call(server.sandbox_run, "ws", "s.py", argv=[big])
    assert "x" * 9000 not in (root / ".audit.jsonl").read_text()


def test_sandbox_output_cap_never_fully_disabled(root, monkeypatch):
    """Degenerate cap env values clamp to >= 1 — truncation cannot be turned
    off into an unbounded stream (or an empty-only marker stream)."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS", "0")
    assert server._sandbox_max_output_chars() >= 1
    text, cut = server._sandbox_truncate("hello world", 0)
    assert cut is True and text.endswith("truncated") or "truncated" in text


def test_sandbox_output_strips_nul_bytes(root, monkeypatch):
    """Fleet k8s-mcp consistency: NUL bytes are stripped from exec output
    streams (ANSI/control bytes pass honestly)."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")

    def fake_stream(pod, ns, container, argv, script, timeout_s):
        return 0, b"before\x00after", b"err\x00tail"

    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["p1"])
    monkeypatch.setattr(server, "_k8s_exec_stream", fake_stream)
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    out = json.loads(call(server.sandbox_run, "ws", "s.py"))
    assert out["stdout"] == "beforeafter"
    assert out["stderr"] == "errtail"


def test_sandbox_discovery_failure_self_describing(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")

    def _boom(label, ns):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server, "_k8s_list_pods", _boom)
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="executor discovery failed"):
        call(server.sandbox_run, "ws", "s.py")
    assert any(e["event"] == "sandbox_run" and e["outcome"] == "error" for e in audit_events(root))


def test_sandbox_env_knobs_read_per_call(root, monkeypatch):
    """Label/container/timeout knobs re-read per call (the fleet env
    re-read pattern); the timeout clamps into 1..600."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_LABEL", "app=custom-exec")
    monkeypatch.setenv("WORKBENCH_SANDBOX_CONTAINER", "pybox")
    monkeypatch.setenv("WORKBENCH_SANDBOX_TIMEOUT_S", "9")
    assert server._sandbox_label() == "app=custom-exec"
    assert server._sandbox_container() == "pybox"
    assert server._sandbox_timeout_s() == 9
    monkeypatch.setenv("WORKBENCH_SANDBOX_TIMEOUT_S", "0")
    assert server._sandbox_timeout_s() == 1
    monkeypatch.setenv("WORKBENCH_SANDBOX_TIMEOUT_S", "100000")
    assert server._sandbox_timeout_s() == 600
    monkeypatch.setenv("WORKBENCH_SANDBOX_TIMEOUT_S", "garbage")
    assert server._sandbox_timeout_s() == 120  # default on parse failure


def test_sandbox_namespace_resolution(monkeypatch):
    """Release namespace env wins outside a pod; empty → 'default'."""
    monkeypatch.setenv("RELEASE_NAMESPACE", "wb-ns")
    assert server._sandbox_namespace() == "wb-ns"
    monkeypatch.delenv("RELEASE_NAMESPACE")
    assert server._sandbox_namespace() == "default"


# ---------------------------------------------------------------------------
# 2 · selection + exec (seam fakes over _k8s_list_pods / _k8s_exec_stream)
# ---------------------------------------------------------------------------


class FakeStream:
    """The seam fake for _k8s_exec_stream: records the argv/script/timeout it
    was called with and returns canned (exit_code, stdout, stderr).  (The
    ``calls`` class attribute exists for ad-hoc inspection; the tests use
    their own local recorder — see dispatch_recorder.)"""

    calls: ClassVar[list] = []

    def __init__(self, *, exit_code=0, stdout=b"", stderr=b"", exc=None):
        self.result = (exit_code, stdout, stderr)
        self.exc = exc

    def __call__(self, pod, namespace, container, argv, script, timeout_s):
        type(self).calls.append(
            {
                "pod": pod,
                "namespace": namespace,
                "container": container,
                "argv": argv,
                "script": script,
                "timeout_s": timeout_s,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture()
def dispatch_recorder(monkeypatch):
    """Ready-pod discovery stub + FakeStream wiring; returns (calls, pods)."""
    calls: list = []

    def _record(pod, namespace, container, argv, script, timeout_s):
        calls.append(
            {
                "pod": pod,
                "namespace": namespace,
                "container": container,
                "argv": argv,
                "script": script,
                "timeout_s": timeout_s,
            }
        )
        return (0, b"out", b"")

    return calls, _record


def test_sandbox_success_shape_and_dispatch(root, monkeypatch):
    """Happy path: ok/exit_code/stdout/stderr/truncated/pod/elapsed_s, and
    the dispatch ran `python3 -I -- *argv` with the file's bytes on stdin
    against the least-loaded Ready pod."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setenv("RELEASE_NAMESPACE", "wb-ns")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a", "exec-b"])
    calls: list = []

    def fake_exec(pod, namespace, container, argv, script, timeout_s):
        calls.append({"pod": pod, "namespace": namespace, "container": container, "argv": argv, "script": script, "timeout_s": timeout_s})
        return (0, b"hello ['-v']\n", b"warning: deprecated\n")

    monkeypatch.setattr(server, "_k8s_exec_stream", fake_exec)
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    out = json.loads(call(server.sandbox_run, "ws", "s.py", ["-v"]))
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert out["stdout"] == "hello ['-v']\n"
    assert out["stderr"] == "warning: deprecated\n"
    assert out["truncated"] is False
    assert out["pod"] in {"exec-a", "exec-b"}
    assert isinstance(out["elapsed_s"], float)
    (d,) = calls
    assert d["namespace"] == "wb-ns"
    assert d["container"] == "python"
    assert d["argv"] == ["python3", "-I", "--", "-v"]
    assert d["script"] == EXEC_SCRIPT.encode()
    assert d["timeout_s"] == 120
    # success is audited with the pod name
    ok_events = [e for e in audit_events(root) if e["event"] == "sandbox_run" and e["outcome"] == "ok"]
    assert ok_events and ok_events[-1]["pod"] == out["pod"]
    # the in-flight map drained to empty (try/finally released the slot)
    assert server._SANDBOX_INFLIGHT == {}


def test_sandbox_exit_code_null_when_protocol_silent(root, monkeypatch):
    """A stream that yields no terminal status reports exit_code honestly as
    null (never invented)."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a"])
    monkeypatch.setattr(server, "_k8s_exec_stream", lambda *a: (None, b"out", b""))
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    out = json.loads(call(server.sandbox_run, "ws", "s.py"))
    assert out["ok"] is True and out["exit_code"] is None


def test_sandbox_least_loaded_pick(root, monkeypatch):
    """Selection is deterministic least-loaded: a pod carrying an in-flight
    run is skipped while a zero-loaded peer exists."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a", "exec-b"])
    with server._SANDBOX_LOCK:
        server._SANDBOX_INFLIGHT["exec-a"] = 1
    assert server._sandbox_select_pod(["exec-a", "exec-b"]) == "exec-b"
    # zero-loaded tie-break is deterministic (sorted)
    with server._SANDBOX_LOCK:
        server._SANDBOX_INFLIGHT.clear()
    assert server._sandbox_select_pod(["exec-b", "exec-a"]) == "exec-a"
    assert server._SANDBOX_INFLIGHT == {"exec-a": 1}  # the pick incremented
    server._sandbox_release_pod("exec-a")
    assert server._SANDBOX_INFLIGHT == {}


def test_sandbox_busy_response_without_exec(root, monkeypatch):
    """Every Ready pod already carries >= 1 run → the structured busy
    response, NO exec attempted (asserted via the stubbed stream fn), audited
    as sandbox_run_busy."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a", "exec-b"])

    def _must_not_exec(*a, **k):
        raise AssertionError("exec must not be attempted when all pods are busy")

    monkeypatch.setattr(server, "_k8s_exec_stream", _must_not_exec)
    with server._SANDBOX_LOCK:
        server._SANDBOX_INFLIGHT.update({"exec-a": 1, "exec-b": 1})
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    out = json.loads(call(server.sandbox_run, "ws", "s.py"))
    assert out == {"ok": False, "busy": True, "running": 2, "hint": "retry shortly"}
    assert any(e["event"] == "sandbox_run_busy" for e in audit_events(root))
    # the busy path consumed no slot of its own
    with server._SANDBOX_LOCK:
        assert server._SANDBOX_INFLIGHT == {"exec-a": 1, "exec-b": 1}


def test_sandbox_timeout_structured_and_released(root, monkeypatch):
    """A timed-out exec returns the structured timeout result (with any
    partial output), and the in-flight map is decremented (try/finally)."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setenv("WORKBENCH_SANDBOX_TIMEOUT_S", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a"])

    def slow_exec(pod, namespace, container, argv, script, timeout_s):
        assert timeout_s == 1
        raise server._SandboxExecTimeout(timeout_s, b"partial-out", b"partial-err")

    monkeypatch.setattr(server, "_k8s_exec_stream", slow_exec)
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    out = json.loads(call(server.sandbox_run, "ws", "s.py"))
    assert out["ok"] is False and out["timeout"] is True
    assert out["timeout_s"] == 1
    assert out["stdout"] == "partial-out" and out["stderr"] == "partial-err"
    assert out["pod"] == "exec-a"
    assert server._SANDBOX_INFLIGHT == {}  # released on the timeout path
    outcomes = [e["outcome"] for e in audit_events(root) if e["event"] == "sandbox_run"]
    assert "timeout" in outcomes


def test_sandbox_exception_releases_slot_and_audits(root, monkeypatch):
    """An unexpected exec exception → self-describing error and the in-flight
    slot is released (the try/finally), with the outcome audited."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a"])

    def boom(*a, **k):
        raise RuntimeError("websocket exploded")

    monkeypatch.setattr(server, "_k8s_exec_stream", boom)
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="sandbox exec failed"):
        call(server.sandbox_run, "ws", "s.py")
    assert server._SANDBOX_INFLIGHT == {}
    outcomes = [e["outcome"] for e in audit_events(root) if e["event"] == "sandbox_run"]
    assert "error" in outcomes


def test_sandbox_rbac_403_self_describing(root, monkeypatch):
    """A 403 from the exec seam surfaces the missing-Role story (the k8s-mcp
    auth can-i lesson: a 'no' is a result), with the `auth can-i` recipe."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setenv("RELEASE_NAMESPACE", "wb-ns")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a"])
    monkeypatch.setattr(
        server,
        "_k8s_exec_stream",
        lambda *a, **k: (_ for _ in ()).throw(server._SandboxRbacError("the API server refused the exec into wb-ns/exec-a (403): Forbidden")),
    )
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    with pytest.raises(WorkbenchError, match="RBAC") as exc:
        call(server.sandbox_run, "ws", "s.py")
    msg = str(exc.value)
    assert "pods/exec" in msg and "auth can-i" in msg
    assert "executors.enabled" in msg
    assert any(e["outcome"] == "refused" for e in audit_events(root) if e["event"] == "sandbox_run")


def test_sandbox_output_cap_truncation_marker(root, monkeypatch):
    """Each stream is capped at WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS with the
    fleet's explicit ' ...[truncated N chars]' marker."""
    monkeypatch.setenv("WORKBENCH_SANDBOX_EXEC", "1")
    monkeypatch.setenv("WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS", "10")
    monkeypatch.setattr(server, "_k8s_list_pods", lambda label, ns: ["exec-a"])
    monkeypatch.setattr(server, "_k8s_exec_stream", lambda *a: (0, b"0123456789ABCDEF", b"e" * 15))
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "s.py", EXEC_SCRIPT)
    out = json.loads(call(server.sandbox_run, "ws", "s.py"))
    assert out["stdout"] == "0123456789 ...[truncated 6 chars]"
    assert out["stderr"] == "eeeeeeeeee ...[truncated 5 chars]"
    assert out["truncated"] is True


def test_sandbox_truncation_marker_unit():
    assert server._sandbox_truncate("short", 10) == ("short", False)
    out, cut = server._sandbox_truncate("0123456789ABCDEF", 10)
    assert cut is True and out == "0123456789 ...[truncated 6 chars]"


def test_sandbox_select_pod_zero_ready_raises():
    with pytest.raises(WorkbenchError, match="no executor pods Ready"):
        server._sandbox_select_pod([])


def test_sandbox_real_exec_stream_requires_kubernetes_client(monkeypatch):
    """The REAL seam implementations degrade self-describingly without the
    kubernetes client (the stdio-dev posture) — no crash, a named error."""
    import builtins

    real_import = builtins.__import__

    def _no_kubernetes(name, *a, **k):
        if name.startswith("kubernetes"):
            raise ImportError("No module named 'kubernetes'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_kubernetes)
    with pytest.raises(WorkbenchError, match="kubernetes client is not installed"):
        server._k8s_list_pods("app=workbench-exec", "default")
    with pytest.raises(WorkbenchError, match="kubernetes client is not installed"):
        server._k8s_exec_stream("p", "default", "python", ["python3", "-I"], b"x", 5)


# ---------------------------------------------------------------------------
# 3 · chart (helm template assertions, the test_auth.py wave style)
# ---------------------------------------------------------------------------


def _helm_available() -> bool:
    return shutil.which("helm") is not None


def _render(monkeypatch, tmp_path: Path, *set_args: str) -> str:
    assert _helm_available(), "helm binary required for chart tests"
    out = tmp_path / "render.yaml"
    proc = subprocess.run(
        ["helm", "template", "test", str(HELM_DIR), *set_args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    out.write_text(proc.stdout, encoding="utf-8")
    return proc.stdout


def _docs(yaml_text: str):
    import yaml

    return [d for d in yaml.safe_load_all(yaml_text) if d]


def test_helm_default_renders_no_executor_resources(monkeypatch, tmp_path):
    """Default values: NO workbench-exec Deployment/NetPol/Role — the default
    render contains only the baseline kinds (PVC, Service, Deployment) and no
    object is named *-exec / *-exec-netpol."""
    text = _render(monkeypatch, tmp_path)
    docs = _docs(text)
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == ["Deployment", "PersistentVolumeClaim", "Service"]
    assert all(not d["metadata"]["name"].endswith("-exec") for d in docs)
    assert "workbench-exec" not in text.replace("# ", "")  # not even a default-on env
    assert "WORKBENCH_SANDBOX_EXEC" not in text  # the tool switch is default-off too


def test_helm_enabled_render_has_hardened_executor(monkeypatch, tmp_path):
    text = _render(monkeypatch, tmp_path, "--set", "executors.enabled=true")
    docs = _docs(text)
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-exec"))
    spec = dep["spec"]["template"]["spec"]
    # pod hardening
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 10001
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    # NO tolerations block AT ALL (control-plane taints self-exclude)
    assert "tolerations" not in spec
    # workers only, expressed via the control-plane DoesNotExist affinity
    terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    exprs = terms[0]["matchExpressions"]
    assert any(
        e["key"] == "node-role.kubernetes.io/control-plane" and e["operator"] == "DoesNotExist" for e in exprs
    )
    # topology spread over the hostname
    spread = spec["topologySpreadConstraints"]
    assert spread[0]["topologyKey"] == "kubernetes.io/hostname"
    assert spread[0]["maxSkew"] == 1
    assert spread[0]["whenUnsatisfiable"] == "ScheduleAnyway"
    # container hardening
    (c,) = spec["containers"]
    assert c["name"] == "python"
    assert c["command"] == ["python3", "-c", "import time; time.sleep(2147483647)"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["requests"] == {"cpu": "100m", "memory": "128Mi"}
    assert c["resources"]["limits"] == {"cpu": "1", "memory": "512Mi"}
    # python's writable temp: a per-pod emptyDir at /tmp
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in spec["volumes"])
    assert any(m["name"] == "tmp" and m["mountPath"] == "/tmp" for m in c["volumeMounts"])
    # no GPU keys ANYWHERE in the rendered yaml (structure, not comments)
    rendered_code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "nvidia.com/gpu" not in rendered_code


def test_helm_enabled_render_deny_all_netpol(monkeypatch, tmp_path):
    text = _render(monkeypatch, tmp_path, "--set", "executors.enabled=true")
    docs = _docs(text)
    np = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-exec-netpol"))
    assert np["spec"]["podSelector"]["matchLabels"] == {"app": "workbench-exec"}
    assert sorted(np["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert "ingress" not in np["spec"], "deny-all: no ingress rules at all"
    assert "egress" not in np["spec"], "deny-all: no egress rules at all"


def test_helm_enabled_render_exec_rbac_same_namespace(monkeypatch, tmp_path):
    text = _render(monkeypatch, tmp_path, "--set", "executors.enabled=true")
    docs = _docs(text)
    role = next(d for d in docs if d["kind"] == "Role")
    binding = next(d for d in docs if d["kind"] == "RoleBinding")
    rules = role["rules"]
    pods_rule = next(r for r in rules if "pods" in r["resources"])
    exec_rule = next(r for r in rules if "pods/exec" in r["resources"])
    assert pods_rule["verbs"] == ["get", "list"]
    assert exec_rule["verbs"] == ["create"]
    assert binding["roleRef"]["kind"] == "Role" and binding["roleRef"]["name"] == role["metadata"]["name"]
    (subject,) = binding["subjects"]
    assert subject["kind"] == "ServiceAccount"
    assert subject["namespace"] == binding["metadata"]["namespace"]


def test_helm_enabled_render_replicas_and_hpa(monkeypatch, tmp_path):
    text = _render(monkeypatch, tmp_path, "--set", "executors.enabled=true", "--set", "executors.replicas=3")
    dep = next(d for d in _docs(text) if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-exec"))
    assert dep["spec"]["replicas"] == 3
    assert not any(d["kind"] == "HorizontalPodAutoscaler" for d in _docs(text))  # autoscaling default off
    text2 = _render(
        monkeypatch,
        tmp_path,
        "--set",
        "executors.enabled=true",
        "--set",
        "executors.autoscaling.enabled=true",
    )
    hpa = next(d for d in _docs(text2) if d["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["minReplicas"] == 2
    assert hpa["spec"]["maxReplicas"] == 4
    assert hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70


def test_helm_sandbox_exec_env_wiring(monkeypatch, tmp_path):
    """The server switch renders ONLY when workbench.sandboxExec is true; the
    executor image defaults to the workbench image reference."""
    base = _render(monkeypatch, tmp_path, "--set", "executors.enabled=true")
    assert "WORKBENCH_SANDBOX_EXEC" not in base  # switch still off
    dep = next(d for d in _docs(base) if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-exec"))
    (c,) = dep["spec"]["template"]["spec"]["containers"]
    repo = next(d for d in _docs(base) if d["kind"] == "Deployment" and not d["metadata"]["name"].endswith("-exec"))
    server_image = repo["spec"]["template"]["spec"]["containers"][0]["image"]
    assert c["image"] == server_image, "executors default to the workbench image (one image, two commands)"
    on = _render(monkeypatch, tmp_path, "--set", "executors.enabled=true", "--set", "workbench.sandboxExec=true")
    env_lines = [l.strip() for l in on.splitlines() if l.strip().startswith("- name: WORKBENCH_SANDBOX")]
    assert "- name: WORKBENCH_SANDBOX_EXEC" in env_lines
    exec_env = [l.strip() for l in on.splitlines() if l.strip() == 'value: "1"']
    assert exec_env, "the switch must render as the literal \"1\""


def test_values_yaml_executors_default_off():
    """The values file pins the posture: executors disabled, autoscaling
    disabled, and the example block stays commented."""
    vals = HELM_DIR / "values.yaml"
    text = vals.read_text(encoding="utf-8")
    m = __import__("re").search(r"(?m)^executors:\n  enabled: (false|true)\n", text)
    assert m and m.group(1) == "false", "executors must default OFF (byte-identical default render)"
    m2 = __import__("re").search(r"(?m)^  autoscaling:\n    enabled: (false|true)\n", text)
    assert m2 and m2.group(1) == "false"
