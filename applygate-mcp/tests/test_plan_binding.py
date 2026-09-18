"""D11 plan-binding tests — apply_manifest is sha256-bound to plan_apply.

Decision D11 (ratified 2026-09, FLEET-EXECUTION-PLAN §12): `apply_manifest`
must carry (``plan_sha256``) or reference from session state the sha256 of
the EXACT bytes a successful `plan_apply` recorded for the namespace.
Missing plan or tampered bytes REFUSE BY DEFAULT — automation that skips
plan_apply fails by design; that is the ratified point. The documented
migration path is `APPLYGATE_UNPLANNED_APPLY=warn` (logs + applies) or
`=allow` (pre-D11 behavior).

The matrix here pins the DEFAULT (env unset = deny, fail-closed on invalid
values), the carried-sha variants, the warn/allow modes, and the namespace
keying of the plan session. The other test files opt their gate-isolation
fixtures into `allow` — this file is where the default lives.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_plan_binding.py -v
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

import server

ENV_VARS = (
    "APPLYGATE_ALLOWED_NAMESPACES",
    "APPLYGATE_BLOCKED_NAMESPACES",
    "APPLYGATE_ALLOWED_KINDS",
    "APPLYGATE_AUDIT_FILE",
    "APPLYGATE_UNPLANNED_APPLY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """D11 tests pin the DEFAULT: APPLYGATE_UNPLANNED_APPLY stays UNSET."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    server._caller_context.set(None)  # no caller leakage between tests
    server._plan_session.clear()  # the binding session is per-test here


def run(coro):
    return asyncio.run(coro)


def allow(monkeypatch, *patterns):
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", ",".join(patterns))


class _SeamSpy:
    def __init__(self, names, result=None):
        self.names = names
        self.calls = []
        self.result = result if result is not None else {"metadata": {"resourceVersion": "1234"}}

    def __call__(self, *args, **kwargs):
        record = dict(zip(self.names, args))
        record.update(kwargs)
        self.calls.append(record)
        return self.result


def install_apply_spy(monkeypatch):
    spy = _SeamSpy(("namespace", "doc", "dry_run"))
    monkeypatch.setattr(server, "_ssa_apply", spy)
    return spy


def doc(name="app-config", data=None):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name},
        **({"data": data} if data else {}),
    }


def manifest(*docs):
    return "\n---\n".join(yaml.safe_dump(d) for d in docs)


def parse(out: str) -> dict:
    return json.loads(out)


def audit_entries(tmp_path):
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


SIMPLE = manifest(doc(name="app-config", data={"k": "v"}))


# ---------------------------------------------------------------------------
# DEFAULT (env unset) = deny
# ---------------------------------------------------------------------------


def test_default_deny_apply_without_plan_is_refused_naming_the_env(monkeypatch):
    """D11 default: confirm-gated apply with NO prior plan refuses, and the
    refusal names the transition env."""
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    assert (os.environ.get("APPLYGATE_UNPLANNED_APPLY") or "") == ""  # default is pinned
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert out["refused"] is True and out["ok"] is False
    assert "plan binding (D11)" in out["error"]
    assert "APPLYGATE_UNPLANNED_APPLY" in out["error"]
    assert "plan_apply" in out["error"]
    assert spy.calls == []  # nothing reached the seam


def test_default_deny_is_fail_closed_on_invalid_env_value(monkeypatch):
    """A typo'd mode value fails CLOSED to deny, never open."""
    allow(monkeypatch, "team-a")
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "warnn")
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert out["refused"] is True and "D11" in out["error"]


# ---------------------------------------------------------------------------
# valid plan → sha match applies
# ---------------------------------------------------------------------------


def test_plan_then_apply_same_bytes_applies_and_result_carries_the_sha(monkeypatch):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    plan = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE)))
    assert plan["ok"] is True
    assert plan["manifest_sha256"] == server._manifest_sha256(SIMPLE)  # 64 hex, exact bytes
    applied = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert applied["ok"] is True and applied["plan_binding"] == "enforced"
    assert applied["manifest_sha256"] == plan["manifest_sha256"]
    real = [c for c in spy.calls if c["dry_run"] is False]
    assert len(real) == 1  # the plan's dry-run call + exactly one real apply


def test_apply_carrying_the_planned_sha_applies(monkeypatch):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    plan = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE)))
    applied = parse(
        run(
            server.apply_manifest(
                namespace="team-a", manifest=SIMPLE, confirm_apply=True, plan_sha256=plan["manifest_sha256"]
            )
        )
    )
    assert applied["ok"] is True and applied["plan_binding"] == "enforced"
    assert sum(1 for c in spy.calls if c["dry_run"] is False) == 1


# ---------------------------------------------------------------------------
# tampered bytes → refused
# ---------------------------------------------------------------------------


def test_apply_with_tampered_bytes_is_refused(monkeypatch):
    """Plan one manifest, apply DIFFERENT bytes (same namespace) → refused."""
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    tampered = manifest(doc(name="app-config", data={"k": "EVIL"}))
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=tampered, confirm_apply=True)))
    assert out["refused"] is True
    assert "do not match the planned bytes" in out["error"]
    assert server._manifest_sha256(SIMPLE) in out["error"]  # planned sha echoed
    assert server._manifest_sha256(tampered) in out["error"]  # actual sha echoed
    assert "APPLYGATE_UNPLANNED_APPLY" in out["error"]
    assert all(c["dry_run"] is True for c in spy.calls)  # only the plan's dry-run


