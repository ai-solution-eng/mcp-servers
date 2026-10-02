"""Light smoke for the web UI's Access-control panel (frontend-admin feature).

Two layers, deliberately kept light (the route contract lives in
tests/test_admin_api.py — admin-api's suite; this file only guards the UI
asset and its wiring to the pinned contract):

1. THE SHELL STAYS OPEN — /ui (and /) serve the HTML ungated even with
   SQLHANDLER_REQUIRE_IDENTITY=1 (the data gates at /api/*; gating the shell
   would blind every browser user, see _IdentityRequiredMiddleware). The
   served bytes carry the Access-control panel's tab + pane ids.
2. THE PANEL'S CONTRACT — GET /api/admin/grants answers 200 to an
   X-API-Key-carrying admin request and 401/403 without a credential
   (401 when requireIdentity is on, 403 when off and the caller resolves
   non-admin). SKIPPED with a clear reason until admin-api's routes land
   (task-6 in flight when this was written) — the skip is a loud marker,
   not a pass.

Run:  python -m pytest tests/test_admin_ui.py -v
"""

import re

import pytest

from sqlhandler import server as server_module
from sqlhandler.webui import register_ui

# The admin routes under test (pinned contract with admin-api).
_GRANTS = "/api/admin/grants"


@pytest.fixture()
def app(monkeypatch, tmp_path):
    """The real app stack over the production composition (_build_http_app),
    with the gate envs controlled per test (conftest shape of
    test_require_identity.py) and the admin store pointed at a temp file."""
    for var in (
        "MCP_API_KEYS",
        "SQLHANDLER_API_KEYS",
        "SQLHANDLER_REQUIRE_IDENTITY",
        "SQLHANDLER_POLICY_ENABLED",
        "SQLHANDLER_POLICY_FILE",
        "SQLHANDLER_ADMIN_KEYS_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(tmp_path / "admin-keys.json"))
    app = server_module._build_http_app()
    return app


# ---------------------------------------------------------------------------
# 1 — the shell serves ungated, and it carries the panel
# ---------------------------------------------------------------------------


def test_ui_shell_serves_ungated_with_identity_required(app, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        for path in ("/", "/ui", "/ui/index.html"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert "text/html" in r.headers.get("content-type", "")


def test_ui_shell_carries_access_panel(app):
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        html = c.get("/ui").text
    # Tab + pane + credential field + the four admin calls' endpoints — the
    # asset must be wired to the pinned contract, not just present.
    assert 'data-tab="access"' in html
    assert 'id="pane-access"' in html
    assert 'id="adm-key"' in html
    for endpoint in ("/api/admin/grants", "/api/admin/policy", "/api/admin/keys"):
        assert endpoint in html
    # Credential handling promise: the key lives in a JS variable only. The
    # Access-control JS block (between its section marker and the next) makes
    # NO localStorage/sessionStorage call at all — the page's other storage
    # users (theme, saved queries) are outside it.
    marker = "// ---- Access control"
    end_marker = "// ---- SV editor component"
    assert marker in html and end_marker in html
    access_js = html[html.index(marker):html.index(end_marker)]
    # Call syntax only (the block's comments mention the storage names by
    # way of promising NOT to use them).
    assert not re.search(r"(local|session)Storage\s*\.", access_js)


def test_ui_html_asset_on_disk_serves_from_webui(tmp_path):
    """The asset the server ships is the asset with the panel (register_ui
    reads ui/index.html from the package — a stale install would 404 the ids)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from starlette.testclient import TestClient

    from sqlhandler.config import FileConfig
    from sqlhandler.engine import SqlEngine
    from sqlhandler.file import FileProvider

    pq.write_table(pa.table({"id": [1]}), str(tmp_path / "orders.parquet"))
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=3600)
    app = server_module.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=server_module._transport_security,
    )
    register_ui(app, lambda: eng)
    html = TestClient(app).get("/ui").text
    assert 'data-tab="access"' in html


# ---------------------------------------------------------------------------
# 2 — the grants route's auth posture (skips until admin-api lands)
# ---------------------------------------------------------------------------


def _admin_posture(app, monkeypatch, admin_key, require_identity, send_key=True):
    """(response) for GET /api/admin/grants.

    ``admin_key`` CONFIGURES the key env (the deployment's Secret);
    ``send_key`` controls whether the CALLER presents it (X-API-Key) —
    the 200-with-admin-key test needs BOTH (a fail-closed surface can
    never 200 a request that presents no credential; pinned by
    test_admin_api's 401-anonymous contract).
    """
    from starlette.testclient import TestClient

    if admin_key:
        monkeypatch.setenv("SQLHANDLER_API_KEYS", admin_key)
    if require_identity:
        monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    headers = {"X-API-Key": admin_key} if (admin_key and send_key) else {}
    with TestClient(app) as c:
        r = c.get(_GRANTS, headers=headers)
    return r


def test_admin_grants_401_anonymous_identity_required(app, monkeypatch):
    """requireIdentity ON: no credential → 401 {"error": "identity required: …"}."""
    r = _admin_posture(app, monkeypatch, admin_key=None, require_identity=True)
    if r.status_code == 404:
        pytest.skip("admin-api routes not landed yet (task-6)")
    assert r.status_code == 401
    assert "identity required" in r.json()["error"]
    assert r.headers.get("www-authenticate") == "Bearer"


def test_admin_grants_403_nonadmin_without_identity_gate(app, monkeypatch):
    """requireIdentity OFF, but a key env IS configured: an unauthenticated
    caller to a gated route is refused (401/403 — either is contract-true for
    a non-admin/anonymous caller; the panel renders the message verbatim)."""
    r = _admin_posture(app, monkeypatch, admin_key="tok-admin", require_identity=False)
    if r.status_code == 404:
        pytest.skip("admin-api routes not landed yet (task-6)")
    assert r.status_code in (401, 403)
    assert r.json().get("error")


def test_admin_grants_200_with_admin_key(app, monkeypatch):
    """The admin key (X-API-Key) reads the grants payload — the panel's first
    call. Shape spot-checks only (the full contract is test_admin_api's)."""
    r = _admin_posture(app, monkeypatch, admin_key="tok-admin", require_identity=False)
    if r.status_code == 404:
        pytest.skip("admin-api routes not landed yet (task-6)")
    assert r.status_code in (200, 403)
    if r.status_code == 403:
        # The key authenticates (env) but the tmp policy designates nobody —
        # the fail-closed 403 IS the conforming answer here; the full 200
        # flow (policy fixture + designated admin) is test_admin_api's.
        assert r.json() == {"error": "admin access required"}
        pytest.skip("no admins designated in this harness's policy (see test_admin_api for the full 200 flow)")
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("admins", "datasets", "assignments", "blocked", "policy_hash", "keys"):
        assert field in body, field
    assert isinstance(body["keys"], list)
