"""Tests for the Workbench MCP web UI + JSON API (offline, tmp root).

The /api/* endpoints call the SAME core functions the MCP tools use, so
these tests drive a real Starlette app (TestClient) against a real
WORKBENCH_ROOT under tmp_path and assert the core policy (path confinement,
caps, argv allowlist, confirm gates, audit) still applies through the UI.
Follows the prometheus-mcp tests/test_webui.py pattern.
"""

import re

import pytest
from starlette.testclient import TestClient

import server
import webui


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture()
def client(root):
    app = server._build_http_app()
    with TestClient(app) as c:
        yield c


def ws_url(ws: str, suffix: str = "") -> str:
    return "/api/ws/" + ws + suffix


# ---------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------


def test_ui_serves_hpe_branding_and_tabs(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Hewlett Packard Enterprise" in html
    assert "hpe-element" in html  # the green parallelogram mark
    assert "workbench-theme" in html  # no-flash theme script + persistence
    assert "Workbench MCP" in html
    for tab in ("Files", "Env", "Run", "Audit"):
        assert tab in html
    # the trust-model banner is visible and says the load-bearing part
    assert "Scratch pad by design" in html
    assert "allow-listed commands" in html
    assert "API-key gated" in html  # fleet-audit P0: key gate replaced the gateway-only wording
    assert client.get("/ui").status_code == 200
    assert client.get("/ui").text == html


def test_ui_html_fallback_when_asset_missing(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(webui, "_HTML_CANDIDATES", (Path("/nonexistent/ui/index.html"),))
    html = webui._load_html()
    assert "UI asset not found" in html
    assert "WORKBENCH_UI_HTML" in html


def test_ui_has_no_duplicate_element_ids():
    # getElementById silently resolves to the FIRST match — duplicated ids
    # would leave one of the two panels dead.
    ids = re.findall(r'id="([^"]+)"', webui._load_html())
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate element ids: {sorted(dupes)}"


def test_ui_is_self_contained():
    # Corporate-proxy safe: no CDN, external font, or build-step references.
    html = webui._load_html()
    for bad in ("<script src", 'src="http', 'href="http', "@import", "cdn.", "fonts.googleapis"):
        assert bad not in html, f"external reference found: {bad}"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_endpoint_reports_caps_and_policy(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "python3,ls")
    monkeypatch.setenv("WORKBENCH_MAX_FILE_BYTES", "1000")
    data = client.get("/api/status").json()
    assert data["status"] == "ok"
    assert data["caps"]["max_file_bytes"] == 1000
    assert data["caps"]["timeout_max_s"] == 600
    assert data["policy"]["allowlist"] == ["ls", "python3"]  # sorted


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------


def test_workspace_create_list_delete_via_api(client):
    r = client.post("/api/workspaces", json={"name": "alpha"})
    assert r.status_code == 200 and r.json()["created"] is True
    listing = client.get("/api/workspaces").json()
    assert [w["workspace"] for w in listing["workspaces"]] == ["alpha"]
    # duplicate refused through the API too
    assert client.post("/api/workspaces", json={"name": "alpha"}).status_code == 400
    # invalid names refused by the core regex
    assert client.post("/api/workspaces", json={"name": "../escape"}).status_code == 400
    # delete requires confirm — the endpoint cannot bypass it
    assert client.post("/api/workspaces/delete", json={"name": "alpha"}).status_code == 400
    r = client.post("/api/workspaces/delete", json={"name": "alpha", "confirm": True})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/api/workspaces").json()["workspaces"] == []


def test_workspaces_bad_body(client):
    assert (
        client.post("/api/workspaces", content=b"not json", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    # null/missing name -> empty string -> core regex refusal
    assert client.post("/api/workspaces", json={"name": None}).status_code == 400
    assert client.post("/api/workspaces", json={}).status_code == 400


# ---------------------------------------------------------------------------
# files: confinement and caps through the UI endpoints
# ---------------------------------------------------------------------------


def test_file_write_read_tree_via_api(client):
    assert client.post("/api/workspaces", json={"name": "ws"}).status_code == 200
    r = client.post(ws_url("ws", "/file"), json={"path": "src/app/main.py", "content": "print('hi')\n"})
    assert r.status_code == 200 and r.json()["bytes"] == 12
    r = client.get(ws_url("ws", "/files"))
    assert {e["path"] for e in r.json()["entries"]} == {"src", "src/app", "src/app/main.py"}
    r = client.get(ws_url("ws", "/file"), params={"path": "src/app/main.py"})
    body = r.json()
    assert body["content"] == "print('hi')\n" and body["truncated"] is False
    # files/{ws}/files on an unknown workspace -> core error surfaced as 400
    assert client.get(ws_url("ghost", "/files")).status_code == 400


def test_file_read_max_bytes_and_binary_via_api(client, root):
    client.post("/api/workspaces", json={"name": "ws"})
    client.post(ws_url("ws", "/file"), json={"path": "big.txt", "content": "x" * 100})
    r = client.get(ws_url("ws", "/file"), params={"path": "big.txt", "max_bytes": 10})
    body = r.json()
    assert body["truncated"] is True and len(body["content"]) == 10
    (root / "ws" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    r = client.get(ws_url("ws", "/file"), params={"path": "blob.bin"})
    assert r.status_code == 400 and "not valid UTF-8" in r.json()["error"]
    assert client.get(ws_url("ws", "/file")).status_code == 400  # missing path
    assert client.get(ws_url("ws", "/file"), params={"path": "big.txt", "max_bytes": "x"}).status_code == 400


def test_path_traversal_blocked_through_api(client):
    client.post("/api/workspaces", json={"name": "ws"})
    for bad in ["../escape.txt", "/etc/passwd", "a/../../b"]:
        r = client.post(ws_url("ws", "/file"), json={"path": bad, "content": "nope"})
        assert r.status_code == 400
        assert "escapes the workspace" in r.json()["error"]
    r = client.get(ws_url("ws", "/file"), params={"path": "../../etc/passwd"})
    assert r.status_code == 400


def test_file_delete_requires_confirm_via_api(client):
    client.post("/api/workspaces", json={"name": "ws"})
    client.post(ws_url("ws", "/file"), json={"path": "f.txt", "content": "data"})
    r = client.post(ws_url("ws", "/file/delete"), json={"path": "f.txt"})
    assert r.status_code == 400 and "confirm=true" in r.json()["error"]
    r = client.post(ws_url("ws", "/file/delete"), json={"path": "f.txt", "confirm": True})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get(ws_url("ws", "/file"), params={"path": "f.txt"}).status_code == 400


def test_file_write_cap_enforced_through_api(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_FILE_BYTES", "10")
    client.post("/api/workspaces", json={"name": "ws"})
    r = client.post(ws_url("ws", "/file"), json={"path": "big.txt", "content": "x" * 11})
    assert r.status_code == 400 and "cap is 10" in r.json()["error"]


# ---------------------------------------------------------------------------
# env store
# ---------------------------------------------------------------------------


def test_env_set_get_via_api(client):
    client.post("/api/workspaces", json={"name": "ws"})
    r = client.post(ws_url("ws", "/env"), json={"key": "MODEL_NAME", "value": "qwen-7b"})
    assert r.status_code == 200 and r.json()["set"] is True
    data = client.get(ws_url("ws", "/env")).json()
    assert data["env"] == {"MODEL_NAME": "qwen-7b"}  # sorted dict, values shown
    # core validation applies: invalid key refused
    r = client.post(ws_url("ws", "/env"), json={"key": "lower-case", "value": "x"})
    assert r.status_code == 400 and "invalid env key" in r.json()["error"]
    assert client.post(ws_url("ws", "/env"), json={"key": "OK"}).status_code == 400  # non-string value
    # unknown workspace -> 400
    assert client.get(ws_url("ghost", "/env")).status_code == 400


# ---------------------------------------------------------------------------
# run_command: allowlist / denylist / caps / env injection via the UI
# ---------------------------------------------------------------------------


def test_run_via_api_and_allowlist_enforced(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "python3,ls")
    monkeypatch.setenv("WORKBENCH_EXEC_DENYLIST", "curl")
    client.post("/api/workspaces", json={"name": "ws"})
    r = client.post(ws_url("ws", "/run"), json={"command": ["python3", "-c", "print(1+1)"]})
    assert r.status_code == 200
    out = r.json()
    assert out["exit_code"] == 0 and out["stdout"].strip() == "2"
    assert out["duration_ms"] >= 0 and out["timed_out"] is False
    # not allow-listed -> 400 with the core's explanation
    r = client.post(ws_url("ws", "/run"), json={"command": ["bash", "-c", "echo hi"]})
    assert r.status_code == 400 and "not in the allowlist" in r.json()["error"]
    # deny-listed wins
    r = client.post(ws_url("ws", "/run"), json={"command": ["curl", "http://example.com"]})
    assert r.status_code == 400 and "deny-listed" in r.json()["error"]
    # non-list argv refused
    assert client.post(ws_url("ws", "/run"), json={"command": "ls"}).status_code == 400
    assert client.post(ws_url("ws", "/run"), json={}).status_code == 400
    # bad timeout -> 400, never a 500
    assert client.post(ws_url("ws", "/run"), json={"command": ["ls"], "timeout_s": "x"}).status_code == 400


def test_run_via_api_caps_output_and_injects_env(client, root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_OUTPUT_BYTES", "100")
    client.post("/api/workspaces", json={"name": "ws"})
    client.post(ws_url("ws", "/env"), json={"key": "GREETING", "value": "hello"})
    r = client.post(
        ws_url("ws", "/run"),
        json={"command": ["python3", "-c", "import os; print(os.environ['GREETING'])"]},
    )
    assert r.json()["stdout"].strip() == "hello"  # workspace env injected
    r = client.post(
        ws_url("ws", "/run"),
        json={"command": ["python3", "-c", "print('x' * 1000)"]},
    )
    out = r.json()
    assert len(out["stdout"]) <= 100 and out["truncated"] is True  # cap still applies


def test_run_via_api_passthrough_proxy_env(client, monkeypatch):
    # The chart wires HTTP_PROXY/HTTPS_PROXY/NO_PROXY on the pod for pip;
    # run_command must pass them through to child processes.
    monkeypatch.setenv("HTTP_PROXY", "http://hpeproxy.its.hpecorp.net:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://hpeproxy.its.hpecorp.net:8080")
    monkeypatch.setenv("NO_PROXY", ".cluster.local,localhost")
    client.post("/api/workspaces", json={"name": "ws"})
    r = client.post(
        ws_url("ws", "/run"),
        json={"command": ["python3", "-c", "import os; print(os.environ['HTTP_PROXY'], '|', os.environ['NO_PROXY'])"]},
    )
    assert r.status_code == 200
    assert r.json()["stdout"].strip() == "http://hpeproxy.its.hpecorp.net:8080 | .cluster.local,localhost"


def test_run_via_api_workspace_env_overrides_passthrough(client, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://from-pod:8080")
    client.post("/api/workspaces", json={"name": "ws"})
    client.post(ws_url("ws", "/env"), json={"key": "HTTP_PROXY", "value": "http://from-workspace:8080"})
    r = client.post(
        ws_url("ws", "/run"), json={"command": ["python3", "-c", "import os; print(os.environ['HTTP_PROXY'])"]}
    )
    assert r.json()["stdout"].strip() == "http://from-workspace:8080"


# ---------------------------------------------------------------------------
# audit tail
# ---------------------------------------------------------------------------


def test_audit_tail_via_api(client, root, monkeypatch):
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "ls")
    client.post("/api/workspaces", json={"name": "ws"})
    client.post(ws_url("ws", "/run"), json={"command": ["ls"]})
    data = client.get("/api/audit").json()
    events = data["events"]
    kinds = [e["event"] for e in events]
    assert "workspace_create" in kinds and "run_command" in kinds
    assert data["n_total"] == data["n_shown"] == len((root / ".audit.jsonl").read_text().strip().splitlines())
    # tail works: n=1 returns only the newest entry
    data = client.get("/api/audit", params={"n": 1}).json()
    assert data["n_total"] >= 2 and data["n_shown"] == 1
    assert data["events"][0]["event"] == "run_command"
    # cap: n is bounded
    assert client.get("/api/audit", params={"n": 99999}).status_code == 200
    assert client.get("/api/audit", params={"n": "x"}).status_code == 400
    # empty/nonexistent audit log -> honest empty payload, not a 500
    (root / ".audit.jsonl").unlink()
    data = client.get("/api/audit").json()
    assert data["events"] == [] and data["n_total"] == 0


def test_audit_tail_skips_torn_lines(client, root):
    client.post("/api/workspaces", json={"name": "ws"})
    with open(root / ".audit.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"ts": "t", "event": "torn")\n')  # malformed JSON tail
    data = client.get("/api/audit").json()
    assert data["n_total"] >= 1  # raw line counted
    assert all(isinstance(e, dict) for e in data["events"])  # torn line skipped


# ---------------------------------------------------------------------------
# server wiring + gating
# ---------------------------------------------------------------------------


def test_server_mounts_ui_routes():
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/ui", "/api/status", "/api/workspaces", "/api/audit", "/health", "/healthz", "/mcp"} <= paths


def test_ui_disabled_removes_ui_routes_but_mcp_keeps_working(monkeypatch, root):
    monkeypatch.setenv("WORKBENCH_UI_ENABLED", "false")
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" not in paths and "/ui" not in paths
    assert not any(p.startswith("/api/") for p in paths if p)
    assert {"/mcp", "/health", "/healthz"} <= paths
    # /mcp still serves tools through the full stateless wire
    with TestClient(app) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "run_command" in names and "workspace_create" in names
        assert "mcp-session-id" not in {k.lower() for k in r.headers}
    # ...and the value parses the fuzzy forms the way _ui_enabled documents
    for off in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("WORKBENCH_UI_ENABLED", off)
        assert server._ui_enabled() is False
    monkeypatch.delenv("WORKBENCH_UI_ENABLED")
    assert server._ui_enabled() is True


def test_ui_enabled_env_parses_fuzzy(monkeypatch):
    for on in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("WORKBENCH_UI_ENABLED", on)
        assert server._ui_enabled() is True
