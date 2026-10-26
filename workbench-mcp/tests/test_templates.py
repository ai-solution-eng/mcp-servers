"""Wave-5 F3 tests — workspace templates via canned argv (ADDITIVE, opt-in).

Covers the audit's accepted feature idea (FLEET-AUDIT §3.3 workbench "Feature
ideas accepted: workspace templates via canned argv"):

1. Opt-in posture — with no WORKBENCH_TEMPLATES configured the ``template``
   parameter is REFUSED and everything else is byte-identical (same response
   shape, same audit event with no template field).
2. Widening only — a template adds operator-supplied binaries to the
   workspace's exec allowlist (denylist still wins); the widened allowlist is
   re-derived from the CURRENT env on every call, never persisted.
3. Canned setup runs INSIDE the new workspace through the exact run_command
   machinery — D7 confinement, PATH-erosion defense, allowlist resolution,
   timeouts and output caps all apply, and each setup run is audited with the
   template name.
4. Malformed config fails closed (template skipped, never half-applied).

Run:  cd mcp_servers/workbench_mcp && SQLhandler/.venv312/bin/python -m pytest tests/test_templates.py -v
"""

import asyncio
import json
import os

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


@pytest.fixture(autouse=True)
def no_templates(monkeypatch):
    """No templates configured unless a test opts in (the default posture)."""
    monkeypatch.delenv("WORKBENCH_TEMPLATES", raising=False)


@pytest.fixture()
def tooldir(tmp_path, monkeypatch):
    """A benign binary on the SERVER PATH that is NOT in the base allowlist —
    the vehicle for proving template-driven allowlist widening."""
    d = tmp_path / "tools-bin"
    d.mkdir()
    tool = d / "fleettool"
    tool.write_text("#!/bin/sh\necho TEMPLATE-TOOL-OK\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{os.environ.get('PATH', '')}")
    return d


TEMPLATE = {
    "description": "python scratch with the fleet tool",
    "extra_allowed": ["fleettool"],
    "canned_setup": [["fleettool"], ["mkdir", "src"]],
}


def audit_events(root):
    lines = (root / ".audit.jsonl").read_text().strip().splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# 1 · opt-in posture: no templates configured → parameter refused, no drift
# ---------------------------------------------------------------------------


def test_template_param_refused_when_none_configured(root):
    call(server.workspace_create, "plain")  # plain create still works
    with pytest.raises(WorkbenchError, match="WORKBENCH_TEMPLATES") as exc:
        call(server.workspace_create, "ws", template="anything")
    assert "NONE" in str(exc.value)  # the error says no templates are configured
    # and the refusal left no workspace behind
    assert not (root / "ws").exists()


def test_plain_create_is_byte_identical(root):
    """No template argument → exactly the pre-template response and audit."""
    out = json.loads(call(server.workspace_create, "ws"))
    assert out == {
        "workspace": "ws",
        "path": str(root / "ws"),
        "created": True,
    }
    events = audit_events(root)
    assert len(events) == 1
    assert events[0]["event"] == "workspace_create"
    assert events[0]["workspace"] == "ws"
    assert "template" not in events[0]


def test_empty_templates_object_behaves_like_unconfigured(root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"empty": {"description": "nothing on it"}}))
    with pytest.raises(WorkbenchError, match="unknown template"):
        call(server.workspace_create, "ws", template="missing")
    out = json.loads(call(server.workspace_create, "ws", template="empty"))
    assert out["template"] == "empty"
    assert out["setup"] == [] and out["setup_ok"] is True


# ---------------------------------------------------------------------------
# 2 · unknown template → clear error listing the configured ones
# ---------------------------------------------------------------------------


def test_unknown_template_error_lists_configured(root, monkeypatch):
    monkeypatch.setenv(
        "WORKBENCH_TEMPLATES",
        json.dumps({"alpha": TEMPLATE, "beta": {"description": "b"}}),
    )
    with pytest.raises(WorkbenchError, match="unknown template 'gamma'") as exc:
        call(server.workspace_create, "ws", template="gamma")
    assert "'alpha'" in str(exc.value) and "'beta'" in str(exc.value)
    assert not (root / "ws").exists()


# ---------------------------------------------------------------------------
# 3 · create-with-template: allowlist widened + canned setup runs + audit
# ---------------------------------------------------------------------------


