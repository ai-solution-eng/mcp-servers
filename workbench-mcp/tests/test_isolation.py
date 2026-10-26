"""Adversarial tests for workbench hardening (fleet decision D7 + PATH erosion
+ read_file streaming).

Covers the audit's confirmed cross-workspace exfiltration (P0-1's non-auth
half): any workspace's commands could read every other workspace's env values,
files, and the fleet-audit JSONL because run_command confined only cwd.  These
tests attempt those attacks through the SAME core functions the MCP tools and
the web UI use, and must see them refused with a clear error naming the
WORKBENCH_SHARED_PATHS escape hatch.

Run:  python -m pytest tests/test_isolation.py -v
"""

import asyncio
import json
import os
import pathlib
import time

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


@pytest.fixture()
def allowexec(monkeypatch):
    """Pin a permissive-but-benign allowlist for command-running tests."""
    monkeypatch.setenv(
        "WORKBENCH_EXEC_ALLOWLIST", "cat,ls,head,tail,grep,cp,mv,touch,mkdir,python3,find,tar,diff,sort,uniq,wc,du"
    )


def _two_workspaces(root):
    call(server.workspace_create, "wsA")
    call(server.workspace_create, "wsB")
    call(server.write_file, "wsA", "secret.txt", "TOPSECRET-ENV-AND-FILES")
    call(server.set_env, "wsA", "FLEET_TOKEN", "tok-abc123")
    return root / "wsA", root / "wsB"


# ---------------------------------------------------------------------------
# D7: cross-workspace reads/writes through run_command argv are refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["cat", "../wsA/secret.txt"],  # relative traversal
        ["cat", "XROOTX/wsA/secret.txt"],  # absolute path
        ["head", "-c", "4", "../wsA/secret.txt"],
        ["grep", "TOPSECRET", "../wsA/secret.txt"],
        ["cp", "../wsA/secret.txt", "stolen.txt"],  # cross-workspace copy-in
        ["mv", "a.txt", "../wsA/"],  # cross-workspace write
        ["mkdir", "../wsA/evil"],  # cross-workspace mkdir
        ["touch", "../wsA/pwned"],  # cross-workspace touch
        ["python3", "-c", "print(open('../wsA/secret.txt').read())"],  # embedded ../
        ["python3", "-c", "print(open('XROOTX/wsA/secret.txt').read())"],  # embedded absolute
        ["tar", "-czf", "out.tar.gz", "../wsA"],  # archive another workspace
        ["ls", ".."],  # bare .. (lists all workspaces)
        ["cat", ".."],  # bare .. variant
    ],
)
def test_run_command_cannot_reach_another_workspace(root, allowexec, monkeypatch, argv):
    _two_workspaces(root)  # workspaces must exist; addressed by name below
    argv = [a.replace("XROOTX", str(root)) for a in argv]
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "wsB", argv)
    # nothing landed in wsB and wsA's secret is untouched
    assert not (root / "wsB" / "stolen.txt").exists()
    assert "TOPSECRET" in json.loads(call(server.read_file, "wsA", "secret.txt"))["content"]


def test_run_command_refusal_is_audited(root, allowexec):
    _two_workspaces(root)
    with pytest.raises(WorkbenchError):
        call(server.run_command, "wsB", ["cat", "../wsA/secret.txt"])
    events = [json.loads(line)["event"] for line in (root / ".audit.jsonl").read_text().strip().splitlines()]
    assert "run_command_refused" in events


def test_audit_jsonl_is_denied_to_commands(root, allowexec):
    """The fleet-audit JSONL at the root is reachable by NO workspace."""
    call(server.workspace_create, "ws")
    call(server.run_command, "ws", ["cat", "x-nonexistent"])  # seeds an audit entry
    for ref in ["../.audit.jsonl", f"{root}/.audit.jsonl", f"{root}/./.audit.jsonl"]:
        with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
            call(server.run_command, "ws", ["cat", ref])
    # ...and not writable either
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "ws", ["touch", "../.audit.jsonl"])


