"""The /api explicit-key rung (audit 2026-11-12 cross-validation, P1).

With the chart default SQLHANDLER_REQUIRE_IDENTITY=1, a caller who presents a
VALID key must be authenticated on /api/* - the documented "paste an API key"
recovery. Before the rung existed, /api/whoami said authenticated=True while
/api/tables 401'd with the same key.
"""

import pytest
from starlette.testclient import TestClient

from sqlhandler.identity import key_fp
from sqlhandler.server import _build_http_app

KEY = "audit-explicit-key-0123456789"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", KEY)
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    monkeypatch.delenv("SQLHANDLER_API_TOKEN", raising=False)
    monkeypatch.setenv("SQLHANDLER_CACHE_TTL", "0")
    return _build_http_app()


def test_api_accepts_x_api_key(app):
    with TestClient(app) as c:
        r = c.get("/api/tables", headers={"X-API-Key": KEY})
    assert r.status_code != 401, r.text


def test_api_accepts_x_api_token(app):
    with TestClient(app) as c:
        r = c.get("/api/tables", headers={"X-API-Token": KEY})
    assert r.status_code != 401, r.text


def test_api_accepts_static_bearer(app):
    with TestClient(app) as c:
        r = c.get("/api/tables", headers={"Authorization": "Bearer " + KEY})
    assert r.status_code != 401, r.text


def test_api_refuses_anonymous_and_wrong_key(app):
    with TestClient(app) as c:
        anon = c.get("/api/tables")
        wrong = c.get("/api/tables", headers={"X-API-Key": "not-a-key"})
    assert anon.status_code == 401
    assert wrong.status_code == 401


def test_whoami_and_data_agree_on_the_key(app):
    """The regression: both must resolve the SAME key identity."""
    with TestClient(app) as c:
        who = c.get("/api/whoami", headers={"X-API-Key": KEY})
        data = c.get("/api/tables", headers={"X-API-Key": KEY})
    assert who.status_code == 200 and data.status_code != 401
    body = who.json()
    assert body["via"] == "key"
    assert body["caller"]["key_fp"] == key_fp(KEY)


def test_key_rung_never_leaks_the_raw_key(app):
    with TestClient(app) as c:
        who = c.get("/api/whoami", headers={"X-API-Key": KEY})
    assert KEY not in who.text

def test_api_token_satisfies_require_identity(monkeypatch):
    """The shared token IS a credential: with REQUIRE_IDENTITY on, presenting
    it must authenticate (before the fix the token gate passed and the
    identity gate 401-ed, locking a token-only deployment out of /api)."""
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "shared-tok")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    app = _build_http_app()
    with TestClient(app) as c:
        ok = c.get("/api/tables", headers={"X-API-Token": "shared-tok"})
        anon = c.get("/api/tables")
        wrong = c.get("/api/tables", headers={"X-API-Token": "nope"})
    assert ok.status_code != 401, ok.text
    assert anon.status_code == 401
    assert wrong.status_code == 401


def test_api_token_list_rotation(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "old-tok,new-tok")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    app = _build_http_app()
    with TestClient(app) as c:
        assert c.get("/api/tables", headers={"X-API-Key": "old-tok"}).status_code != 401
        assert c.get("/api/tables", headers={"X-API-Key": "new-tok"}).status_code != 401


def test_subject_bound_key_keeps_its_subject_on_api(monkeypatch, tmp_path):
    """A self-minted key bound to an SSO subject must carry that subject on
    /api too, so policy grants BY NAME apply on both surfaces."""
    from sqlhandler import admin_keys

    store = tmp_path / "admin-keys.json"
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(store))
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    monkeypatch.delenv("SQLHANDLER_API_TOKEN", raising=False)
    entry = admin_keys.add_key("bound-key", label="l", created_by="a", subject="alice")
    app = _build_http_app()
    with TestClient(app) as c:
        who = c.get("/api/whoami", headers={"X-API-Key": "bound-key"})
        data = c.get("/api/tables", headers={"X-API-Key": "bound-key"})
    body = who.json()
    assert who.status_code == 200
    assert body["caller"]["subject"] == "alice"
    assert body["caller"]["key_fp"] == entry["fp"]
    assert data.status_code != 401
