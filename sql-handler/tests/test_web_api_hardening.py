"""Route-level hardening tests for the web API (audit 2026-11-12, z4).

Covers the webui-side fixes: catalog caller scoping (P1-9), the always-on
web read-only guard + payload clamp (F3), security headers (F8), CSV
formula-injection neutralization (F9), filename sanitization (F11),
generic 500 bodies (F12), the SSO state cookie capture (F13), the
cross-site mutation guard (F15) and the highlight bounds (F16).

Fake engines are duck-typed recorders — no real engine or lake needed.
"""

import json

import pyarrow as pa
import pytest

from sqlhandler import oidc_sso
from sqlhandler import webui as webui_module
from sqlhandler.webui import (
    _arrow_to_csv_bytes,
    _HIGHLIGHT_MAX_CHARS,
    api_export,
    QueryJobManager,
    register_ui,
)


class RecordingEngine:
    """Duck-typed engine that records the kwargs routes forward."""

    def __init__(self):
        from types import SimpleNamespace

        self.calls = []
        self.tables = [pa.table({"a": [1, 2]})]
        self.provider = SimpleNamespace(kind="test")

    def list_tables(self, caller=None):
        self.calls.append(("list_tables", caller))
        raise RuntimeError("secret internal resolver path /srv/hidden")

    def catalog_content(self, fmt="yaml", *, caller=None):
        self.calls.append(("catalog_content", fmt, caller))
        return {"format": fmt, "content": "tables: {}"}

    def catalog_table_entry(self, table, fmt="yaml", *, caller=None):
        self.calls.append(("catalog_table_entry", table, fmt, caller))
        return {"table": table, "format": fmt, "content": ""}

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, row_cap=None, *, caller=None):
        self.calls.append(("query_duckdb", sql, limit, params, version_as_of, row_cap, caller))
        return pa.table({"one": [1]})

    def scan_arrow(self, table, limit=None, caller=None):
        self.calls.append(("scan_arrow", table, limit, caller))
        return self.tables[0]


@pytest.fixture()
def saved_store(tmp_path, monkeypatch):
    """A fresh saved-query store pointed at tmp (singleton reset both ends)."""
    from sqlhandler import saved as saved_module

    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    monkeypatch.delenv("SQLHANDLER_API_KEYS", raising=False)
    monkeypatch.delenv("SQLHANDLER_API_TOKEN", raising=False)
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved.json"))
    saved_module.reset_saved_query_store()
    yield tmp_path / "saved.json"
    saved_module.reset_saved_query_store()


def _make_client(eng, *, authed=False):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from starlette.applications import Starlette

    from sqlhandler.server import _CallerIdentityMiddleware

    app = Starlette()
    register_ui(app, lambda: eng)
    if authed:
        app.add_middleware(_CallerIdentityMiddleware)
    return TestClient(app)


@pytest.fixture()
def hard_client(monkeypatch):
    """TestClient over register_ui with the recording engine."""
    eng = RecordingEngine()
    return _make_client(eng), eng


@pytest.fixture()
def authed_client(monkeypatch):
    """Same app with the caller resolver forced to a sentinel (the
    test_policy_dispatch.py pattern) so caller-forwarding is observable."""
    from sqlhandler import identity

    eng = RecordingEngine()
    monkeypatch.setattr(identity, "caller_from_scope", lambda scope: SENTINEL_CALLER)
    return _make_client(eng, authed=True), eng


SENTINEL_CALLER = object()


# ---------------------------------------------------------------------------
# P1-9: catalog reads scope by caller
# ---------------------------------------------------------------------------


def test_catalog_content_route_forwards_caller(authed_client):
    client, eng = authed_client
    r = client.get("/api/semantic-catalog/content")
    assert r.status_code == 200, r.text
    kinds = [c for c in eng.calls if c[0] == "catalog_content"]
    assert kinds, "the route must ask the engine for the content"
    assert kinds[-1][2] is SENTINEL_CALLER, "the resolved caller must reach the engine"


def test_catalog_table_route_forwards_caller(authed_client):
    client, eng = authed_client
    r = client.get("/api/semantic-catalog/table?table=hr.salaries")
    assert r.status_code == 200, r.text
    kinds = [c for c in eng.calls if c[0] == "catalog_table_entry"]
    assert kinds and kinds[-1][1] == "hr.salaries"
    assert kinds[-1][3] is SENTINEL_CALLER, "the resolved caller must reach the engine"