def test_create_with_template_widens_allowlist_and_runs_setup(root, monkeypatch, tooldir):
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls,mkdir")  # NO fleettool
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    out = json.loads(call(server.workspace_create, "ws", template="pytools"))
    assert out["created"] is True and out["template"] == "pytools"
    assert out["setup_ok"] is True
    assert [s["exit_code"] for s in out["setup"]] == [0, 0]
    assert out["setup"][0]["stdout"].strip() == "TEMPLATE-TOOL-OK"  # extra binary RAN
    assert (root / "ws" / "src").is_dir()  # canned mkdir landed inside the ws

    # the widened allowlist persists for later run_command calls...
    out = json.loads(call(server.run_command, "ws", ["fleettool"]))
    assert out["exit_code"] == 0 and "TEMPLATE-TOOL-OK" in out["stdout"]
    # ...but ONLY by the operator's extras: everything else still gated
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["bash", "-c", "echo hi"])

    # audit: creation carries the template name; each setup run is its own
    # workspace_template_setup event with the template name attached
    events = audit_events(root)
    create = next(e for e in events if e["event"] == "workspace_create")
    assert create["template"] == "pytools"
    setups = [e for e in events if e["event"] == "workspace_template_setup"]
    assert [e["argv"] for e in setups] == [["fleettool"], ["mkdir", "src"]]
    assert all(e["template"] == "pytools" for e in setups)
    assert [e["setup_index"] for e in setups] == [0, 1]
    assert all(e["exit_code"] == 0 for e in setups)


def test_template_metadata_persists_only_the_name(root, monkeypatch, tooldir):
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    call(server.workspace_create, "ws", template="pytools")
    meta = json.loads((root / "ws" / ".workbench-template.json").read_text())
    assert meta["template"] == "pytools"
    assert "extra_allowed" not in meta and "canned_setup" not in meta


def test_widened_allowlist_is_rederived_not_persisted(root, monkeypatch, tooldir):
    """Only the template NAME is durable: the workspace can never be wider
    than the operator's CURRENT WORKBENCH_TEMPLATES defines."""
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    call(server.workspace_create, "ws", template="pytools")
    assert call(server.run_command, "ws", ["fleettool"])  # widened while configured

    # template removed from the env → extras vanish (fail closed)
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"other": TEMPLATE}))
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["fleettool"])

    # template re-added with DIFFERENT extras → the new set is what applies
    monkeypatch.setenv(
        "WORKBENCH_TEMPLATES",
        json.dumps({"pytools": {"description": "d", "extra_allowed": [], "canned_setup": []}}),
    )
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["fleettool"])

    # corrupt metadata file → fail closed to the base allowlist
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    (root / "ws" / ".workbench-template.json").write_text("{not json")
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["fleettool"])


def test_template_extras_do_not_leak_into_other_workspaces(root, monkeypatch, tooldir):
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    call(server.workspace_create, "templated", template="pytools")
    call(server.workspace_create, "plain")
    assert call(server.run_command, "templated", ["fleettool"])
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "plain", ["fleettool"])


def test_denylist_beats_template_extras(root, monkeypatch, tooldir):
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    monkeypatch.setenv("WORKBENCH_EXEC_DENYLIST", "fleettool")
    call(server.workspace_create, "ws", template="pytools")
    with pytest.raises(WorkbenchError, match="deny-listed"):
        call(server.run_command, "ws", ["fleettool"])


# ---------------------------------------------------------------------------
# 4 · ALL existing guards hold under template setup
# ---------------------------------------------------------------------------


def test_d7_confinement_still_holds_for_setup_commands(root, monkeypatch):
    call(server.workspace_create, "victim")
    call(server.write_file, "victim", "secret.txt", "TOPSECRET")
    evil = {
        "description": "d",
        "extra_allowed": [],
        "canned_setup": [["cat", "../victim/secret.txt"]],
    }
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "cat,mkdir")
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"evil": evil}))
    out = json.loads(call(server.workspace_create, "ws", template="evil"))
    # the workspace still exists, but the setup step was refused and reported
    assert out["created"] is True and out["setup_ok"] is False
    assert "error" in out["setup"][0]
    assert "WORKBENCH_SHARED_PATHS" in out["setup"][0]["error"]
    assert "TOPSECRET" not in json.dumps(out)
    events = audit_events(root)
    assert any(e["event"] == "workspace_template_setup_error" and e["template"] == "evil" for e in events)
    assert "TOPSECRET" in json.loads(call(server.read_file, "victim", "secret.txt"))["content"]
    # and D7 stays enforced for normal runs in the templated workspace too
    with pytest.raises(WorkbenchError, match="WORKBENCH_SHARED_PATHS"):
        call(server.run_command, "ws", ["cat", "../victim/secret.txt"])


