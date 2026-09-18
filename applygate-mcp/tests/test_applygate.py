"""Unit tests for applygate-mcp — the guardrail matrix, no cluster needed.

The test venv has NO `kubernetes` package by design: every test drives the
MCP tool functions through asyncio.run and monkeypatches the module-level
k8s seam functions (_ssa_apply / _get_status / _delete) with recording
fakes, so the policy code is what's under test — never a live API.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/ -v
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

import server


def run(coro):
    """Fleet test convention: drive the async tool functions directly."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ENV_VARS = (
    "APPLYGATE_ALLOWED_NAMESPACES",
    "APPLYGATE_BLOCKED_NAMESPACES",
    "APPLYGATE_ALLOWED_KINDS",
    "APPLYGATE_AUDIT_FILE",
    "APPLYGATE_UNPLANNED_APPLY",
    "APPLYGATE_METRICS_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """No ambient policy, and every test gets its own audit sink.

    D11 note: the D11 plan-binding DEFAULT (deny, env unset) is pinned in
    tests/test_plan_binding.py. THESE tests predate D11 and exercise the
    OTHER gates in isolation, so the fixture opts them into
    APPLYGATE_UNPLANNED_APPLY=allow — without it, every confirm-gated apply
    would first trip the plan-binding refusal and the gate-under-test would
    never be reached.
    """
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "allow")


def allow(monkeypatch, *patterns):
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", ",".join(patterns))


def block(monkeypatch, *patterns):
    monkeypatch.setenv("APPLYGATE_BLOCKED_NAMESPACES", ",".join(patterns))


def kinds(monkeypatch, *names):
    monkeypatch.setenv("APPLYGATE_ALLOWED_KINDS", ",".join(names))


class _SeamSpy:
    """Recording fake for a k8s seam function; optionally raises.

    `names` maps the positional args onto the seam's real signature so tests
    can assert on readable records like {"namespace": ..., "doc": ..., "dry_run": ...}.
    """

    def __init__(self, names, exc=None, result=None):
        self.names = names
        self.calls = []
        self.exc = exc
        self.result = result if result is not None else {}

    def __call__(self, *args, **kwargs):
        record = dict(zip(self.names, args))
        record.update(kwargs)
        self.calls.append(record)
        if self.exc is not None:
            raise self.exc
        return self.result


def install_apply_spy(monkeypatch, **kwargs):
    spy = _SeamSpy(
        names=("namespace", "doc", "dry_run"),
        result={"metadata": {"resourceVersion": "1234"}, "status": {}},
        **kwargs,
    )
    monkeypatch.setattr(server, "_ssa_apply", spy)
    return spy


def install_delete_spy(monkeypatch, **kwargs):
    spy = _SeamSpy(names=("namespace", "kind", "name"), result={"response": {}}, **kwargs)
    monkeypatch.setattr(server, "_delete", spy)
    return spy


def install_status_fake(monkeypatch, obj=None, exc=None):
    spy = _SeamSpy(names=("namespace", "kind", "name"), result=obj or {}, exc=exc)
    monkeypatch.setattr(server, "_get_status", spy)
    return spy


def doc(kind="ConfigMap", name="app-config", ns=None, api_version=None, **extra):
    d = {
        "apiVersion": api_version or ("v1" if kind == "ConfigMap" else "apps/v1"),
        "kind": kind,
        "metadata": {"name": name},
    }
    if ns:
        d["metadata"]["namespace"] = ns
    d.update(extra)
    return d


def manifest(*docs):
    return "\n---\n".join(yaml.safe_dump(d) for d in docs)


def parse(out: str) -> dict:
    """Tool results are json.dumps strings — parse them back."""
    return json.loads(out)


def audit_lines(tmp_path):
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


SIMPLE_MANIFEST = manifest(doc(name="app-config", data={"k": "v"}))


# ---------------------------------------------------------------------------
# 1. Namespace policy — default-deny matrix
# ---------------------------------------------------------------------------


def test_default_deny_no_allowlist_refuses_every_writable_tool(monkeypatch, tmp_path):
    """Unset allowlist = NOTHING writable — plan/apply/delete/status all refuse."""
    spy = install_apply_spy(monkeypatch)
    plan = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE_MANIFEST)))
    assert plan["ok"] is False and plan["refused"] is True
    assert "DEFAULT-DENY" in plan["error"] and "APPLYGATE_ALLOWED_NAMESPACES" in plan["error"]

    apply = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE_MANIFEST, confirm_apply=True)))
    assert apply["refused"] is True

    del_spy = install_delete_spy(monkeypatch)
    deleted = parse(run(server.delete_resource(namespace="team-a", kind="ConfigMap", name="x", confirm_delete=True)))
    assert deleted["refused"] is True

    status = parse(run(server.get_resource_status(namespace="team-a", kind="ConfigMap", name="x")))
    assert status["refused"] is True

    # The loud refusal message must name the knob that would change it.
    assert "APPLYGATE_ALLOWED_NAMESPACES" in deleted["error"]
    # And nothing reached the seams.
    assert spy.calls == [] and del_spy.calls == []
    # Refusals are audit-logged too (outcome=refused).
    outcomes = [e["outcome"] for e in audit_lines(tmp_path)]
    assert outcomes.count("refused") >= 4


def test_explicitly_empty_allowlist_also_denies(monkeypatch, tmp_path):
    """APPLYGATE_ALLOWED_NAMESPACES='' (set but empty) is still default-deny."""
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", "")
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE_MANIFEST)))
    assert out["refused"] is True
    assert spy.calls == []


def test_glob_allowlist_matches_and_non_matching_refused(monkeypatch, tmp_path):
    allow(monkeypatch, "team-*", "platform")
    spy = install_apply_spy(monkeypatch)
    ok = parse(run(server.plan_apply(namespace="team-b", manifest=SIMPLE_MANIFEST)))
    assert ok["ok"] is True and ok["summary"] == {"total": 1, "ok": 1, "failed": 0}
    ok2 = parse(run(server.plan_apply(namespace="platform", manifest=SIMPLE_MANIFEST)))
    assert ok2["ok"] is True
    refused = parse(run(server.plan_apply(namespace="random-ns", manifest=SIMPLE_MANIFEST)))
    assert refused["refused"] is True and "not matched by APPLYGATE_ALLOWED_NAMESPACES" in refused["error"]
    assert sorted(c["namespace"] for c in spy.calls) == ["platform", "team-b"]


def test_blocked_namespaces_always_win(monkeypatch, tmp_path):
    allow(monkeypatch, "team-*,platform")
    block(monkeypatch, "team-secrets,*-prod")
    spy = install_apply_spy(monkeypatch)

    ok = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE_MANIFEST)))
    assert ok["ok"] is True

    # Allowed by the glob, but the blocklist glob wins.
    refused = parse(run(server.apply_manifest(namespace="team-secrets", manifest=SIMPLE_MANIFEST, confirm_apply=True)))
    assert refused["refused"] is True and "blocklist always wins" in refused["error"]

    refused2 = parse(
        run(server.delete_resource(namespace="platform-prod", kind="ConfigMap", name="x", confirm_delete=True))
    )
    assert refused2["refused"] is True

    # Only the legitimate team-a plan reached the seam — nothing else did.
    assert [c["namespace"] for c in spy.calls] == ["team-a"]


# ---------------------------------------------------------------------------
# 2. Kind allowlist / cluster-scoped / Secret
# ---------------------------------------------------------------------------


def test_kind_not_on_allowlist_refused_with_allowlist_echoed(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    out = parse(
        run(server.apply_manifest(namespace="team-a", manifest=manifest(doc(kind="Pod", name="p")), confirm_apply=True))
    )
    assert out["documents"][0]["ok"] is False
    assert "not on the kind allowlist" in out["documents"][0]["message"]
    assert "APPLYGATE_ALLOWED_KINDS" in out["documents"][0]["message"]
    assert "HorizontalPodAutoscaler" in out["documents"][0]["message"]  # the echoed default list
    assert spy.calls == []


def test_cluster_scoped_kind_refused_even_if_allowlisted(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    kinds(monkeypatch, "Namespace,ClusterRole,ConfigMap")  # operator error: allowlisting them must not help
    spy = install_apply_spy(monkeypatch)
    out = parse(
        run(
            server.apply_manifest(
                namespace="team-a",
                manifest=manifest(doc(kind="ClusterRole", name="cr", api_version="rbac.authorization.k8s.io/v1")),
                confirm_apply=True,
            )
        )
    )
    assert out["documents"][0]["ok"] is False
    assert "cluster-scoped" in out["documents"][0]["message"]
    # Namespace too (plan path).
    out2 = parse(
        run(
            server.plan_apply(namespace="team-a", manifest=manifest(doc(kind="Namespace", name="ns", api_version="v1")))
        )
    )
    assert out2["documents"][0]["ok"] is False and "cluster-scoped" in out2["documents"][0]["message"]
    assert spy.calls == []


def test_secret_is_hard_refused_everywhere_even_if_allowlisted(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    kinds(monkeypatch, "Secret,ConfigMap")  # adding Secret to the env must not help
    install_apply_spy(monkeypatch)
    del_spy = install_delete_spy(monkeypatch)
    status_spy = install_status_fake(monkeypatch, {})

    secret_doc = doc(kind="Secret", name="creds", api_version="v1", type="Opaque", data={"p": "dw=="})
    m = manifest(secret_doc)

    plan = parse(run(server.plan_apply(namespace="team-a", manifest=m)))
    assert plan["documents"][0]["ok"] is False
    assert "never flow through this server" in plan["documents"][0]["message"]

    applied = parse(run(server.apply_manifest(namespace="team-a", manifest=m, confirm_apply=True)))
    assert applied["documents"][0]["ok"] is False and "Secret" in applied["documents"][0]["message"]

    deleted = parse(run(server.delete_resource(namespace="team-a", kind="Secret", name="creds", confirm_delete=True)))
    assert deleted["refused"] is True and "never flow through this server" in deleted["error"]

    status = parse(run(server.get_resource_status(namespace="team-a", kind="Secret", name="creds")))
    assert status["refused"] is True

    assert del_spy.calls == [] and status_spy.calls == []


def test_kind_allowlist_can_narrow_the_default_set(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    kinds(monkeypatch, "ConfigMap")
    spy = install_apply_spy(monkeypatch)
    out = parse(
        run(
            server.apply_manifest(
                namespace="team-a",
                manifest=manifest(doc(kind="Deployment", name="d", api_version="apps/v1")),
                confirm_apply=True,
            )
        )
    )
    assert out["documents"][0]["ok"] is False and "not on the kind allowlist" in out["documents"][0]["message"]
    ok = parse(run(server.apply_manifest(namespace="team-a", manifest=manifest(doc(name="c")), confirm_apply=True)))
    assert ok["ok"] is True
    assert len(spy.calls) == 1


def test_allowlisted_but_unknown_kind_refused(monkeypatch, tmp_path):
    """The env allowlist can narrow the registry, never widen it."""
    allow(monkeypatch, "team-a")
    kinds(monkeypatch, "ConfigMap,MyCustomThing")
    spy = install_apply_spy(monkeypatch)
    out = parse(
        run(
            server.plan_apply(
                namespace="team-a", manifest=manifest(doc(kind="MyCustomThing", name="x", api_version="example.com/v1"))
            )
        )
    )
    assert out["documents"][0]["ok"] is False
    assert "unknown to the built-in namespaced-kind registry" in out["documents"][0]["message"]
    assert spy.calls == []


# ---------------------------------------------------------------------------
# 3. Manifest hygiene: multi-doc, empty docs, mismatches, caps
# ---------------------------------------------------------------------------


def test_multi_doc_manifest_plans_and_applies_per_doc(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    m = manifest(
        doc(name="cm-1"),
        doc(kind="Service", name="svc-1", api_version="v1"),
        doc(kind="Job", name="j-1", api_version="batch/v1"),
    )
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=m, confirm_apply=True)))
    assert out["summary"] == {"total": 3, "ok": 3, "failed": 0}
    assert [d["name"] for d in out["documents"]] == ["cm-1", "svc-1", "j-1"]
    assert len(spy.calls) == 3


def test_empty_doc_between_documents_rejected(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    bad = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n---\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n"
    out = parse(run(server.plan_apply(namespace="team-a", manifest=bad)))
    assert out["refused"] is True
    assert "empty or not a YAML mapping" in out["error"] and "#2" in out["error"]
    assert spy.calls == []


def test_trailing_separator_tolerated_not_an_empty_doc(monkeypatch, tmp_path):
    """A trailing '---' is an end-of-docs marker, not a phantom empty doc."""
    allow(monkeypatch, "team-a")
    install_apply_spy(monkeypatch)
    m = yaml.safe_dump(doc(name="cm-1")) + "\n---\n"
    out = parse(run(server.plan_apply(namespace="team-a", manifest=m)))
    assert out["ok"] is True and out["summary"]["ok"] == 1


def test_docs_missing_required_fields_rejected(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)

    no_apiversion = {"kind": "ConfigMap", "metadata": {"name": "x"}}
    out = parse(run(server.plan_apply(namespace="team-a", manifest=manifest(no_apiversion))))
    assert out["refused"] is True and "apiVersion" in out["error"]

    no_kind = {"apiVersion": "v1", "metadata": {"name": "x"}}
    out = parse(run(server.plan_apply(namespace="team-a", manifest=manifest(no_kind))))
    assert out["refused"] is True and "kind" in out["error"]

    no_name = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {}}
    out = parse(run(server.plan_apply(namespace="team-a", manifest=manifest(no_name))))
    assert out["refused"] is True and "metadata.name" in out["error"]

    not_yaml = "{["  # unclosed flow sequence -> hard YAML parse error
    out = parse(run(server.plan_apply(namespace="team-a", manifest=not_yaml)))
    assert out["refused"] is True and "not valid YAML" in out["error"]

    assert spy.calls == []


def test_doc_namespace_must_equal_parameter(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    m = manifest(doc(name="cm-1", ns="other-ns"))
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=m, confirm_apply=True)))
    assert out["documents"][0]["ok"] is False
    assert "'other-ns'" in out["documents"][0]["message"] and "'team-a'" in out["documents"][0]["message"]
    assert spy.calls == []  # mismatched doc is never applied

    # Omitting metadata.namespace inherits the tool's parameter — fine.
    ok = parse(run(server.apply_manifest(namespace="team-a", manifest=manifest(doc(name="cm-2")), confirm_apply=True)))
    assert ok["ok"] is True and len(spy.calls) == 1


def test_oversized_manifest_rejected(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    big = manifest(doc(name="big", data={"blob": "x" * (256 * 1024)}))
    assert len(big.encode()) > 256 * 1024
    out = parse(run(server.plan_apply(namespace="team-a", manifest=big)))
    assert out["refused"] is True and "byte cap" in out["error"]
    assert spy.calls == []


def test_too_many_documents_rejected(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    nine = manifest(*[doc(name=f"cm-{i}") for i in range(9)])  # cap is 8
    out = parse(run(server.plan_apply(namespace="team-a", manifest=nine)))
    assert out["refused"] is True and "cap is 8" in out["error"]
    assert spy.calls == []
    # Exactly 8 is fine.
    eight = manifest(*[doc(name=f"cm-{i}") for i in range(8)])
    out = parse(run(server.plan_apply(namespace="team-a", manifest=eight)))
    assert out["summary"] == {"total": 8, "ok": 8, "failed": 0}


# ---------------------------------------------------------------------------
# 4. plan_apply — always dry-run, never mutates
# ---------------------------------------------------------------------------


def test_plan_always_dry_run_and_never_mutates(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.plan_apply(namespace="team-a", manifest=SIMPLE_MANIFEST, force=True)))
    assert out["dry_run"] is True and out["ok"] is True
    # Every seam call carried dry_run=True — that is the whole contract.
    assert spy.calls
    assert all(c["dry_run"] is True for c in spy.calls)
    assert all(c["namespace"] == "team-a" for c in spy.calls)
    assert "ALWAYS a dry-run" in out["note"]


def test_plan_reports_per_doc_failures_without_refusing_the_rest(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    install_apply_spy(monkeypatch, exc=Exception("api server exploded"))
    m = manifest(doc(name="cm-1"), doc(name="cm-2"))
    out = parse(run(server.plan_apply(namespace="team-a", manifest=m)))
    assert out["ok"] is False
    assert out["summary"] == {"total": 2, "ok": 0, "failed": 2}
    assert "api server exploded" in out["documents"][0]["message"]


# ---------------------------------------------------------------------------
# 5. apply_manifest — confirm gate
# ---------------------------------------------------------------------------


def test_apply_refuses_without_confirm(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE_MANIFEST)))
    assert out["refused"] is True
    assert "confirm_apply is False" in out["error"] and "plan_apply" in out["error"]
    assert spy.calls == []


def test_apply_proceeds_with_confirm_and_calls_seam_for_real(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE_MANIFEST, confirm_apply=True)))
    assert out["ok"] is True and out["documents"][0]["ok"] is True
    # Real apply: dry_run=False reaches the seam.
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["namespace"] == "team-a" and call["dry_run"] is False
    assert call["doc"]["metadata"]["name"] == "app-config"
    assert "resourceVersion=1234" in out["documents"][0]["message"]


# ---------------------------------------------------------------------------
# 6. delete_resource — gates
# ---------------------------------------------------------------------------


def test_delete_refuses_without_confirm(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_delete_spy(monkeypatch)
    out = parse(run(server.delete_resource(namespace="team-a", kind="ConfigMap", name="x")))
    assert out["refused"] is True and "confirm_delete is False" in out["error"]
    assert spy.calls == []


def test_delete_proceeds_with_confirm(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    spy = install_delete_spy(monkeypatch)
    out = parse(run(server.delete_resource(namespace="team-a", kind="ConfigMap", name="x", confirm_delete=True)))
    assert out["ok"] is True and out["deleted"]["name"] == "x"
    assert spy.calls == [{"namespace": "team-a", "kind": "ConfigMap", "name": "x"}]


def test_delete_kind_and_namespace_gates(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    block(monkeypatch, "team-a")  # blocked wins even for delete
    spy = install_delete_spy(monkeypatch)
    out = parse(run(server.delete_resource(namespace="team-a", kind="ConfigMap", name="x", confirm_delete=True)))
    assert out["refused"] is True and spy.calls == []

    monkeypatch.delenv("APPLYGATE_BLOCKED_NAMESPACES", raising=False)  # unblock, keep the allowlist
    # 'Pod' is namespaced in the registry but not on the default kind allowlist.
    out = parse(run(server.delete_resource(namespace="team-a", kind="Pod", name="p", confirm_delete=True)))
    assert out["refused"] is True and "allowlist" in out["error"]

    out = parse(
        run(server.delete_resource(namespace="team-a", kind="PersistentVolume", name="pv", confirm_delete=True))
    )
    assert out["refused"] is True and "cluster-scoped" in out["error"]
    assert spy.calls == []


# ---------------------------------------------------------------------------
# 7. get_resource_status — status shaping
# ---------------------------------------------------------------------------


def test_status_deployment_shape(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    obj = {
        "metadata": {"name": "web", "namespace": "team-a"},
        "status": {
            "replicas": 3,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "updatedReplicas": 3,
            "conditions": [{"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}],
        },
    }
    install_status_fake(monkeypatch, obj)
    out = parse(run(server.get_resource_status(namespace="team-a", kind="Deployment", name="web")))
    assert out["ok"] is True
    assert out["status"]["summary"] == {"replicas": 3, "ready": 2, "available": 2, "updated": 3}
    assert out["status"]["conditions"][0]["type"] == "Available"


def test_status_job_and_default_shapes(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    install_status_fake(monkeypatch, {"status": {"succeeded": 1, "failed": 0, "active": 0}})
    out = parse(run(server.get_resource_status(namespace="team-a", kind="Job", name="migrate")))
    assert out["status"]["summary"] == {"succeeded": 1, "failed": 0, "active": 0}

    install_status_fake(monkeypatch, {"status": {"phase": "Running"}})
    out = parse(run(server.get_resource_status(namespace="team-a", kind="Service", name="svc")))
    assert out["status"]["summary"] == {"phase": "Running"}


def test_status_lookup_failure_is_reported_not_raised(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    install_status_fake(monkeypatch, exc=Exception("404 not found"))
    out = parse(run(server.get_resource_status(namespace="team-a", kind="ConfigMap", name="ghost")))
    assert out["ok"] is False and "404 not found" in out["error"]


# ---------------------------------------------------------------------------
# 8. Audit trail (JSONL)
# ---------------------------------------------------------------------------


def test_audit_jsonl_written_for_plan_apply_delete(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    install_apply_spy(monkeypatch)
    install_delete_spy(monkeypatch)

    run(server.plan_apply(namespace="team-a", manifest=SIMPLE_MANIFEST))
    run(server.apply_manifest(namespace="team-a", manifest=SIMPLE_MANIFEST, confirm_apply=True))
    run(server.delete_resource(namespace="team-a", kind="ConfigMap", name="app-config", confirm_delete=True))

    lines = audit_lines(tmp_path)
    by = {(e["tool"], e["outcome"]): e for e in lines}
    assert ("plan_apply", "dry-run") in by
    assert ("apply_manifest", "applied") in by
    assert ("delete_resource", "deleted") in by

    plan_e = by[("plan_apply", "dry-run")]
    assert plan_e["dry_run"] is True and plan_e["namespace"] == "team-a"
    assert plan_e["kind"] == "ConfigMap" and plan_e["name"] == "app-config"
    apply_e = by[("apply_manifest", "applied")]
    assert apply_e["dry_run"] is False and apply_e["outcome"] == "applied"
    del_e = by[("delete_resource", "deleted")]
    assert del_e["kind"] == "ConfigMap" and del_e["name"] == "app-config"
    # Every entry carries the full base schema {ts, tool, namespace, kind,
    # name, dry_run, outcome} PLUS the additive hardening fields
    # (prev_sha256 hash chain, caller identity) — readers of the old
    # format stay compatible (new fields only; the chain itself is tested
    # in tests/test_audit_chain.py).
    for e in lines:
        assert {"ts", "tool", "namespace", "kind", "name", "dry_run", "outcome"} <= set(e)
        assert "prev_sha256" in e and "caller" in e
        assert e["ts"].endswith("Z")


def test_audit_one_line_per_document(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    install_apply_spy(monkeypatch)
    m = manifest(doc(name="cm-1"), doc(name="cm-2"))
    run(server.apply_manifest(namespace="team-a", manifest=m, confirm_apply=True))
    lines = [e for e in audit_lines(tmp_path) if e["tool"] == "apply_manifest"]
    assert sorted(e["name"] for e in lines) == ["cm-1", "cm-2"]
    assert all(e["outcome"] == "applied" for e in lines)


def test_audit_refusal_recorded(monkeypatch, tmp_path):
    install_apply_spy(monkeypatch)  # no namespace allowlist -> refused
    run(server.apply_manifest(namespace="nowhere", manifest=SIMPLE_MANIFEST, confirm_apply=True))
    lines = audit_lines(tmp_path)
    assert len(lines) == 1 and lines[0]["outcome"] == "refused" and lines[0]["tool"] == "apply_manifest"


# ---------------------------------------------------------------------------
# 9. Tool catalog sanity (annotations ship the guardrails to the harness)
# ---------------------------------------------------------------------------


def test_tool_catalog_and_hints():
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    assert set(tools) == {"plan_apply", "apply_manifest", "delete_resource", "get_resource_status"}
    hints = {name: t.annotations for name, t in tools.items()}
    assert hints["plan_apply"].read_only_hint is True
    assert hints["get_resource_status"].read_only_hint is True
    assert hints["apply_manifest"].read_only_hint is False
    assert hints["apply_manifest"].destructive_hint is True
    assert hints["delete_resource"].destructive_hint is True
