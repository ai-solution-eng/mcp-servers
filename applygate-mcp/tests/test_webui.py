"""Unit tests for the applygate-mcp web UI + JSON API — the read-only console.

The UI routes are built with the SAME seam-monkeypatching pattern as the MCP
tool tests (tests/test_applygate.py): the test venv has NO `kubernetes`
package, and every k8s touch goes through the module-level seam fakes. The
web console is driven over Starlette's TestClient and asserts the two
properties that make it safe to expose:

  1. it can NEVER mutate the cluster — no apply/delete endpoints exist, and
     the plan endpoint always rides the always-dry-run tool path (the seam
     only ever sees dry_run=True, no matter what the HTTP body says);
  2. it never reads outside its configuration — the audit endpoint reads
     ONLY the configured APPLYGATE_AUDIT_FILE and refuses client-selected
     paths outright.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/ -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

import server
import webui


# ---------------------------------------------------------------------------
# Fixtures / helpers (same conventions as tests/test_applygate.py)
# ---------------------------------------------------------------------------

ENV_VARS = (
    "APPLYGATE_ALLOWED_NAMESPACES",
    "APPLYGATE_BLOCKED_NAMESPACES",
    "APPLYGATE_ALLOWED_KINDS",
    "APPLYGATE_AUDIT_FILE",
    "APPLYGATE_WEBUI_ENABLED",
    "APPLYGATE_WEBUI_HTML",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """No ambient policy, console enabled by default, own audit sink."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))


def allow(monkeypatch, *patterns):
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", ",".join(patterns))


def block(monkeypatch, *patterns):
    monkeypatch.setenv("APPLYGATE_BLOCKED_NAMESPACES", ",".join(patterns))


def kinds(monkeypatch, *names):
    monkeypatch.setenv("APPLYGATE_ALLOWED_KINDS", ",".join(names))


class _SeamSpy:
    """Recording fake for a k8s seam function; optionally raises."""

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


SIMPLE_MANIFEST = manifest(doc(name="app-config", data={"k": "v"}))


def make_client() -> TestClient:
    """The console over the REAL tool bodies (seams monkeypatched per-test)."""
    return TestClient(Starlette(routes=webui.build_ui_routes()))


# ---------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------


def test_ui_serves_hpe_branding_tabs_and_trust_banner():
    c = make_client()
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    assert "Hewlett Packard Enterprise" in html
    assert "hpe-element" in html  # the green parallelogram mark
    assert "applygate-theme" in html  # no-flash theme script + persistence
    assert "ApplyGate MCP" in html
    for tab in ("Plan", "Status", "Audit", "Policy"):
        assert tab in html
    # The exact trust-model sentence, as one contiguous string:
    assert (
        "Read-only console — mutations happen only through the MCP tools "
        "with explicit confirm flags, behind gateway authn."
    ) in html
    assert c.get("/ui").status_code == 200
    assert c.get("/ui").text == html