def test_production_shape_root_refs_are_screened(monkeypatch):
    """Unit check of the production geometry (WORKBENCH_ROOT=/data, where the
    tmp-root tests above cannot go): /data/.audit.jsonl and other workspaces
    resolve as escape targets; system paths outside /data stay untouched."""
    monkeypatch.setenv("WORKBENCH_ROOT", "/data")
    ws = pathlib.Path("/data/wsA")
    assert server._argv_escape_target(ws, "/data/.audit.jsonl") == pathlib.Path("/data/.audit.jsonl")
    assert server._argv_escape_target(ws, "../.audit.jsonl") == pathlib.Path("/data/.audit.jsonl")
    assert server._argv_escape_target(ws, "/data/wsB/.workbench-env.json") == pathlib.Path(
        "/data/wsB/.workbench-env.json"
    )
    # not workbench space: untouched by the policy
    assert server._argv_escape_target(ws, "/etc/passwd") is None
    assert server._argv_escape_target(ws, "/usr/share/dict/words") is None
    # inside the addressed workspace: fine
    assert server._argv_escape_target(ws, "sub/file.txt") is None
    assert server._argv_escape_target(ws, "/data/wsA/sub/file.txt") is None


def test_cross_workspace_env_is_invisible(root, allowexec):
    """wsB's commands neither read wsA's env file nor inherit its values."""
    _two_workspaces(root)
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "wsB", ["cat", "../wsA/.workbench-env.json"])
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "wsB", ["cat", f"{root}/wsA/.workbench-env.json"])
    out = json.loads(
        call(server.run_command, "wsB", ["python3", "-c", "import os; print(os.environ.get('FLEET_TOKEN', 'absent'))"])
    )
    assert out["stdout"].strip() == "absent"
    # the env TOOLS are addressed-workspace-only by construction
    assert json.loads(call(server.get_env, "wsA", "FLEET_TOKEN"))["value"] == "tok-abc123"
    with pytest.raises(WorkbenchError, match="not set in workspace 'wsB'"):
        call(server.get_env, "wsB", "FLEET_TOKEN")
    assert not (root / "wsB" / ".workbench-env.json").exists()


def test_env_file_of_own_workspace_is_readable(root, allowexec):
    """Only the ADDRESSED workspace's own files are reachable (no over-block)."""
    _two_workspaces(root)
    out = json.loads(call(server.run_command, "wsA", ["cat", ".workbench-env.json"]))
    assert "FLEET_TOKEN" in out["stdout"]


def test_argv0_path_form_cannot_escape_even_with_allowlisted_basename(root, allowexec):
    """'../x/python3'-style argv[0] used to pass the basename allowlist; D7
    confinement refuses any argv[0] resolving outside the workspace."""
    _two_workspaces(root)
    with pytest.raises(WorkbenchError, match="outside workspace"):
        call(server.run_command, "wsB", ["../wsA/python3"])


def test_normal_commands_are_unaffected(root, allowexec):
    """No false positives: plain in-workspace work runs exactly as before."""
    _two_workspaces(root)
    out = json.loads(call(server.run_command, "wsA", ["cat", "secret.txt"]))
    assert "TOPSECRET" in out["stdout"]
    out = json.loads(call(server.run_command, "wsA", ["python3", "-c", "print(1/2)"]))
    assert out["stdout"].strip() == "0.5"
    out = json.loads(
        call(server.run_command, "wsA", ["python3", "-c", "url='https://example.com/org/repo'; print(url)"])
    )
    assert "https://example.com/org/repo" in out["stdout"]
    out = json.loads(call(server.run_command, "wsA", ["ls", "."]))
    assert "secret.txt" in out["stdout"]
    out = json.loads(call(server.run_command, "wsA", ["grep", "-r", "TOPSECRET", "."]))
    assert "secret.txt" in out["stdout"]