def test_path_erosion_defense_holds_for_template_extras(root, monkeypatch, tmp_path, tooldir):
    """A workspace PATH override must not redirect a template-widened binary:
    resolution still happens against the SERVER's PATH."""
    call(server.workspace_create, "ws")
    evilbin = tmp_path / "evilbin"
    evilbin.mkdir()
    evil = evilbin / "fleettool"
    evil.write_text("#!/bin/sh\necho PWNED\n")
    evil.chmod(0o755)
    call(server.set_env, "ws", "PATH", str(evilbin))
    monkeypatch.setenv(
        "WORKBENCH_TEMPLATES",
        json.dumps({"pytools": {"description": "d", "extra_allowed": ["fleettool"], "canned_setup": []}}),
    )
    # (the metadata file is what makes the workspace templated; seed it the
    # way workspace_create would — this test isolates run_command's resolution)
    (root / "ws" / ".workbench-template.json").write_text(json.dumps({"template": "pytools"}))
    out = json.loads(call(server.run_command, "ws", ["fleettool"]))
    assert out["stdout"].strip() == "TEMPLATE-TOOL-OK"  # the SERVER-PATH binary ran
    assert "PWNED" not in out["stdout"]


def test_template_setup_failures_are_reported_not_fatal(root, monkeypatch, tooldir):
    failing = {
        "description": "d",
        "extra_allowed": ["fleettool"],
        "canned_setup": [["ls", "/nonexistent-dir-for-f3-test"], ["mkdir", "ok-dir"]],
    }
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls,mkdir")
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"f": failing}))
    out = json.loads(call(server.workspace_create, "ws", template="f"))
    assert out["created"] is True
    assert out["setup_ok"] is False
    assert "setup_note" in out
    assert out["setup"][0]["exit_code"] != 0
    assert out["setup"][1]["exit_code"] == 0
    assert (root / "ws" / "ok-dir").is_dir()  # later commands still ran


def test_setup_output_is_capped(root, monkeypatch, tooldir):
    """The canned setup inherits run_command's output caps (WORKBENCH_MAX_OUTPUT_BYTES)."""
    big = {
        "description": "d",
        "extra_allowed": [],
        "canned_setup": [["python3", "-c", "print('x' * 5000)"]],
    }
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "python3")
    monkeypatch.setenv("WORKBENCH_MAX_OUTPUT_BYTES", "100")
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"big": big}))
    out = json.loads(call(server.workspace_create, "ws", template="big"))
    assert len(out["setup"][0]["stdout"]) <= 100
    assert out["setup"][0]["truncated"] is True


# ---------------------------------------------------------------------------
# 5 · malformed WORKBENCH_TEMPLATES fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '["a", "b"]',  # JSON array, not object
        '{"bad": {"extra_allowed": "not-a-list"}}',
        '{"bad": {"canned_setup": "nope"}}',
        '{"bad": {"canned_setup": [["ls", 42]]}}',
        '{"bad": {"canned_setup": [[]]}}',
        '{"bad": {"description": 7}}',
        '{"   ": {"description": "blank name"}}',
    ],
)
def test_malformed_template_configs_are_skipped(root, monkeypatch, raw):
    monkeypatch.setenv("WORKBENCH_TEMPLATES", raw)
    with pytest.raises(WorkbenchError, match="NONE"):
        call(server.workspace_create, "ws", template="bad")


def test_malformed_template_does_not_disable_valid_ones(root, monkeypatch, tooldir):
    mixed = {"bad": {"canned_setup": "nope"}, "good": dict(TEMPLATE)}
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps(mixed))
    with pytest.raises(WorkbenchError, match="unknown template 'bad'"):
        call(server.workspace_create, "ws1", template="bad")
    out = json.loads(call(server.workspace_create, "ws2", template="good"))
    assert out["setup_ok"] is True
    with pytest.raises(WorkbenchError, match="unknown template 'bad'") as exc:
        call(server.workspace_create, "ws3", template="bad")
    assert "'good'" in str(exc.value) and "'bad'" not in str(exc.value).split("configured templates:")[1]


def test_path_shaped_extra_allowed_entries_are_dropped(root, monkeypatch, tooldir):
    """extra_allowed must be bare binary names (argv[0]-basename matching).
    A path-shaped entry is dropped with a warning — it never widens anything
    and never invalidates the template."""
    with_path_only = {"description": "d", "extra_allowed": ["/usr/bin/fleettool"], "canned_setup": []}
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls")
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"t": with_path_only}))
    call(server.workspace_create, "ws", template="t")  # template still valid
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "ws", ["fleettool"])  # the path form widened nothing
    # a bare entry alongside a path-shaped one: only the bare one applies
    mixed = {"description": "d", "extra_allowed": ["/usr/bin/fleettool", "fleettool"], "canned_setup": []}
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"t2": mixed}))
    call(server.workspace_create, "ws2", template="t2")
    out = json.loads(call(server.run_command, "ws2", ["fleettool"]))
    assert out["exit_code"] == 0


def test_workspace_list_unaffected_by_templates(root, monkeypatch, tooldir):
    monkeypatch.setenv("WORKBENCH_TEMPLATES", json.dumps({"pytools": TEMPLATE}))
    call(server.workspace_create, "ws", template="pytools")
    listing = json.loads(call(server.workspace_list))
    assert [w["workspace"] for w in listing] == ["ws"]
