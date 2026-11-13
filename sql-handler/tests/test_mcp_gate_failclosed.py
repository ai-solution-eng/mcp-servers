"""P1-4: the /mcp API-key gate fails CLOSED on the keys store.

The gate used to arm only when the store was configured AND non-empty, so
the emergency action (revoke the last minted key) or a corrupt store file
read as "no gate" and /mcp admitted anonymous callers (REQUIRE_IDENTITY is
off by default, so nothing else caught it). The store's STATE now decides:

* disabled  (no keys file configured) -> env keys / openMode escape as before;
* empty     (configured, zero keys)   -> gate ARMED (401 for keyless callers);
* unreadable (configured, broken)     -> 503, fail closed, distinguishable
  from 401 so an operator sees a fault, not a bad credential;
* ok        -> unchanged (minted keys authenticate via match_presentation).

It also pins the F4 fix on the /api token middleware (same credential
shape family): _ApiTokenMiddleware accepts X-API-Key and a COMMA-SEPARATED
SQLHANDLER_API_TOKEN list.

Run: python -m pytest tests/test_mcp_gate_failclosed.py -v
"""

import pytest
from starlette.testclient import TestClient

from sqlhandler import admin_keys
from sqlhandler.server import (
    _build_http_app,
    _keys_store_state,
    _McpApiKeyMiddleware,
)


@pytest.fixture()
def app(monkeypatch):
    """Fresh app per test; the middleware reads env/state per request."""
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_ADMIN_KEYS_FILE"):
        monkeypatch.delenv(var, raising=False)
    # The once-per-process unreadable ERROR log is class state: reset it so
    # the log assertions below see THIS test's requests only.
    _McpApiKeyMiddleware._store_unreadable_logged = False
    app = _build_http_app()
    with TestClient(app) as c:
        yield c
    _McpApiKeyMiddleware._store_unreadable_logged = False


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def test_disabled_store_no_env_keys_stays_open(app):
    """Documented LOCAL-DEV escape, preserved: no keys file, no env keys ->
    /mcp open with the loud startup warning (unchanged behavior)."""
    assert _keys_store_state() == "disabled"
    assert _ping(app).status_code == 200


def test_env_keys_still_arm_and_authenticate(app, monkeypatch):
    """Control: the env-key path is untouched by the store-state change."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    assert _ping(app).status_code == 401
    assert _ping(app, {"X-API-Key": "uni-k"}).status_code == 200


def test_configured_empty_store_arms_gate(app, monkeypatch, tmp_path):
    """THE emergency action: the last minted key is revoked -> a configured,
    EMPTY store. The gate must STAY ARMED (401 keyless), never read as
    "no gate" (the P1-4 bug: that turned the revoke into an open switch)."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text('{"keys": []}', encoding="utf-8")  # valid schema, zero keys
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(keys_file))
    assert _keys_store_state() == "empty"
    r = _ping(app)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_ok_state_does_not_block_env_key(app, monkeypatch):
    """Control: a healthy ('ok') store never trips the 503 branch, and env
    keys keep authenticating over it."""
    monkeypatch.setattr(admin_keys, "store_state", lambda *a, **k: "ok")
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    assert _ping(app, {"X-API-Key": "uni-k"}).status_code == 200
    assert _ping(app).status_code == 401


def test_unreadable_store_fails_closed_503(app, monkeypatch, caplog):
    """A configured-but-unreadable store (corrupt file on restart) is a
    deploy-level fault: 503 — distinguishable from a 401 bad credential —
    plus the once-per-process ERROR log."""
    monkeypatch.setattr(admin_keys, "store_state", lambda *a, **k: "unreadable")
    with caplog.at_level("ERROR", logger="sqlhandler.server"):
        r = _ping(app)
    assert r.status_code == 503
    assert r.json() == {"error": "authentication store unavailable"}
    assert any("UNREADABLE" in rec.message for rec in caplog.records)


def test_unreadable_store_file_fails_closed_503(app, monkeypatch, tmp_path):
    """Real-file path (no monkeypatch): a store file with the WRONG SHAPE
    (no 'keys' list) is UNREADABLE per admin_keys.store_state -> 503."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text('{"minted": []}', encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(keys_file))
    assert _keys_store_state() == "unreadable"
    r = _ping(app)
    assert r.status_code == 503
    assert r.json() == {"error": "authentication store unavailable"}


def test_unreadable_store_serves_env_keys_but_refuses_minted(app, monkeypatch, caplog):
    """Reconciled posture (task-11 follow-up): env keys live in the process
    environment, NOT the store, so a corrupt store must not self-DoS them —
    env-key callers are SERVED (with the once-per-process ERROR log naming
    the degradation), and unverifiable minted/unknown keys are refused 401."""
    monkeypatch.setattr(admin_keys, "store_state", lambda *a, **k: "unreadable")
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    with caplog.at_level("ERROR", logger="sqlhandler.server"):
        assert _ping(app, {"X-API-Key": "uni-k"}).status_code == 200
        assert _ping(app, {"X-API-Key": "minted-but-unverifiable"}).status_code == 401
    assert any("still authenticate" in rec.message for rec in caplog.records)


def test_unreadable_store_error_logged_once_per_process(app, monkeypatch, caplog):
    monkeypatch.setattr(admin_keys, "store_state", lambda *a, **k: "unreadable")
    with caplog.at_level("ERROR", logger="sqlhandler.server"):
        _ping(app)
        _ping(app)
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "UNREADABLE" in r.message]
    assert len(errors) == 1


# ------------------------------------------------- F4: the /api token gate


def _api_client(monkeypatch, token):
    """A fresh app with SQLHANDLER_API_TOKEN set (the middleware is installed
    at _build_http_app time from the env). Probed on /api/whoami — the
    engine-free identity preview — so the TOKEN gate is what is under test."""
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", token)
    app = _build_http_app()
    with TestClient(app) as c:
        yield c


def test_api_token_accepts_x_api_key_header(monkeypatch):
    """F4: the UI's key widget sends X-API-Key — a valid token pasted there
    must authenticate, not 401."""
    for c in _api_client(monkeypatch, "tok-1"):
        assert c.get("/api/whoami", headers={"X-API-Key": "tok-1"}).status_code == 200
        assert c.get("/api/whoami").status_code == 401
        assert c.get("/api/whoami", headers={"X-API-Key": "wrong"}).status_code == 401


def test_api_token_accepts_token_from_comma_separated_list(monkeypatch):
    """The rotation story: SQLHANDLER_API_TOKEN as a 2-token list — either
    token authenticates, via any accepted header shape; junk does not."""
    for c in _api_client(monkeypatch, "tok-1, tok-2"):
        assert c.get("/api/whoami", headers={"X-API-Key": "tok-2"}).status_code == 200
        assert c.get("/api/whoami", headers={"Authorization": "Bearer tok-1"}).status_code == 200
        assert c.get("/api/whoami", headers={"X-API-Token": "tok-2"}).status_code == 200
        assert c.get("/api/whoami", headers={"X-API-Key": "nope"}).status_code == 401


def test_api_token_comma_only_env_fails_closed(monkeypatch):
    """A comma/whitespace-only env is NOT an empty env: it strips to a
    truthy "," so the middleware IS installed — with zero usable tokens —
    and fails CLOSED (401 for everyone), exactly as the pre-F4 code did.
    The 'middleware not installed' case is the EMPTY env (no gate at all)."""
    for c in _api_client(monkeypatch, " , "):
        assert c.get("/api/whoami").status_code == 401