# ---------------------------------------------------------------------------
# D7 escape hatch: WORKBENCH_SHARED_PATHS
# ---------------------------------------------------------------------------


def test_shared_paths_allow_cross_workspace_access(root, allowexec, monkeypatch):
    ws_a, _ = _two_workspaces(root)  # only wsA's path is asserted below
    shared = root.parent / "shared-area"  # outside the workbench root
    shared.mkdir()
    (shared / "note.txt").write_text("SHARED-NOTE")
    inroot = root / "shared"  # inside the root, outside every workspace
    inroot.mkdir()
    (inroot / "note.txt").write_text("INROOT-SHARED")
    monkeypatch.setenv("WORKBENCH_SHARED_PATHS", f"{shared}:{inroot}:not/absolute::  ")
    # commands: absolute AND traversal forms both reach the shared dirs now
    out = json.loads(call(server.run_command, "wsB", ["cat", str(shared / "note.txt")]))
    assert "SHARED-NOTE" in out["stdout"]
    out = json.loads(call(server.run_command, "wsB", ["cat", "../../shared-area/note.txt"]))
    assert "SHARED-NOTE" in out["stdout"]
    out = json.loads(call(server.run_command, "wsB", ["cat", "../shared/note.txt"]))
    assert "INROOT-SHARED" in out["stdout"]
    out = json.loads(call(server.run_command, "wsA", ["cp", str(shared / "note.txt"), "local-copy.txt"]))
    assert out["exit_code"] == 0
    # other workspaces stay sealed even with a hatch configured
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "wsB", ["cat", "../wsA/secret.txt"])
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "wsB", ["cat", "../.audit.jsonl"])
    # file tools: read/write/list/delete against the shared dirs are allowed
    read = json.loads(call(server.read_file, "wsB", str(shared / "note.txt")))
    assert read["content"] == "SHARED-NOTE"
    write = json.loads(call(server.write_file, "wsB", str(inroot / "out.txt"), "x"))
    assert write["written"] is True
    listing = json.loads(call(server.list_files, "wsB", str(inroot)))
    # entries outside the addressed workspace are reported by absolute path
    assert {e["path"] for e in listing["entries"]} == {
        str(inroot / "note.txt"),
        str(inroot / "out.txt"),
    }
    deleted = json.loads(call(server.delete_file, "wsB", str(inroot / "out.txt"), confirm=True))
    assert deleted["deleted"] is True
    # non-shared paths are still refused with the hatch named
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.read_file, "wsB", str(ws_a / "secret.txt"))
    with pytest.raises(WorkbenchError, match="escapes the workspace"):
        call(server.read_file, "wsB", "../wsA/secret.txt")


def test_shared_paths_parsing(root, monkeypatch):
    monkeypatch.delenv("WORKBENCH_SHARED_PATHS", raising=False)
    assert server._shared_roots() == ()
    monkeypatch.setenv("WORKBENCH_SHARED_PATHS", "")
    assert server._shared_roots() == ()
    monkeypatch.setenv("WORKBENCH_SHARED_PATHS", f"  {root} ::rel/not-absolute:")
    roots = server._shared_roots()
    assert roots == (root.resolve(),)  # relative entry ignored (warned), blanks dropped
    assert server._path_is_shared(root / "wsA" / "secret.txt")
    assert server._path_is_shared(root)
    assert not server._path_is_shared(root.parent / "elsewhere")


def test_shared_paths_status_endpoint_reports_them(root, monkeypatch):
    from starlette.testclient import TestClient

    shared = root.parent / "shared"
    monkeypatch.setenv("WORKBENCH_SHARED_PATHS", str(shared))
    app = server._build_http_app()
    with TestClient(app) as c:
        data = c.get("/api/status").json()
    assert data["policy"]["shared_paths"] == [str(shared.resolve())]