def test_carried_sha_that_does_not_match_call_bytes_is_refused(monkeypatch):
    """A carried plan_sha256 must match THIS call's manifest bytes exactly."""
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    other = manifest(doc(name="other-config"))
    out = parse(
        run(
            server.apply_manifest(
                namespace="team-a", manifest=other, confirm_apply=True, plan_sha256=server._manifest_sha256(SIMPLE)
            )
        )
    )
    assert out["refused"] is True
    assert "does not match the sha256 of THIS call's manifest bytes" in out["error"]
    assert all(c["dry_run"] is True for c in spy.calls)


def test_carried_sha_without_any_plan_is_refused(monkeypatch):
    """Carrying a self-computed sha proves nothing — the plan must exist in
    session state (that is what binds apply to plan_apply)."""
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    out = parse(
        run(
            server.apply_manifest(
                namespace="team-a",
                manifest=SIMPLE,
                confirm_apply=True,
                plan_sha256=server._manifest_sha256(SIMPLE),  # never planned
            )
        )
    )
    assert out["refused"] is True
    assert "no plan_apply is recorded for namespace 'team-a'" in out["error"]
    assert spy.calls == []


# ---------------------------------------------------------------------------
# session-state semantics
# ---------------------------------------------------------------------------


def test_plan_session_is_keyed_by_namespace(monkeypatch):
    """A plan for team-a does not authorize team-b."""
    allow(monkeypatch, "team-a,team-b")
    spy = install_apply_spy(monkeypatch)
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    out = parse(run(server.apply_manifest(namespace="team-b", manifest=SIMPLE, confirm_apply=True)))
    assert out["refused"] is True and "no plan_apply has been recorded" in out["error"]
    assert all(c["dry_run"] is True for c in spy.calls)  # team-b never reached the seam
    # ...while team-a still applies.
    ok = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert ok["ok"] is True
    assert sum(1 for c in spy.calls if c["dry_run"] is False) == 1


def test_refused_plan_records_no_binding(monkeypatch):
    """A tool-level-refused plan (bad namespace policy) must NOT authorize a
    later apply — the binding only comes from a plan that reached the
    dry-run stage."""
    spy = install_apply_spy(monkeypatch)  # no allowlist → plan refused
    refused_plan = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE)))
    assert refused_plan["refused"] is True
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", "team-a")  # operator "fixes" policy
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert out["refused"] is True and "D11" in out["error"]
    assert spy.calls == []


def test_latest_plan_wins(monkeypatch):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    v1 = manifest(doc(name="app-config", data={"rev": "1"}))
    v2 = manifest(doc(name="app-config", data={"rev": "2"}))
    run(server.plan_apply(namespace="team-a", manifest=v1))
    run(server.plan_apply(namespace="team-a", manifest=v2))  # re-plan → latest binds
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=v2, confirm_apply=True)))
    assert out["ok"] is True and out["manifest_sha256"] == server._manifest_sha256(v2)
    stale = parse(run(server.apply_manifest(namespace="team-a", manifest=v1, confirm_apply=True)))
    assert stale["refused"] is True
    assert sum(1 for c in spy.calls if c["dry_run"] is False) == 1  # only v2 applied


# ---------------------------------------------------------------------------
# warn mode — logs + applies (the documented migration path)
# ---------------------------------------------------------------------------


def test_warn_mode_logs_loudly_and_applies(monkeypatch, capsys, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "warn")
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert out["ok"] is True and out["plan_binding"] == "warn"
    assert len(spy.calls) == 1 and spy.calls[0]["dry_run"] is False
    # The warn names the env on stderr…
    err = capsys.readouterr().err
    assert "APPLYGATE_UNPLANNED_APPLY" in err and "plan_apply" in err
    # …and the audit entries carry the additive warn marker (readers of the
    # old format are unaffected — outcome stays "applied").
    lines = [e for e in audit_entries(tmp_path) if e["tool"] == "apply_manifest"]
    assert lines and all(e["outcome"] == "applied" for e in lines)
    assert all(e.get("plan_binding") == "warn" for e in lines)


def test_warn_mode_also_forgives_byte_mismatch_but_logs_it(monkeypatch, capsys):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    tampered = manifest(doc(name="app-config", data={"k": "EVIL"}))
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "warn")
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=tampered, confirm_apply=True)))
    assert out["ok"] is True and out["plan_binding"] == "warn"
    assert "APPLYGATE_UNPLANNED_APPLY" in capsys.readouterr().err
    assert sum(1 for c in spy.calls if c["dry_run"] is False) == 1


def test_warn_mode_still_refuses_a_mismatched_carried_sha(monkeypatch):
    """warn forgives MISSING binding for migration — it does not bless a
    caller-explicit sha claim that contradicts the bytes."""
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "warn")
    out = parse(
        run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True, plan_sha256="0" * 64))
    )
    assert out["refused"] is True
    assert all(c["dry_run"] is True for c in spy.calls)


# ---------------------------------------------------------------------------
# allow mode — exact pre-D11 behavior
# ---------------------------------------------------------------------------


def test_allow_mode_is_the_pre_d11_behavior(monkeypatch, capsys, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "allow")
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert out["ok"] is True and out["plan_binding"] == "allow"
    assert len(spy.calls) == 1
    assert "plan-binding" not in capsys.readouterr().err  # no warning spam in allow mode


# ---------------------------------------------------------------------------
# plan tool surface
# ---------------------------------------------------------------------------


def test_plan_result_surfaces_manifest_sha256_for_callers(monkeypatch):
    allow(monkeypatch, "team-a")
    plan = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE)))
    assert plan["manifest_sha256"] == server._manifest_sha256(SIMPLE)
    assert len(plan["manifest_sha256"]) == 64


def test_unplanned_apply_refusal_is_audit_logged(tmp_path):
    run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True))
    lines = audit_entries(tmp_path)
    assert len(lines) == 1
    assert lines[0]["tool"] == "apply_manifest" and lines[0]["outcome"] == "refused"
    assert lines[0]["dry_run"] is False