def test_ui_html_fallback_when_asset_missing(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(webui, "_HTML_CANDIDATES", (Path("/nonexistent/ui/index.html"),))
    html = webui._load_html()
    assert "UI asset not found" in html
    assert "APPLYGATE_WEBUI_HTML" in html


def test_ui_has_no_duplicate_element_ids():
    # getElementById silently resolves to the FIRST match — duplicated ids
    # would leave one of the two panels rendering "—" forever.
    import re

    ids = re.findall(r'id="([^"]+)"', webui._load_html())
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate element ids: {sorted(dupes)}"


def test_ui_contains_no_mutation_switches():
    """The console HTML never carries a confirm/apply/delete switch: even a
    hand-crafted request has no UI surface to flip."""
    html = webui._load_html()
    for forbidden in ("confirm_apply", "confirm_delete", "/api/apply", "/api/delete"):
        assert forbidden not in html


# ---------------------------------------------------------------------------
# the API surface is strictly read-only
# ---------------------------------------------------------------------------


def test_api_surface_is_strictly_read_only():
    routes = webui.build_ui_routes()
    paths = {r.path for r in routes}
    assert paths == {
        "/",
        "/ui",
        "/api/status",
        "/api/policy",
        "/api/plan",
        "/api/resource_status",
        "/api/audit",
    }
    # THE hard rule: no mutating endpoint exists — not even a gated one.
    for r in routes:
        for token in ("apply", "delete", "confirm"):
            assert token not in r.path.lower()
    methods = {r.path: sorted(r.methods or {"GET"}) for r in routes}
    assert methods["/api/plan"] == ["POST"]  # the plan console
    for p in paths:
        if p != "/api/plan":
            assert "POST" not in methods[p]


# ---------------------------------------------------------------------------
# plan endpoint — the tool's ALWAYS-dry-run code path
# ---------------------------------------------------------------------------


def test_plan_endpoint_returns_dry_run_results_and_seam_stays_dry_run(monkeypatch):
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    c = make_client()
    r = c.post("/api/plan", json={"namespace": "team-a", "manifest": SIMPLE_MANIFEST})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["dry_run"] is True
    assert data["documents"][0]["ok"] is True
    assert data["summary"] == {"total": 1, "ok": 1, "failed": 0}
    # THE contract: every seam call carried dry_run=True — the UI can never
    # flip it (no field in the request body even reaches the tool body).
    assert spy.calls and all(call["dry_run"] is True for call in spy.calls)


def test_plan_endpoint_ignores_client_mutation_flags(monkeypatch):
    """Posting dry_run=false / confirm_apply=true / force=true changes nothing:
    the plan path is a dry-run by construction, not by request parameter."""
    allow(monkeypatch, "team-a")
    spy = install_apply_spy(monkeypatch)
    c = make_client()
    r = c.post(
        "/api/plan",
        json={
            "namespace": "team-a",
            "manifest": SIMPLE_MANIFEST,
            "dry_run": False,
            "confirm_apply": True,
            "force": True,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["dry_run"] is True
    assert spy.calls and all(call["dry_run"] is True for call in spy.calls)
    assert all("dry_run" not in call or call["dry_run"] is True for call in spy.calls)


def test_plan_endpoint_refusals_use_the_exact_tool_strings(monkeypatch):
    install_apply_spy(monkeypatch)
    c = make_client()

    # 1. denied namespace (default-deny — allowlist unset):
    r = c.post("/api/plan", json={"namespace": "team-a", "manifest": SIMPLE_MANIFEST})
    assert r.status_code == 200
    data = r.json()
    assert data["refused"] is True and data["ok"] is False
    assert "DEFAULT-DENY" in data["error"] and "APPLYGATE_ALLOWED_NAMESPACES" in data["error"]

    # 2. refused kind:
    allow(monkeypatch, "team-a")
    r = c.post("/api/plan", json={"namespace": "team-a", "manifest": manifest(doc(kind="Pod", name="p"))})
    data = r.json()
    # a per-doc kind refusal is a tool result (ok=False), not a tool-level refusal
    assert data["ok"] is False and "refused" not in data
    doc_result = data["documents"][0]
    assert doc_result["ok"] is False
    assert "not on the kind allowlist" in doc_result["message"]

    # 3. unparseable YAML:
    r = c.post("/api/plan", json={"namespace": "team-a", "manifest": "{["})
    data = r.json()
    assert data["refused"] is True and "not valid YAML" in data["error"]


def test_plan_endpoint_validates_the_body():
    c = make_client()
    assert c.post("/api/plan", json={}).status_code == 400
    assert c.post("/api/plan", json={"manifest": "a: 1"}).status_code == 400  # no namespace
    assert c.post("/api/plan", json={"namespace": "team-a"}).status_code == 400  # no manifest
    assert c.post("/api/plan", json={"namespace": "team-a", "manifest": "   "}).status_code == 400
    assert c.post("/api/plan", json={"namespace": "team-a", "manifest": ["not", "a", "string"]}).status_code == 400
    r = c.post("/api/plan", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# resource_status endpoint — same guardrails as the tools
# ---------------------------------------------------------------------------


def test_status_endpoint_honors_namespace_policy_and_kind_gates(monkeypatch):
    install_status_fake(monkeypatch, {"status": {"phase": "Running"}})
    c = make_client()
    url = "/api/resource_status?namespace=team-a&kind=ConfigMap&name=x"

    # 1. default-deny: no allowlist -> the exact tool refusal, seam untouched.
    r = c.get(url)
    assert r.status_code == 200
    data = r.json()
    assert data["refused"] is True
    assert "DEFAULT-DENY" in data["error"]
    assert server._get_status.calls == []  # type: ignore[attr-defined]

    # 2. blocked namespace wins over an allowlist.
    allow(monkeypatch, "team-*")
    block(monkeypatch, "team-secrets")
    r = c.get("/api/resource_status?namespace=team-secrets&kind=ConfigMap&name=x")
    data = r.json()
    assert data["refused"] is True and "blocklist always wins" in data["error"]

    # 3. Secret stays hard-refused through the UI too.
    kinds(monkeypatch, "Secret,ConfigMap")
    r = c.get("/api/resource_status?namespace=team-a&kind=Secret&name=creds")
    data = r.json()
    assert data["refused"] is True and "never flow through this server" in data["error"]

    # 4. a legit lookup flows through to the (faked) seam.
    r = c.get(url)
    data = r.json()
    assert data["ok"] is True
    assert data["status"]["summary"] == {"phase": "Running"}
    calls = server._get_status.calls  # type: ignore[attr-defined]
    assert calls == [{"namespace": "team-a", "kind": "ConfigMap", "name": "x"}]


def test_status_endpoint_requires_all_params():
    c = make_client()
    assert c.get("/api/resource_status").status_code == 400
    assert c.get("/api/resource_status?namespace=team-a&kind=ConfigMap").status_code == 400
    r = c.get("/api/resource_status?namespace=team-a&name=x")
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


def test_status_endpoint_dependency_injection():
    """build_ui_routes accepts stub bodies (same DI pattern as prometheus)."""

    def fake_plan(namespace, manifest):
        return json.dumps({"ok": True, "dry_run": True, "stub": namespace})

    def fake_status(namespace, kind, name):
        return json.dumps({"ok": True, "stub": f"{kind}/{name}@{namespace}"})

    c = TestClient(Starlette(routes=webui.build_ui_routes(plan_fn=fake_plan, status_fn=fake_status)))
    assert c.post("/api/plan", json={"namespace": "ns", "manifest": "a: 1"}).json()["stub"] == "ns"
    got = c.get("/api/resource_status?namespace=ns&kind=Job&name=j").json()
    assert got["stub"] == "Job/j@ns"


# ---------------------------------------------------------------------------
# audit endpoint — reads ONLY the configured file
# ---------------------------------------------------------------------------


def test_audit_endpoint_parses_the_configured_file(monkeypatch, tmp_path):
    server._audit("plan_apply", "team-a", "ConfigMap", "app-config", True, "dry-run")
    server._audit("apply_manifest", "team-a", "ConfigMap", "app-config", False, "applied")
    server._audit("delete_resource", "team-a", "Service", "svc", False, "deleted")

    c = make_client()
    r = c.get("/api/audit")
    assert r.status_code == 200
    data = r.json()
    assert data["file"] == str(tmp_path / "audit.jsonl")
    assert data["exists"] is True
    assert data["n_total"] == 3 and data["n_shown"] == 3 and data["malformed"] == 0
    assert [e["tool"] for e in data["entries"]] == ["plan_apply", "apply_manifest", "delete_resource"]
    assert data["entries"][0]["outcome"] == "dry-run"
    assert data["entries"][-1]["outcome"] == "deleted"


def test_audit_endpoint_tails_last_n_lines(monkeypatch, tmp_path):
    for i in range(6):
        server._audit("plan_apply", "team-a", "ConfigMap", f"cm-{i}", True, "dry-run")
    c = make_client()
    data = c.get("/api/audit", params={"lines": 2}).json()
    assert data["n_total"] == 6 and data["n_shown"] == 2
    assert [e["name"] for e in data["entries"]] == ["cm-4", "cm-5"]  # the LAST two
    # lines beyond the cap clamp to _MAX_AUDIT_LINES
    data = c.get("/api/audit", params={"lines": 100000}).json()
    assert data["n_shown"] == 6
    # invalid values are a 400, not a silent default
    assert c.get("/api/audit", params={"lines": "abc"}).status_code == 400
    assert c.get("/api/audit", params={"lines": "0"}).status_code == 400
    assert c.get("/api/audit", params={"lines": "-5"}).status_code == 400


def test_audit_endpoint_refuses_path_traversal(monkeypatch):
    """A client-selected path is refused outright — the endpoint reads ONLY
    the configured APPLYGATE_AUDIT_FILE, whatever traversal is attempted."""
    c = make_client()
    for attempt in (
        {"file": "/etc/passwd"},
        {"path": "../../etc/passwd"},
        {"path": "/etc/shadow"},
        {"audit_file": "/etc/passwd"},
        {"auditFile": "/etc/passwd"},
        {"file": str(__file__)},  # even "harmless" reads of repo files are refused
    ):
        r = c.get("/api/audit", params=attempt)
        assert r.status_code == 400, f"{attempt} must be refused"
        assert "APPLYGATE_AUDIT_FILE" in r.json()["error"]
    # ...and the normal call still reports the CONFIGURED path only.
    data = c.get("/api/audit").json()
    assert data["file"] == os.environ["APPLYGATE_AUDIT_FILE"]
    assert data["file"] != "/etc/passwd"


def test_audit_endpoint_missing_file_is_an_empty_trail_not_an_error(monkeypatch, tmp_path):
    c = make_client()
    data = c.get("/api/audit").json()
    assert data["exists"] is False and data["entries"] == [] and data["n_total"] == 0


def test_audit_endpoint_skips_malformed_lines(monkeypatch, tmp_path):
    p = tmp_path / "audit.jsonl"
    good = {"ts": "2026-01-01T00:00:00Z", "tool": "plan_apply", "namespace": "team-a",
            "kind": "ConfigMap", "name": "x", "dry_run": True, "outcome": "dry-run"}
    p.write_text(
        json.dumps(good) + "\nnot json at all\n" + json.dumps({"not": "an audit entry shape"}) + "\n"
    )
    c = make_client()
    data = c.get("/api/audit").json()
    # the unparseable line AND the wrong-shape dict both count as malformed —
    # the table only ever renders real audit entries
    assert data["n_total"] == 3 and data["n_shown"] == 1 and data["malformed"] == 2
    assert data["entries"][0]["tool"] == "plan_apply"


# ---------------------------------------------------------------------------
# policy endpoint — the effective policy from the same config functions
# ---------------------------------------------------------------------------


def test_policy_endpoint_reflects_the_effective_policy(monkeypatch):
    allow(monkeypatch, "team-*", "platform")
    block(monkeypatch, "team-secrets")
    kinds(monkeypatch, "ConfigMap,Service")
    c = make_client()
    data = c.get("/api/policy").json()
    assert data["allowed_namespaces"] == ["team-*", "platform"]
    assert data["blocked_namespaces"] == ["team-secrets"]
    assert data["allowed_kinds"] == ["ConfigMap", "Service"]
    assert data["namespaces_enabled"] is True and data["default_deny"] is False
    assert data["kinds_from_env"] is True

    # The hard-refusal texts are the REAL strings the guard function raises:
    try:
        server._check_kind("Secret")
        raised = ""
    except server._Refusal as exc:
        raised = str(exc)
    assert data["hard_refusals"]["secret"] == raised
    assert "never flow through this server" in data["hard_refusals"]["secret"]


def test_policy_endpoint_default_deny_shape(monkeypatch):
    c = make_client()
    data = c.get("/api/policy").json()
    assert data["allowed_namespaces"] == []
    assert data["namespaces_enabled"] is False and data["default_deny"] is True
    # With no env override the panel shows the built-in default kind set.
    assert data["allowed_kinds"] == server.DEFAULT_ALLOWED_KINDS.split(",")
    assert data["kinds_from_env"] is False


# ---------------------------------------------------------------------------
# status endpoint + server wiring (webui.enabled gate)
# ---------------------------------------------------------------------------


def test_api_status_reports_namespaces_enabled_and_caps(monkeypatch):
    allow(monkeypatch, "team-a")
    c = make_client()
    data = c.get("/api/status").json()
    assert data["status"] == "ok" and data["server"] == "applygate-mcp"
    assert data["namespaces_enabled"] is True
    assert data["field_manager"] == server.FIELD_MANAGER
    assert data["audit_file"] == os.environ["APPLYGATE_AUDIT_FILE"]
    assert data["caps"]["max_docs"] == server.MAX_DOCS
    assert data["caps"]["max_audit_lines"] == webui._MAX_AUDIT_LINES


def test_server_mounts_ui_routes_by_default():
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {
        "/", "/ui", "/api/status", "/api/policy", "/api/plan",
        "/api/resource_status", "/api/audit", "/health", "/healthz", "/mcp",
    } <= paths


def test_webui_enabled_false_removes_ui_routes_but_keeps_mcp(monkeypatch):
    monkeypatch.setenv("APPLYGATE_WEBUI_ENABLED", "false")
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" not in paths and "/ui" not in paths
    assert not any(str(p).startswith("/api/") for p in paths if p)
    # /mcp and the health endpoints keep working.
    assert {"/mcp", "/health", "/healthz"} <= paths

    c = TestClient(app)
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/").status_code == 404
    assert c.get("/api/audit").status_code == 404


def test_webui_enabled_flag_parsing():
    # unset -> enabled (the chart ships webui.enabled: true)
    assert webui.webui_enabled({}) is True
    for on in ("true", "TRUE", "1", "yes", "on", "enabled"):
        assert webui.webui_enabled({"APPLYGATE_WEBUI_ENABLED": on}) is True, on
    for off in ("false", "FALSE", "0", "no", "off", "disabled"):
        assert webui.webui_enabled({"APPLYGATE_WEBUI_ENABLED": off}) is False, off