def test_cross_workspace_read_refused_through_ui_run_api(root, allowexec, monkeypatch):
    """The web console's /api/run uses the same core, so D7 holds there too."""
    from starlette.testclient import TestClient

    _two_workspaces(root)
    app = server._build_http_app()
    with TestClient(app) as c:
        r = c.post("/api/ws/wsB/run", json={"command": ["cat", "../wsA/secret.txt"]})
        assert r.status_code == 400
        assert "WORKBENCH_SHARED_PATHS" in r.json()["error"]
        r = c.post("/api/ws/wsB/run", json={"command": ["cat", "../.audit.jsonl"]})
        assert r.status_code == 400 and "WORKBENCH_SHARED_PATHS" in r.json()["error"]


# ---------------------------------------------------------------------------
# PATH-erosion defense: workspace PATH never decides what executes
# ---------------------------------------------------------------------------


def test_workspace_path_override_cannot_bypass_allowlist(root, allowexec, monkeypatch, tmp_path):
    """The audit's PATH-erosion vector: a workspace env PATH that shadows an
    allow-listed name must not redirect execution.  The allowlist resolves
    the ORIGINAL binary via the server PATH; the workspace PATH only flows
    into the child environment."""
    call(server.workspace_create, "ws")
    evilbin = tmp_path / "evilbin"
    evilbin.mkdir()
    evil = evilbin / "python3"
    evil.write_text("#!/bin/sh\necho PWNED\n")
    evil.chmod(0o755)
    call(server.set_env, "ws", "PATH", f"{evilbin}:/nonexistent-dir")
    out = json.loads(call(server.run_command, "ws", ["python3", "-c", "import os; print(os.environ['PATH'])"]))
    assert out["exit_code"] == 0
    assert out["stdout"].strip() == f"{evilbin}:/nonexistent-dir"  # ws PATH reaches child env...
    assert (
        "hello" in json.loads(call(server.run_command, "ws", ["python3", "-c", "print('hello')"]))["stdout"]
    )  # ...but the REAL python3 executed
    everything = (
        out["stdout"] + json.loads(call(server.run_command, "ws", ["python3", "-c", "print('second')"]))["stdout"]
    )
    assert "PWNED" not in everything


def test_bare_command_missing_from_server_path_errors_clearly(root, monkeypatch):
    call(server.workspace_create, "ws")
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ghostcmd-xyz")
    with pytest.raises(WorkbenchError, match="not found on the server PATH"):
        call(server.run_command, "ws", ["ghostcmd-xyz"])


def test_allowlist_and_denylist_still_apply(root, allowexec, monkeypatch, tmp_path):
    """The PATH fix must not weaken the gate: unknown/denied names are still
    refused BEFORE any resolution."""
    call(server.workspace_create, "ws")
    evilbin = tmp_path / "evilbin2"
    evilbin.mkdir()
    for name in ("bash", "curl"):
        script = evilbin / name
        script.write_text("#!/bin/sh\necho PWNED\n")
        script.chmod(0o755)
    call(server.set_env, "ws", "PATH", str(evilbin))
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["bash", "-c", "echo hi"])
    monkeypatch.setenv("WORKBENCH_EXEC_DENYLIST", "curl")
    with pytest.raises(WorkbenchError, match="deny-listed"):
        call(server.run_command, "ws", ["curl", "http://example.com"])


# ---------------------------------------------------------------------------
# read_file streaming (fleet audit quick win: whole-file loads → OOM)
# ---------------------------------------------------------------------------


