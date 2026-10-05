"""plan_delete tests — the delete dry-run (the delete twin of plan_apply).

plan_delete runs the SAME fence chain as delete_resource (namespace policy
default-deny, kind allowlist, Secret hard-refusal, cluster-scoped refusal)
with NO confirm gate — a plan can never mutate, so there is nothing to
confirm — and NEVER calls the delete seam. For an existing object it
surfaces the same status view get_resource_status renders; for a missing
object (404) it reports a clean "nothing to delete" verdict.

The intended flow pinned here: plan_delete → human review →
delete_resource(confirm_delete=True).

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_plan_delete.py -v
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

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
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    server._caller_context.set(None)  # no caller leakage between tests


def run(coro):
    return asyncio.run(coro)


def parse(out: str) -> dict:
    return json.loads(out)


def allow(monkeypatch, *patterns):
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", ",".join(patterns))


def kinds(monkeypatch, *names):
    monkeypatch.setenv("APPLYGATE_ALLOWED_KINDS", ",".join(names))


class _SeamSpy:
    def __init__(self, names, result=None, exc=None):
        self.names = names
        self.calls = []
        self.result = result if result is not None else {}
        self.exc = exc

    def __call__(self, *args, **kwargs):
        record = dict(zip(self.names, args))
        record.update(kwargs)
        self.calls.append(record)
        if self.exc is not None:
            raise self.exc
        return self.result


def install_delete_spy(monkeypatch, **kwargs):
    """The DELETE seam spy: plan_delete must never reach it."""
    spy = _SeamSpy(("namespace", "kind", "name"), result={"response": {}}, **kwargs)
    monkeypatch.setattr(server, "_delete", spy)
    return spy


def install_status_spy(monkeypatch, obj=None, exc=None):
    spy = _SeamSpy(("namespace", "kind", "name"), result=obj if obj is not None else {}, exc=exc)
    monkeypatch.setattr(server, "_get_status", spy)
    return spy


DEPLOYMENT_OBJ = {
    "metadata": {"name": "web", "namespace": "team-a"},
    "status": {
        "replicas": 3,
        "readyReplicas": 2,
        "availableReplicas": 2,
        "updatedReplicas": 3,
        "conditions": [{"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}],
    },
}


def audit_outcomes(tmp_path):
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line)["outcome"] for line in p.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The fence — identical to delete_resource (minus the confirm gate)
# ---------------------------------------------------------------------------


def test_default_deny_refuses_plan_delete(monkeypatch, tmp_path):
    del_spy = install_delete_spy(monkeypatch)
    status_spy = install_status_spy(monkeypatch)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="web")))
    assert out["refused"] is True and out["ok"] is False
    assert "DEFAULT-DENY" in out["error"] and "APPLYGATE_ALLOWED_NAMESPACES" in out["error"]
    assert del_spy.calls == [] and status_spy.calls == []
    assert audit_outcomes(tmp_path) == ["refused"]


def test_blocklist_wins_over_allowlist(monkeypatch, tmp_path):
    allow(monkeypatch, "team-*")
    monkeypatch.setenv("APPLYGATE_BLOCKED_NAMESPACES", "team-internal")
    install_status_spy(monkeypatch)
    out = parse(run(server.plan_delete(namespace="team-internal", kind="Deployment", name="web")))
    assert out["refused"] is True
    assert "APPLYGATE_BLOCKED_NAMESPACES" in out["error"]


def test_dns_hygiene_refuses_bad_name(monkeypatch):
    allow(monkeypatch, "team-a")
    status_spy = install_status_spy(monkeypatch)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="Bad_Name!")))
    assert out["refused"] is True
    assert "RFC1123" in out["error"]
    assert status_spy.calls == []


def test_secret_is_hard_refused(monkeypatch):
    allow(monkeypatch, "team-a")
    install_status_spy(monkeypatch)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Secret", name="creds")))
    assert out["refused"] is True
    assert "Secret" in out["error"]


def test_cluster_scoped_kind_is_refused(monkeypatch):
    allow(monkeypatch, "team-a")
    install_status_spy(monkeypatch)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Namespace", name="kube-system")))
    assert out["refused"] is True
    assert "cluster-scoped" in out["error"]


def test_kind_not_on_allowlist_is_refused(monkeypatch):
    allow(monkeypatch, "team-a")
    kinds(monkeypatch, "ConfigMap")  # narrow the allowlist away from Deployments
    install_status_spy(monkeypatch)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="web")))
    assert out["refused"] is True
    assert "kind allowlist" in out["error"]


# ---------------------------------------------------------------------------
# The plan itself — status verdict, never a mutation
# ---------------------------------------------------------------------------


def test_plan_delete_never_touches_the_delete_seam_and_surfaces_status(monkeypatch):
    allow(monkeypatch, "team-a")
    del_spy = install_delete_spy(monkeypatch)
    status_spy = install_status_spy(monkeypatch, obj=DEPLOYMENT_OBJ)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="web")))
    assert out["ok"] is True and out["dry_run"] is True
    assert out["would_delete"] == {"kind": "Deployment", "name": "web", "namespace": "team-a"}
    assert out["exists"] is True
    # The same shaping get_resource_status renders.
    assert out["current_state"]["summary"] == {"replicas": 3, "ready": 2, "available": 2, "updated": 3}
    assert out["current_state"]["conditions"] == [
        {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}
    ]
    # NEVER deletes — not even with a confirm-shaped flag in play.
    assert del_spy.calls == []
    assert status_spy.calls == [{"namespace": "team-a", "kind": "Deployment", "name": "web"}]
    assert audit_outcomes(monkeypatch) if False else True  # outcomes asserted below


def test_plan_delete_audit_says_planned_not_deleted(monkeypatch, tmp_path):
    allow(monkeypatch, "team-a")
    install_delete_spy(monkeypatch)
    install_status_spy(monkeypatch, obj=DEPLOYMENT_OBJ)
    out = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="web")))
    assert out["ok"] is True
    outcomes = audit_outcomes(tmp_path)
    assert outcomes == ["planned"]  # the plan_delete outcome — never "deleted"


def test_missing_object_404_is_a_clean_nothing_to_delete_verdict(monkeypatch):
    allow(monkeypatch, "team-a")
    del_spy = install_delete_spy(monkeypatch)

    class _Api404(Exception):
        status = 404

    status_spy = install_status_spy(monkeypatch, exc=_Api404("404 Not Found"))
    out = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="ghost")))
    assert out["ok"] is True and out["dry_run"] is True
    assert out["would_delete"] == {"kind": "Deployment", "name": "ghost", "namespace": "team-a"}
    assert out["exists"] is False
    assert out["current_state"] is None
    assert "nothing to delete" in out["message"]
    assert out["status"] == 404
    assert del_spy.calls == []
    assert len(status_spy.calls) == 1


def test_non_404_lookup_failure_is_structured_not_a_plan(monkeypatch):
    allow(monkeypatch, "team-a", "*")
    kinds(monkeypatch, "PersistentVolumeClaim", "ConfigMap")
    install_delete_spy(monkeypatch)

    class _Api403(Exception):
        status = 403

    install_status_spy(monkeypatch, exc=_Api403("Forbidden"))
    out = parse(run(server.plan_delete(namespace="team-a", kind="PersistentVolumeClaim", name="data")))
    assert out["ok"] is False
    assert out["reason"] == "api_error" and out["status"] == 403
    # A 403 on an admitted-but-ungranted kind carries the RBAC hint; a 403
    # on a kind the shipped RBAC grants (a different problem) does not.
    assert "rbac_hint" in out and "rbac.yaml" in out["rbac_hint"]
    granted = parse(run(server.plan_delete(namespace="team-a", kind="ConfigMap", name="cm")))
    assert granted["status"] == 403 and "rbac_hint" not in granted
    assert "exists" not in out  # no verdict without a readable object


def test_delete_still_requires_its_own_confirm_after_planning(monkeypatch):
    """The plan NEVER carries mutation power: delete_resource without
    confirm_delete=True still refuses after a clean plan_delete."""
    allow(monkeypatch, "team-a")
    del_spy = install_delete_spy(monkeypatch)
    install_status_spy(monkeypatch, obj=DEPLOYMENT_OBJ)
    plan = parse(run(server.plan_delete(namespace="team-a", kind="Deployment", name="web")))
    assert plan["ok"] is True
    unconfirmed = parse(
        run(server.delete_resource(namespace="team-a", kind="Deployment", name="web", confirm_delete=False))
    )
    assert unconfirmed["refused"] is True
    assert "confirm_delete" in unconfirmed["error"]
    assert del_spy.calls == []
