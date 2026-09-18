"""The console (GET /, /ui) must load WITHOUT a key — the unlock bar lives
in that HTML and cannot appear behind it (2026-09-13 deploy defect: the
browser got the 401 JSON at / and the console was unreachable).  Every DATA
route stays keyed. Fleet pattern: K8S-MCP console (public-but-inert HTML).
"""

import pytest

import server


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("WORKBENCH_EXEC_ALLOWLIST", raising=False)
    monkeypatch.delenv("WORKBENCH_METRICS_ENABLED", raising=False)
    return server._build_http_app()


@pytest.mark.anyio
async def test_console_html_loads_without_key(app):
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200, "the console HTML must be public"
        assert "unlock" in r.text.lower(), "the unlock bar must be present in the served HTML"
        assert c.get("/ui").status_code == 200


@pytest.mark.anyio
async def test_data_routes_stay_keyed(app, monkeypatch):
    """With a key CONFIGURED: unkeyed data calls -> 401; console HTML still public.
    (With no keys configured the middleware is open-with-warning — the
    documented unset posture — so the gate is asserted in the configured state.)"""
    from starlette.testclient import TestClient

    monkeypatch.setenv("WORKBENCH_API_KEYS", "cfg-key-1")
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/workspaces").status_code == 401
        assert c.post("/api/run", json={"workspace": "x", "command": ["ls"]}).status_code == 401
        assert c.get("/").status_code == 200, "console HTML stays public even when keyed"


@pytest.mark.anyio
async def test_keyed_calls_succeed(app, monkeypatch):
    from starlette.testclient import TestClient

    key = "probe-key-123"
    monkeypatch.setenv("WORKBENCH_API_KEYS", key)  # middleware re-reads env per request
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": key}).status_code == 200
        assert c.get("/api/status").status_code == 401


@pytest.mark.anyio
async def test_metrics_off_leaves_metrics_absent(app):
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 404  # route not registered when metrics disabled