def test_read_file_streaming_bounded_and_correct(root, monkeypatch):
    """A multi-GB sparse file must read promptly, memory-bounded, with the
    exact slice as content — and the implementation must never call
    Path.read_bytes (the whole-file load) at all."""
    call(server.workspace_create, "ws")
    big = root / "ws" / "sparse.bin"
    with open(big, "wb") as fh:  # 8 GiB sparse file: ~4 KiB on disk
        fh.seek(8 * 1024**3)
        fh.write(b"END-OF-SPARSE")
    assert big.stat().st_size == 8 * 1024**3 + 13

    def _boom(self):
        raise AssertionError("Path.read_bytes called — read_file must stream, not load")

    monkeypatch.setattr(pathlib.Path, "read_bytes", _boom)
    t0 = time.monotonic()
    out = json.loads(call(server.read_file, "ws", "sparse.bin", max_bytes=16))
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"slice read took {elapsed:.1f}s — not memory/time-bounded"
    assert out["bytes"] == 16 and out["truncated"] is True
    assert out["content"] == "\x00" * 16  # the sparse hole reads as NULs
    assert out["workspace"] == "ws" and out["path"] == "sparse.bin"


def test_read_file_slice_matches_full_prefix(root):
    call(server.workspace_create, "ws")
    body = "abcdefghij" * 10000  # 100_000 bytes
    call(server.write_file, "ws", "known.txt", body)
    out = json.loads(call(server.read_file, "ws", "known.txt", max_bytes=1000))
    assert out["content"] == body[:1000] and out["truncated"] is True and out["bytes"] == 1000
    # slice larger than the file: whole content, truncated False (edge preserved)
    out = json.loads(call(server.read_file, "ws", "known.txt", max_bytes=100_000))
    assert out["content"] == body and out["truncated"] is False and out["bytes"] == 100_000
    # default cap (65536) truncates the 100_000-byte file
    out = json.loads(call(server.read_file, "ws", "known.txt"))
    assert out["bytes"] == 65536 and out["truncated"] is True


def test_read_file_zero_and_negative_caps(root):
    call(server.workspace_create, "ws")
    call(server.write_file, "ws", "f.txt", "data")
    out = json.loads(call(server.read_file, "ws", "f.txt", max_bytes=0))
    assert out["content"] == "" and out["bytes"] == 0 and out["truncated"] is True
    with pytest.raises(WorkbenchError, match="max_bytes must be >= 0"):
        call(server.read_file, "ws", "f.txt", max_bytes=-1)
    # empty-file edge unchanged: whole (empty) content, truncated False
    (root / "ws" / "empty.txt").write_bytes(b"")
    out = json.loads(call(server.read_file, "ws", "empty.txt", max_bytes=10))
    assert out["content"] == "" and out["truncated"] is False
    with pytest.raises(WorkbenchError, match="no such file"):
        call(server.read_file, "ws", "ghost.txt")


# ---------------------------------------------------------------------------
# file tools: cross-workspace refusals keep their hatch-named errors
# ---------------------------------------------------------------------------


def test_file_tools_refuse_cross_workspace_paths(root):
    _two_workspaces(root)
    for fn, args in [
        (server.read_file, ("wsB", "../wsA/secret.txt")),
        (server.read_file, ("wsB", f"{root}/wsA/secret.txt")),
        (server.write_file, ("wsB", "../wsA/evil.txt", "x")),
        (server.delete_file, ("wsB", "../wsA/secret.txt", True)),
    ]:
        with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
            call(fn, *args)
    # symlink escape into another workspace still refused, same error family
    os.symlink(str(root / "wsA"), str(root / "wsB" / "sneaky"))
    with pytest.raises(WorkbenchError, match="escapes the workspace"):
        call(server.read_file, "wsB", "sneaky/secret.txt")


def test_list_files_on_workspace_root_lists_only_that_workspace(root):
    _two_workspaces(root)
    listing = json.loads(call(server.list_files, "wsB"))
    assert {e["path"] for e in listing["entries"]} == set()  # wsB is empty
    (root / "wsB" / "own.txt").write_text("x")
    listing = json.loads(call(server.list_files, "wsB"))
    assert {e["path"] for e in listing["entries"]} == {"own.txt"}
