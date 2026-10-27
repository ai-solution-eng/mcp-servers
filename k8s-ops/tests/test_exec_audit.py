"""Exec audit trail tests (fleet D16, hash-chained).

exec_in_pod's audit record is the only forensic trace of who ran what where.
It used to be an unstructured stdout print; it is now ALSO written as a JSONL
entry through the fleet's shared HashChainedAuditLog (vendored
mcp_fleet_common), so entries chain and can be verified.

The stdout line is deliberately KEPT (existing log matchers depend on it), so
these tests pin BOTH channels and the chain link itself.

Run:  cd /home/andrew/Code/HPE/mcp_servers/K8S-MCP && python -m pytest test_exec_audit.py -v
"""

import asyncio
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_namespace_policy import (  # noqa: E402
    _install_kubernetes_stubs,
    _LabeledPod,
    _pop_policy_envs,
    ensure_exec_state,
)


@pytest.fixture(scope="module")
def server():
    _install_kubernetes_stubs()
    _pop_policy_envs()
    os.environ.pop("K8S_MCP_EXEC_ENABLED", None)
    from k8s_mcp import server as server_module

    yield server_module


def _entries(path):
    return [json.loads(ln) for ln in path.read_text().strip().splitlines() if ln.strip()]


def test_exec_audit_writes_jsonl_with_chain(server, tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()

    server._exec_audit(
        decision="allowed",
        caller="alice",
        namespace="team-a",
        pod="web-0",
        container="",
        command=["ps", "aux"],
        reason=None,
    )
    server._exec_audit(
        decision="denied",
        caller="bob",
        namespace="team-a",
        pod="web-0",
        container="",
        command=["sh"],
        reason="shell not allowed",
    )

    entries = _entries(trail)
    assert len(entries) == 2
    first, second = entries

    # content is forensically complete
    assert first["tool"] == "exec_in_pod"
    assert first["decision"] == "allowed"
    assert first["caller"] == "alice"
    assert first["namespace"] == "team-a"
    assert first["pod"] == "web-0"
    assert first["command"] == ["ps", "aux"]
    assert "reason" not in first, "an allow carries no reason field"

    assert second["decision"] == "denied"
    assert second["reason"] == "shell not allowed"

    # the chain link is real: entry N's prev_sha256 == sha256(line N-1)
    lines = trail.read_text().strip().splitlines()
    assert second["prev_sha256"] == hashlib.sha256(lines[0].encode()).hexdigest()
    # first entry seeds from genesis (64 zeros)
    assert first["prev_sha256"] == "0" * 64


def test_exec_audit_chain_verifies(server, tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()

    for i in range(3):
        server._exec_audit(
            decision="allowed",
            caller=f"u{i}",
            namespace="ns",
            pod=f"p{i}",
            container="c",
            command=["ls"],
            reason=None,
        )

    from mcp_fleet_common.audit import verify_audit_chain

    report = verify_audit_chain(str(trail))
    assert report["ok"] is True, report
    assert report["entries"] == 3


def test_tampering_breaks_the_chain(server, tmp_path, monkeypatch):
    """The point of chaining: an edited line is detectable."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    for i in range(2):
        server._exec_audit(
            decision="allowed", caller=f"u{i}", namespace="ns", pod="p", container="", command=["ls"], reason=None
        )

    lines = trail.read_text().strip().splitlines()
    doctored = lines[0].replace('"u0"', '"attacker"')
    trail.write_text(chr(10).join([doctored, lines[1]]) + chr(10))

    from mcp_fleet_common.audit import verify_audit_chain

    report = verify_audit_chain(str(trail))
    assert report["ok"] is False, "a modified line must fail verification"


def test_stdout_line_is_still_emitted(server, tmp_path, monkeypatch, capsys):
    """Legacy log matchers must keep working alongside the JSONL trail."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="denied",
        caller="carol",
        namespace="team-a",
        pod="db-0",
        container="",
        command=["cat"],
        reason="namespace policy",
    )
    out = capsys.readouterr().out
    assert "AUDIT exec decision=denied" in out
    assert "client=carol" in out
    assert "pod=team-a/db-0" in out


def test_unwritable_path_never_breaks_the_tool(server, tmp_path, monkeypatch, capsys):
    """Auditing is best-effort by contract: a bad path must not raise."""
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(tmp_path / "nope" / "deeper" / "audit.jsonl"))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    # must not raise
    server._exec_audit(
        decision="allowed", caller="d", namespace="ns", pod="p", container="", command=["ls"], reason=None
    )

# ══════════════════════════════════════════════════════════════════════════
# Label-gate posture: the gate can be switched off, but never quietly.
#
# The live finding (2026-10-26): the deployed site values set
# K8S_MCP_EXEC_REQUIRE_LABEL=false, so exec_in_pod reached an unlabelled pod's
# container runtime while the tool description still advertised the label gate.
# The knob stays (operators need it); these tests pin that the posture is
# recorded and announced everywhere a caller could look.
# ══════════════════════════════════════════════════════════════════════════