def test_catalog_api_helpers_forward_caller_kwarg():
    """The api_* helpers accept (and forward) the caller kwarg directly."""
    from sqlhandler.webui import api_catalog_content, api_catalog_table

    eng = RecordingEngine()
    caller = object()
    api_catalog_content(eng, "yaml", caller=caller)
    api_catalog_table(eng, "t1", "yaml", caller=caller)
    assert eng.calls[-2][2] is caller
    assert eng.calls[-1][3] is caller


# ---------------------------------------------------------------------------
# F3: always-on web read-only guard + payload clamp
# ---------------------------------------------------------------------------


def _seed_store(store_dir, name, sql, params=None):
    store = store_dir / "saved.json"
    store.write_text(
        json.dumps({"version": 1, "queries": {name: {"sql": sql, "params": params}}}),
        encoding="utf-8",
    )
    return store


def test_saved_run_refuses_write_sql_even_with_d2_off(saved_store, monkeypatch, hard_client):
    """A hand-edited store cannot smuggle DDL through the WEB route even
    when the MCP D2 knob is off: the web surface's own guard still runs."""
    client, eng = hard_client
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    _seed_store(saved_store.parent, "bad", "DELETE FROM t")
    r = client.post("/api/saved-queries/bad/run", json={})
    assert r.status_code == 400, r.text
    assert "read-only" in r.json()["error"].lower()
    assert not [c for c in eng.calls if c[0] == "query_duckdb"], "nothing may execute"


def test_saved_run_clamps_limit_when_server_cap_disabled(saved_store, monkeypatch, hard_client):
    """With SQLHANDLER_MAX_ROWS=0 the UI fallback bound still applies to
    saved-run (FEATURES.md's 'results clamp ... even when the server cap is
    unlimited')."""
    client, eng = hard_client
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    _seed_store(saved_store.parent, "q", "SELECT 1 AS one")
    r = client.post("/api/saved-queries/q/run", json={"limit": 10 ** 9})
    assert r.status_code == 200, r.text
    runs = [c for c in eng.calls if c[0] == "query_duckdb"]
    assert runs and runs[-1][2] <= 1000, "the client limit must clamp to the UI cap"


def test_saved_run_bounds_unlimited_server_cap_when_no_limit_sent(saved_store, monkeypatch, hard_client):
    client, eng = hard_client
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    _seed_store(saved_store.parent, "q", "SELECT 1 AS one")
    r = client.post("/api/saved-queries/q/run", json={})
    assert r.status_code == 200, r.text
    runs = [c for c in eng.calls if c[0] == "query_duckdb"]
    assert runs and runs[-1][2] == 1000, "a disabled server cap must fall back to the UI bound"


def test_jobs_route_refuses_write_sql_even_with_d2_off(monkeypatch, hard_client):
    """POST /api/jobs applies the always-on web guard BEFORE submitting.
    The MCP tool path (server.query_submit) is unchanged — this is the route."""
    client, eng = hard_client
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    submitted = []

    def spy_submit(engine, body, *, caller=None, owner=None, caller_scope=None):
        submitted.append(body)
        return {"job_id": "j1", "state": "running"}

    monkeypatch.setattr(webui_module, "api_job_submit", spy_submit)
    r = client.post("/api/jobs", json={"sql": "DROP TABLE t"})
    assert r.status_code == 400, r.text
    assert not submitted, "nothing may be submitted"


# ---------------------------------------------------------------------------
# F8: security headers
# ---------------------------------------------------------------------------


def test_ui_page_carries_security_headers(hard_client):
    client, _ = hard_client
    r = client.get("/ui")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp


def test_api_responses_carry_nosniff(hard_client):
    client, _ = hard_client
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------
# F15: cross-site mutation guard
# ---------------------------------------------------------------------------


