"""Offline tests for the Workbench MCP server (no cluster, no network).

Run:  python -m pytest tests/ -v
"""

import asyncio
import hashlib
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


def test_workspace_list_lazy_vs_deep(root, monkeypatch):
    """The MCP listing is lazy by default (no rglob over a PVC that may hold
    hundreds of thousands of NFS inodes): names only, null counts + marker.
    deep=true restores the exact pre-lazy behavior; keys are present in both
    modes."""
    call(server.workspace_create, "alpha")
    call(server.write_file, "alpha", "a.txt", "12345")  # 5 bytes

    lazy = json.loads(call(server.workspace_list))
    assert [w["workspace"] for w in lazy] == ["alpha"]
    assert lazy[0]["files"] is None and lazy[0]["bytes"] is None
    assert lazy[0]["counts"] == "lazy (pass deep=true)"
    assert set(lazy[0]) == {"workspace", "files", "bytes", "counts"}

    deep = json.loads(call(server.workspace_list, deep=True))
    assert [w["workspace"] for w in deep] == ["alpha"]
    assert deep[0]["files"] == 1 and deep[0]["bytes"] == 5
    assert set(deep[0]) == {"workspace", "files", "bytes"}
    assert "counts" not in deep[0]

    # webui console path: always deep (the UI renders files/bytes)
    from starlette.testclient import TestClient

    app = server._build_http_app()
    with TestClient(app) as c:
        data = c.get("/api/workspaces").json()
    assert data["workspaces"] == [{"workspace": "alpha", "files": 1, "bytes": 5}]

    # empty root: both modes return []
    monkeypatch.setenv("WORKBENCH_ROOT", str(root.parent / "missing-root"))
    assert json.loads(call(server.workspace_list)) == []
    assert json.loads(call(server.workspace_list, deep=True)) == []


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


def test_write_file_append_chunks_under_cumulative_cap(root, monkeypatch):
    """The "write in chunks" promise: append=true ADDS to the file (the old
    write always REPLACED it), and the byte cap is enforced CUMULATIVELY —
    an append never becomes a cap bypass."""
    monkeypatch.setenv("WORKBENCH_MAX_FILE_BYTES", "100")
    call(server.workspace_create, "ws")
    out = json.loads(call(server.write_file, "ws", "log.txt", "a" * 60))
    assert out == {"workspace": "ws", "path": "log.txt", "bytes": 60, "written": True}
    out = json.loads(call(server.write_file, "ws", "log.txt", "b" * 40, append=True))
    assert out["appended"] is True
    assert out["bytes"] == 40
    assert out["total_bytes"] == 100  # existing 60 + incoming 40 == exactly at cap
    assert (root / "ws" / "log.txt").read_text() == "a" * 60 + "b" * 40
    # cumulative over-cap refused: 60 + 41 > 100
    with pytest.raises(WorkbenchError, match="cap is 100"):
        call(server.write_file, "ws", "log.txt", "c" * 41, append=True)
    # a plain (non-append) write still honors the single-write cap
    with pytest.raises(WorkbenchError, match="cap is 100"):
        call(server.write_file, "ws", "log.txt", "d" * 101)
    # appending to the already-at-cap file: even 1 byte refuses
    with pytest.raises(WorkbenchError, match="cumulative"):
        call(server.write_file, "ws", "log.txt", "e", append=True)
    # appending to a NOT-yet-existing file creates it (and reports so)
    out = json.loads(call(server.write_file, "ws", "new.txt", "hello", append=True))
    assert out["appended"] is True and out["total_bytes"] == 5
    assert (root / "ws" / "new.txt").read_text() == "hello"
    # the chunked file reads back byte-identical
    read = json.loads(call(server.read_file, "ws", "log.txt"))
    assert read["content"] == "a" * 60 + "b" * 40 and read["truncated"] is False


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


# -- audit JSONL rotation -----------------------------------------------------