def _enabled_with_gate(monkeypatch, server, gate: str):
    """exec registered, gate posture forced to "enforce" / "off"."""
    ensure_exec_state(server, True)
    monkeypatch.setenv("K8S_MCP_EXEC_ENABLED", "true")
    if gate == "off":
        monkeypatch.setenv("K8S_MCP_EXEC_REQUIRE_LABEL", "false")
    else:
        monkeypatch.delenv("K8S_MCP_EXEC_REQUIRE_LABEL", raising=False)
    return server


def test_allowed_entry_records_the_enforced_gate(server, tmp_path, monkeypatch):
    """Every entry names the gate state, even when it is the safe one."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.delenv("K8S_MCP_EXEC_REQUIRE_LABEL", raising=False)
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="allowed", caller="alice", namespace="ns", pod="p", container="", command=["ps"], reason=None
    )
    entry = _entries(trail)[0]
    assert entry["label_gate"] == "enforce", "an allow records label_gate=enforce"


def test_allowed_entry_records_the_disabled_gate(server, tmp_path, monkeypatch, capsys):
    """With the gate off the trail says so — allowed is not the same event."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setenv("K8S_MCP_EXEC_REQUIRE_LABEL", "false")
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="allowed", caller="bob", namespace="ns", pod="p", container="", command=["ps"], reason=None
    )
    entry = _entries(trail)[0]
    assert entry["label_gate"] == "off", "an allow under a disabled gate records label_gate=off"
    out = capsys.readouterr().out
    assert "label_gate=off" in out, "the stdout line names the disabled gate too"


def test_hardened_stdout_line_stays_byte_identical(server, tmp_path, monkeypatch, capsys):
    """Existing log matchers keep working: no gate note when the gate is ON."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.delenv("K8S_MCP_EXEC_REQUIRE_LABEL", raising=False)
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="allowed", caller="carol", namespace="team-a", pod="web-0", container="side", command=["ps"], reason=None
    )
    out = capsys.readouterr().out.strip()
    assert out == (
        "AUDIT exec decision=allowed client=carol pod=team-a/web-0 container=side command=['ps']"
    ), "the hardened line is unchanged (no posture suffix)"


def test_gate_off_raises_a_startup_warning(server, monkeypatch):
    _enabled_with_gate(monkeypatch, server, "off")
    warnings = server.exec_posture_warnings()
    assert warnings, "exec enabled with the label gate off MUST warn"
    text = " ".join(warnings)
    assert "label gate OFF" in text and "K8S_MCP_EXEC_REQUIRE_LABEL=false" in text
    assert "audit-logged" in text, "the warning names the forensic consequence"


def test_enforced_gate_is_quiet(server, monkeypatch):
    _enabled_with_gate(monkeypatch, server, "enforce")
    assert server.exec_posture_warnings() == [], "an enforced gate needs no warning"


def test_disabled_exec_is_quiet(server, monkeypatch):
    """Gate off but exec off: nothing is reachable, so nothing to warn about."""
    ensure_exec_state(server, False)
    monkeypatch.delenv("K8S_MCP_EXEC_ENABLED", raising=False)
    monkeypatch.setenv("K8S_MCP_EXEC_REQUIRE_LABEL", "false")
    assert server.exec_posture_warnings() == []


def test_startup_summary_names_the_posture(server, monkeypatch):
    _enabled_with_gate(monkeypatch, server, "off")
    summary = server.startup_posture_summary()
    assert "label gate OFF" in summary and "NOT hardened" in summary
    assert "namespaces:" in summary, "the namespace posture rides the same banner"
    _enabled_with_gate(monkeypatch, server, "enforce")
    assert "label gate ENFORCED" in server.startup_posture_summary()


def test_tool_description_carries_the_live_posture(server, monkeypatch):
    """tools/list must not advertise a label gate the deployment does not run."""
    _enabled_with_gate(monkeypatch, server, "off")
    off_text = server._exec_tool_description()
    assert "label gate DISABLED" in off_text and "NOT hardened" in off_text
    _enabled_with_gate(monkeypatch, server, "enforce")
    on_text = server._exec_tool_description()
    assert "label gate ENFORCED" in on_text and "DISABLED" not in on_text
    assert "k8s-mcp.io/exec" in on_text, "the contract still states the label"


def test_gate_off_surfaces_on_every_denial(server, fakebin, monkeypatch):
    """A refusal must not read as 'the label gate stopped this'."""
    _enabled_with_gate(monkeypatch, server, "off")
    monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
    monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["sh", "-c", "id"]))
    assert r.startswith("Error:") and "label gate is" in r and "OFF" in r


def test_gate_off_annotates_a_successful_exec(server, fakebin, monkeypatch):
    """The caller is told, in the result, that no label opt-in was required."""
    _enabled_with_gate(monkeypatch, server, "off")
    monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
    monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps", "aux"]))
    assert "[--] [ps] [aux]" in r, "the exec still runs"
    assert "WARNING" in r and "label gate is OFF" in r, "the result names the open posture"
    assert "label_gate=off" in r


def test_enforced_gate_annotates_nothing(server, fakebin, monkeypatch):
    """A hardened deployment's successful result stays clean."""
    _enabled_with_gate(monkeypatch, server, "enforce")
    monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
    monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps", "aux"]))
    assert "WARNING" not in r and "label gate" not in r


