"""Offline tests for the Workbench MCP server (no cluster, no network).

Run:  python -m pytest tests/ -v
"""

import asyncio
import json

import pytest

import server
from server import WorkbenchError


def call(fn, *args, **kwargs):
    """Invoke an async MCP tool synchronously (the decorator keeps the fn)."""
    return asyncio.run(fn(*args, **kwargs))


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


# -- workspaces --------------------------------------------------------------


def test_workspace_create_list_delete(root):
    call(server.workspace_create, "alpha")
    listing = json.loads(call(server.workspace_list))
    assert [w["workspace"] for w in listing] == ["alpha"]
    # duplicate refused
    with pytest.raises(WorkbenchError, match="already exists"):
        call(server.workspace_create, "alpha")
    # delete requires confirm
    with pytest.raises(WorkbenchError, match="confirm=true"):
        call(server.workspace_delete, "alpha")
    out = json.loads(call(server.workspace_delete, "alpha", confirm=True))
    assert out["deleted"] is True
    assert json.loads(call(server.workspace_list)) == []


def test_workspace_name_validation(root):
    with pytest.raises(WorkbenchError, match="invalid workspace name"):
        call(server.workspace_create, "../escape")
    with pytest.raises(WorkbenchError, match="does not exist"):
        call(server.write_file, "ghost", "f.txt", "x")


# -- files -------------------------------------------------------------------


def test_write_read_list_delete(root):
    call(server.workspace_create, "ws")
    out = json.loads(call(server.write_file, "ws", "src/app/main.py", "print('hi')\n"))
    assert out["bytes"] == 12
    read = json.loads(call(server.read_file, "ws", "src/app/main.py"))
    assert read["content"] == "print('hi')\n" and read["truncated"] is False
    listing = json.loads(call(server.list_files, "ws"))
    assert {e["path"] for e in listing["entries"]} == {"src", "src/app", "src/app/main.py"}


def test_path_traversal_blocked(root):
    call(server.workspace_create, "ws")
    for bad in ["../escape.txt", "/etc/passwd", "a/../../b", "sub/../../../x"]:
        with pytest.raises(WorkbenchError, match="escapes the workspace"):
            call(server.write_file, "ws", bad, "nope")
    # symlink escape also blocked
    import os

    os.symlink("/etc", str(root / "ws" / "sneaky"))
    with pytest.raises(WorkbenchError, match="escapes the workspace"):
        call(server.read_file, "ws", "sneaky/passwd")


def test_read_size_cap_and_binary(root):
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "big.txt", "x" * 100)
    read = json.loads(call(server.read_file, "ws", "big.txt", max_bytes=10))
    assert read["truncated"] is True and len(read["content"]) == 10
    (root / "ws" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(WorkbenchError, match="not valid UTF-8"):
        call(server.read_file, "ws", "blob.bin")


def test_delete_file_requires_confirm(root):
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "f.txt", "data")
    with pytest.raises(WorkbenchError, match="confirm=true"):
        call(server.delete_file, "ws", "f.txt")
    call(server.delete_file, "ws", "f.txt", confirm=True)
    with pytest.raises(WorkbenchError, match="no such file"):
        call(server.read_file, "ws", "f.txt")
    # workspace itself protected from file-level delete
    with pytest.raises(WorkbenchError, match="workspace_delete"):
        call(server.delete_file, "ws", ".", confirm=True)


# -- env ---------------------------------------------------------------------


def test_env_roundtrip_and_validation(root):
    call(server.workspace_create, "ws")
    call(server.set_env, "ws", "MODEL_NAME", "qwen-7b")
    got = json.loads(call(server.get_env, "ws", "MODEL_NAME"))
    assert got["value"] == "qwen-7b"
    with pytest.raises(WorkbenchError, match="invalid env key"):
        call(server.set_env, "ws", "lower-case", "x")
    with pytest.raises(WorkbenchError, match="not set"):
        call(server.get_env, "ws", "MISSING")


# -- run_command -------------------------------------------------------------


def test_run_command_allowlist_and_denylist(root, monkeypatch):
    call(server.workspace_create, "ws")
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls,python3")
    out = json.loads(call(server.run_command, "ws", ["ls", "-1"]))
    assert out["exit_code"] == 0 and "stderr" in out
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["bash", "-c", "echo hi"])
    monkeypatch.setenv("WORKBENCH_EXEC_DENYLIST", "curl")
    with pytest.raises(WorkbenchError, match="deny-listed"):
        call(server.run_command, "ws", ["curl", "http://example.com"])


def test_run_command_env_injection_and_timeout(root, monkeypatch):
    call(server.workspace_create, "ws")
    call(server.set_env, "ws", "GREETING", "hello")
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "python3")
    out = json.loads(call(server.run_command, "ws", ["python3", "-c", "import os; print(os.environ['GREETING'])"]))
    assert out["stdout"].strip() == "hello"
    out = json.loads(call(server.run_command, "ws", ["python3", "-c", "import time; time.sleep(5)"], 1))
    assert out["timed_out"] is True and out["exit_code"] == 124


def test_run_command_caps_output(root, monkeypatch):
    call(server.workspace_create, "ws")
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "python3")
    monkeypatch.setenv("WORKBENCH_MAX_OUTPUT_BYTES", "100")
    out = json.loads(call(server.run_command, "ws", ["python3", "-c", "print('x' * 1000)"]))
    assert len(out["stdout"]) <= 100 and out["truncated"] is True


def test_audit_log_written(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls")
    call(server.workspace_create, "ws")
    call(server.run_command, "ws", ["ls"])
    audit = (root / ".audit.jsonl").read_text().strip().splitlines()
    events = [json.loads(line)["event"] for line in audit]
    assert "workspace_create" in events and "run_command" in events