def test_audit_rotation_size_triggered(root, monkeypatch):
    """A trail grown past WORKBENCH_AUDIT_MAX_BYTES rotates on the next
    event: active file renamed to .1 (previous .1 overwritten), fresh file
    starts with an ``audit_rotated`` provenance entry carrying the sha256 of
    the rotated file's last line."""
    monkeypatch.setenv("WORKBENCH_AUDIT_MAX_BYTES", "400")
    # five ~80-byte events → ~400 bytes active (one workspace_create event
    # is ~80 bytes: ts + event + workspace)
    for i in range(5):
        call(server.workspace_create, f"ws{i}")
    assert (root / ".audit.jsonl").stat().st_size >= 400
    assert not (root / ".audit.jsonl.1").exists()  # not yet rotated
    call(server.workspace_create, "ws5")  # crosses the threshold -> rotate first
    assert not (root / ".audit.jsonl.2").exists()  # single generation only
    lines1 = (root / ".audit.jsonl.1").read_text().strip().splitlines()
    assert json.loads(lines1[-1])["event"] == "workspace_create"  # last create moved to .1
    lines_active = (root / ".audit.jsonl").read_text().strip().splitlines()
    head = json.loads(lines_active[0])
    assert head["event"] == "audit_rotated"
    assert head["rotated_to"] == ".audit.jsonl.1"
    # provenance: sha256 of the last line of the ROTATED file
    assert head["rotated_from"] == hashlib.sha256(lines1[-1].encode()).hexdigest()
    assert json.loads(lines_active[-1])["event"] == "workspace_create"  # ws5 create live


def test_audit_rotation_failure_keeps_appending(root, monkeypatch):
    """If the rename itself fails (e.g. a broken NFS mount), rotation is
    best-effort: the event must STILL land in the active file — audit
    events are never dropped on rotation failure."""
    monkeypatch.setenv("WORKBENCH_AUDIT_MAX_BYTES", "200")
    root.mkdir(parents=True, exist_ok=True)  # _audit is best-effort: no mkdir
    real_replace = server.os.replace

    def _blocked(src, dst):
        raise OSError("rename blocked (simulated)")

    monkeypatch.setattr(server.os, "replace", _blocked)
    server._audit({"event": "before_blocked_rotate", "payload": "x" * 150})
    server._audit({"event": "after_blocked_rotate"})
    monkeypatch.setattr(server.os, "replace", real_replace)

    assert not (root / ".audit.jsonl.1").exists()  # rotate never succeeded
    lines = (root / ".audit.jsonl").read_text().strip().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "before_blocked_rotate" in events
    assert "after_blocked_rotate" in events  # kept appending, nothing lost


def test_audit_rotation_overwrites_previous_generation(root, monkeypatch):
    """Rotation is single-generation: repeated rotations REPLACE .1 (no .2
    ever appears) and each new provenance references the CURRENT last line,
    so the trail never grows unbounded on a busy server."""
    monkeypatch.setenv("WORKBENCH_AUDIT_MAX_BYTES", "300")
    root.mkdir(parents=True, exist_ok=True)  # _audit is best-effort: no mkdir
    marker = "x" * 500  # comfortably > 300 bytes per event
    for _ in range(3):
        server._audit({"event": "bulk", "payload": marker})
        server._audit({"event": "next"})  # always crosses the threshold
    assert not (root / ".audit.jsonl.2").exists()
    lines1 = (root / ".audit.jsonl.1").read_text().strip().splitlines()
    # .1 holds the PREVIOUS generation: bulk events + the small trailing
    # events that fit after the last rotation (a rotated_from marker line)
    kinds = {json.loads(l)["event"] for l in lines1}
    assert "bulk" in kinds and kinds <= {"bulk", "next", "audit_rotated"}
    lines_active = (root / ".audit.jsonl").read_text().strip().splitlines()
    head = json.loads(lines_active[0])
    assert head["event"] == "audit_rotated"
    assert head["rotated_from"] == hashlib.sha256(lines1[-1].encode()).hexdigest()