def test_cross_site_origin_is_refused_on_mutations(hard_client):
    client, eng = hard_client
    r = client.post(
        "/api/query",
        json={"sql": "SELECT 1"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403, r.text
    assert not [c for c in eng.calls if c[0] == "query_duckdb"]


def test_same_site_origin_passes(hard_client):
    client, eng = hard_client
    r = client.post(
        "/api/query",
        json={"sql": "SELECT 1"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text


def test_no_origin_no_sec_fetch_passes(hard_client):
    """curl / MCP agents / tests send neither header — never refused."""
    client, _ = hard_client
    assert client.post("/api/query", json={"sql": "SELECT 1"}).status_code == 200


def test_sec_fetch_site_cross_is_refused(hard_client):
    client, _ = hard_client
    r = client.post(
        "/api/query",
        json={"sql": "SELECT 1"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403


def test_csrf_guard_env_escape(hard_client, monkeypatch):
    client, _ = hard_client
    monkeypatch.setenv("SQLHANDLER_CSRF_ORIGIN_CHECK", "0")
    r = client.post(
        "/api/query",
        json={"sql": "SELECT 1"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 200


def test_cross_site_logout_refused(hard_client):
    client, _ = hard_client
    r = client.get("/oauth/logout", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# F9 / F11: export hardening
# ---------------------------------------------------------------------------


def test_csv_export_neutralizes_formula_prefixes():
    import csv

    table = pa.table({"s": ["=SUM(A1)", "+cmd", "-note", "@x", "plain", None], "n": [1, -2, 3, 4, 5, 6]})
    out = _arrow_to_csv_bytes(table).decode("utf-8")
    rows = list(csv.reader(out.strip().splitlines()))
    assert rows[0] == ["s", "n"]
    # every dangerous string cell is neutralized with the leading apostrophe
    # (the writer legitimately quotes cells whose value contains a quote)
    assert rows[1][0] == "'=SUM(A1)"
    assert rows[2][0] == "'+cmd"
    assert rows[3][0] == "'-note"
    assert rows[4][0] == "'@x"
    assert rows[5][0] == "plain", "benign cells are untouched"
    assert rows[6][0] == "", "null string cell stays empty"
    # numeric columns are untouched: the -2 in the int column is NOT neutralized
    assert rows[2][1] == "-2"


def test_export_filename_is_sanitized():
    eng = RecordingEngine()
    result = api_export(eng, {"table": 'we"ird/na me', "format": "csv"})
    fn = result["filename"]
    assert '"' not in fn and "/" not in fn
    assert fn.endswith(".csv")


# ---------------------------------------------------------------------------
# F12: generic 500 bodies
# ---------------------------------------------------------------------------


def test_internal_error_body_is_generic(hard_client):
    client, _ = hard_client
    r = client.get("/api/tables")
    assert r.status_code == 500
    body = r.json()["error"]
    assert "secret internal resolver path" not in body
    assert "server logs" in body


# ---------------------------------------------------------------------------
# F13: SSO state cookie comes from the generated state, never URL splitting
# ---------------------------------------------------------------------------


def test_oauth_login_plants_the_generated_state(monkeypatch, hard_client):
    client, _ = hard_client
    captured = {}

    def fake_build(state):
        captured["state"] = state
        return "https://idp.example/authorize?client_id=x&state=DECOY-IN-URL"

    monkeypatch.setattr(oidc_sso, "sso_enabled", lambda: True)
    monkeypatch.setattr(oidc_sso, "build_authorization_url", fake_build)
    r = client.get("/oauth/login?next=/ui", follow_redirects=False)
    assert r.status_code == 302
    cookie = r.headers.get("set-cookie", "")
    assert captured["state"] and captured["state"] in cookie, "the GENERATED state must be planted"
    assert "DECOY-IN-URL" not in cookie.split("|")[0], "the URL must never be text-parsed for state"


# ---------------------------------------------------------------------------
# F14: QueryJobManager eviction still works under the new lock
# ---------------------------------------------------------------------------


def test_query_job_manager_cleanup_under_lock(monkeypatch):
    class DoneJob:
        state = "done"

    mgr = QueryJobManager(max_jobs=2)
    monkeypatch.setenv("SQLHANDLER_ASYNC_JOB_TTL", "0")  # evict immediately
    for i in range(3):
        mgr._jobs[str(i)] = (DoneJob(), None, None, 0.0)  # finished long ago
    mgr._cleanup()
    assert len(mgr._jobs) <= 2, "the cap is enforced through the locked path"


# ---------------------------------------------------------------------------
# F16: highlight bounds
# ---------------------------------------------------------------------------


def test_highlight_refuses_over_cap():
    out = webui_module.api_highlight({"text": "a" * (_HIGHLIGHT_MAX_CHARS + 1)})
    assert out["highlighted"] is False and out["html"] is None


def test_highlight_small_text_still_highlights():
    out = webui_module.api_highlight({"text": "tables:\\n  t1: doc\n", "format": "yaml"})
    assert out["highlighted"] is True
    assert "tok-key" in out["html"] or out["html"].count("<span") >= 1