def test_wildcard_exec_scope_reads_as_every_namespace(server, monkeypatch):
    """A bare "*" is reported as what it is, not as the literal glob."""
    _enabled_with_gate(monkeypatch, server, "off")
    monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "*")
    assert "every namespace" in server.exec_posture()
    assert "namespaces *" not in server.exec_posture()
    monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "debug-*,team-a")
    narrowed = server.exec_posture()
    assert "debug-*" in narrowed and "team-a" in narrowed, "an explicit list is named verbatim"


# ══════════════════════════════════════════════════════════════════════════
# Exec OUTCOME, not just permission (wave-2 P3-b).
#
# The pre-exec "allowed" entry proves the attempt happened; it cannot say
# whether the command ran. A permitted-but-failed exec (the live
# "ls: executable file not found") used to look identical to a success.
# Both entries must exist, in that order.
# ══════════════════════════════════════════════════════════════════════════


def test_completed_entry_carries_the_outcome(server, tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="completed", caller="alice", namespace="ns", pod="p", container="", command=["ps"], reason=None,
        outcome="ok",
    )
    entry = _entries(trail)[0]
    assert entry["decision"] == "completed" and entry["outcome"] == "ok"
    assert "error" not in entry, "a clean run carries no error field"


def test_failed_outcome_carries_a_bounded_error_class(server, tmp_path, monkeypatch, capsys):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="completed", caller="bob", namespace="ns", pod="p", container="", command=["ls"], reason=None,
        outcome="failed", error='command terminated with exit code 127: ls: executable file not found',
    )
    entry = _entries(trail)[0]
    assert entry["outcome"] == "failed"
    assert "executable file not found" in entry["error"]
    out = capsys.readouterr().out
    assert "decision=completed" in out and "outcome=failed" in out


def test_error_class_is_first_line_and_bounded(server):
    long_line = "x" * 400
    assert server._exec_error_class(f"Error: {long_line}\nsecond line") == "x" * 160
    assert server._exec_error_class("Error: boom\nmore") == "boom"
    assert server._exec_error_class("Error: ") == "unknown error"


def test_allowed_entry_stays_pre_exec(server, tmp_path, monkeypatch):
    """The pre-exec record must NOT carry an outcome — it is written before."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    server._exec_audit(
        decision="allowed", caller="carol", namespace="ns", pod="p", container="", command=["ps"], reason=None
    )
    entry = _entries(trail)[0]
    assert entry["decision"] == "allowed"
    assert "outcome" not in entry, "the pre-exec entry must not claim an outcome it cannot know"


def test_exec_writes_allowed_then_completed(server, fakebin, tmp_path, monkeypatch):
    """End to end: two chained entries, in order, for one permitted exec."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
    monkeypatch.delenv("K8S_MCP_EXEC_REQUIRE_LABEL", raising=False)
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
    ensure_exec_state(server, True)
    monkeypatch.setenv("K8S_MCP_EXEC_ENABLED", "true")

    asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps", "aux"]))

    entries = _entries(trail)
    assert [e["decision"] for e in entries] == ["allowed", "completed"], "attempt then outcome"
    assert entries[1]["outcome"] == "ok"
    assert "error" not in entries[1]


def test_failed_exec_is_distinguishable_end_to_end(server, fakebin, tmp_path, monkeypatch):
    """The live shape: permitted, ran, failed inside the container."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("K8S_MCP_AUDIT_FILE", str(trail))
    monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
    monkeypatch.delenv("K8S_MCP_EXEC_REQUIRE_LABEL", raising=False)
    monkeypatch.setattr(server, "_EXEC_AUDIT_LOG", None)
    server._init_exec_audit()
    monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
    ensure_exec_state(server, True)
    monkeypatch.setenv("K8S_MCP_EXEC_ENABLED", "true")

    async def _boom(argv):
        raise server.KubectlError(
            'command terminated with exit code 127: OCI runtime exec failed: exec: "ls": '
            "executable file not found in $PATH: unknown"
        )

    monkeypatch.setattr(server, "_kubectl_exec", _boom)

    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps", "aux"]))
    assert r.startswith("Error:"), "the caller sees the failure"

    entries = _entries(trail)
    assert [e["decision"] for e in entries] == ["allowed", "completed"]
    assert entries[1]["outcome"] == "failed"
    assert "executable file not found" in entries[1]["error"], "the trail names the real reason"