def test_audit_rotation_provenance_chain(root, monkeypatch):
    """rotated_from bridges generations: sha256 of .1's final line == the new
    generation's rotated_from.  The ACTIVE path stays exactly .audit.jsonl —
    the D7 argv-confinement tests and the fleet logsearch-mount convention
    (mount the root, search .audit.jsonl and .audit.jsonl.1) keep working."""
    monkeypatch.setenv("WORKBENCH_AUDIT_MAX_BYTES", "300")
    root.mkdir(parents=True, exist_ok=True)  # _audit is best-effort: no mkdir
    server._audit({"event": "e1", "payload": "y" * 400})  # leaves file > 300
    server._audit({"event": "e2"})  # crosses the threshold -> rotate first
    prev = (root / ".audit.jsonl.1").read_text().strip().splitlines()
    head = json.loads((root / ".audit.jsonl").read_text().strip().splitlines()[0])
    assert head["rotated_from"] == hashlib.sha256(prev[-1].encode()).hexdigest()
    assert (root / ".audit.jsonl").is_file()


# -- exec concurrency bound --------------------------------------------------


def test_run_command_busy_response_when_pool_saturated(root, monkeypatch):
    """All workers busy → structured busy response WITHOUT executing (no
    queue, no reordering), and the busy outcome is audit-logged.  File/env
    tools are untouched (they stay on the default executor)."""
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "sleep,cp")
    monkeypatch.setenv("WORKBENCH_EXEC_MAX_CONCURRENCY", "1")
    server._EXEC_POOL = None  # reset the lazy pool so the new width applies
    server._EXEC_INFLIGHT = 0

    async def scenario():
        # saturate the width-1 pool with a long sleep
        first = asyncio.ensure_future(server.run_command("ws", ["sleep", "2.0"]))
        for _ in range(500):
            if server._EXEC_INFLIGHT:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("run_command never went in-flight")

        # second call: pool full → structured busy response, not executed
        busy = json.loads(await server.run_command("ws", ["cp", "next.txt", "sentinel"]))
        assert busy == {"ok": False, "busy": True, "running": 1, "hint": "retry shortly"}
        assert (root / "ws" / "sentinel").read_text() == "0"  # no queue: not run

        # once the pool drains, commands execute again
        await first
        assert server._EXEC_INFLIGHT == 0
        after = json.loads(await server.run_command("ws", ["cp", "next.txt", "sentinel"]))
        assert after["exit_code"] == 0 and "busy" not in after
        assert (root / "ws" / "sentinel").read_text() == "ran"

    call(server.workspace_create, "ws")
    (root / "ws" / "sentinel").write_text("0")
    (root / "ws" / "next.txt").write_text("ran")
    asyncio.run(scenario())
    audit = (root / ".audit.jsonl").read_text().strip().splitlines()
    events = [json.loads(line)["event"] for line in audit]
    assert "run_command_busy" in events
    # exactly two real runs (sleep + drain touch) and one busy non-run
    assert events.count("run_command") == 2
    assert events.count("run_command_busy") == 1


holder: dict = {}


def test_run_command_env_knob_width_rebuilds_pool(root, monkeypatch):
    """WORKBENCH_EXEC_MAX_CONCURRENCY is re-read per call: a changed width
    rebuilds the dedicated pool (and never misreports running counts)."""
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls")
    monkeypatch.setenv("WORKBENCH_EXEC_MAX_CONCURRENCY", "3")
    server._EXEC_POOL = None
    server._EXEC_INFLIGHT = 0
    call(server.workspace_create, "ws")
    call(server.run_command, "ws", ["ls"])
    assert server._EXEC_WIDTH == 3
    monkeypatch.setenv("WORKBENCH_EXEC_MAX_CONCURRENCY", "2")
    call(server.run_command, "ws", ["ls"])
    assert server._EXEC_WIDTH == 2  # rebuilt on the width change
    assert server._EXEC_POOL._max_workers == 2
    # width-0 / negative clamp to 1 rather than a dead pool
    monkeypatch.setenv("WORKBENCH_EXEC_MAX_CONCURRENCY", "0")
    call(server.run_command, "ws", ["ls"])
    assert server._EXEC_WIDTH == 1
    server._EXEC_POOL = None
    server._EXEC_INFLIGHT = 0
