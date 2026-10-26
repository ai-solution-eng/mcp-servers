"""Ported smoke test for the built-in console — one pytest test per check.

Mirrors smoke_console.py 1:1 (all 26 checks, same three scenarios, same
labels): boots the actual ASGI wiring (__main__ equivalent) with kubernetes
stubbed, then exercises the static shell, traversal guard, console toggle,
and the MCP endpoint in both modern (2026-07-28 envelope) and legacy eras,
with and without API-key auth. The original script stays runnable
standalone (`python3 smoke_console.py`); this file is the CI/pytest
interface.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_namespace_policy import _install_kubernetes_stubs, ensure_exec_state  # noqa: E402

_install_kubernetes_stubs()

PORT = 9191
BASE = f"http://127.0.0.1:{PORT}"


def _start_server(api_key=None, console=True):
    os.environ.pop("K8S_MCP_API_KEY", None)
    os.environ.pop("K8S_MCP_CONSOLE_ENABLED", None)
    if api_key:
        os.environ["K8S_MCP_API_KEY"] = api_key
    if not console:
        os.environ["K8S_MCP_CONSOLE_ENABLED"] = "false"
    import server

    mcp_asgi = server._ApiKeyAuthMiddleware(server.mcp.streamable_http_app(stateless_http=True))
    router = server._ConsoleRouterApp(mcp_asgi, server._ConsoleApp(server._UI_DIR))
    import uvicorn

    cfg = uvicorn.Config(router, host="127.0.0.1", port=PORT, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/ui/index.html", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return srv


def _stop_server(srv):
    srv.should_exit = True
    time.sleep(0.4)
    os.environ.pop("K8S_MCP_API_KEY", None)
    os.environ.pop("K8S_MCP_CONSOLE_ENABLED", None)


def get(path, key=None, no_redirect=False):
    req = urllib.request.Request(BASE + path)
    if key:
        req.add_header("Authorization", f"Bearer {key}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect) if no_redirect else urllib.request.build_opener()
    try:
        r = opener.open(req, timeout=5)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post_mcp(body, key=None, modern=True, method_name=None, named=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if modern and method_name:
        headers["Mcp-Protocol-Version"] = "2026-07-28"
        headers["Mcp-Method"] = method_name
        if named:
            headers["Mcp-Name"] = named
    req = urllib.request.Request(BASE + "/mcp", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.headers.get("content-type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()


_META = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"tools": {}},
    }
}


def _modern_tools(api_key):
    st, _ct, body = post_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": dict(_META)},
        key=api_key,
        modern=True,
        method_name="tools/list",
    )
    try:
        tools = json.loads(body)["result"]["tools"]
    except Exception:
        tools = None
    return st, tools


def _modern_call_list_namespaces(api_key):
    call_params = {"name": "list_namespaces", "arguments": {}}
    call_params.update(_META)
    st, _ct, body = post_mcp(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call_params},
        key=api_key,
        modern=True,
        method_name="tools/call",
        named="list_namespaces",
    )
    text = None
    try:
        text = json.loads(body)["result"]["content"][0]["text"]
    except Exception:
        pass
    return st, text


def _legacy_tools_list(api_key):
    """The legacy stateless path answers SSE."""
    st, ct, body = post_mcp(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}, key=api_key, modern=False
    )
    ok = False
    try:
        if "event-stream" in ct:
            for line in body.decode().split("\n"):
                if line.startswith("data:") and json.loads(line[5:].strip()).get("result", {}).get("tools"):
                    ok = True
                    break
        else:
            ok = st == 200 and bool(json.loads(body).get("result", {}).get("tools"))
    except Exception:
        ok = False
    return ok, ct


# ── Scenario 1: console + mcp, no auth (dev mode) — checks 1-11 ───────────


@pytest.fixture(scope="class")
def srv_dev():
    _install_kubernetes_stubs()
    import server

    ensure_exec_state(server, False)  # deterministic tool surface (18 tools)
    srv = _start_server(api_key=None, console=True)
    yield srv
    _stop_server(srv)


@pytest.mark.usefixtures("srv_dev")
class TestConsoleAndMcpNoAuth:
    def test_get_root_redirects_to_ui(self):
        st, hdrs, _body = get("/", no_redirect=True)
        assert st == 302 and hdrs.get("Location", hdrs.get("location")) == "/ui/", "GET / -> 302 /ui/"

    def test_get_ui_serves_the_shell(self):
        st, _hdrs, body = get("/ui/")
        html = body.decode()
        assert st == 200 and "HPE Kubernetes Ops Console" in html, "GET /ui/ serves the shell"

    def test_shell_carries_csp_headers(self):
        _st, hdrs, _body = get("/ui/")
        assert "content-security-policy" in {k.lower() for k in hdrs}, "shell carries CSP headers"

    def test_style_css_served_as_text_css(self):
        st, hdrs, _body = get("/ui/style.css")
        assert st == 200 and "text/css" in hdrs.get("Content-Type", hdrs.get("content-type", "")), (
            "style.css served with text/css"
        )

    def test_app_js_served_as_javascript(self):
        st, hdrs, _body = get("/ui/app.js")
        assert st == 200 and "javascript" in hdrs.get("Content-Type", hdrs.get("content-type", "")), (
            "app.js served with javascript content type"
        )

    def test_path_traversal_404(self):
        st, _, _ = get("/ui/../server.py")
        assert st == 404, "path traversal /ui/../server.py -> 404"

    def test_encoded_traversal_404(self):
        st, _, _ = get("/ui/%2e%2e/server.py")
        assert st == 404, "encoded traversal /ui/%2e%2e/server.py -> 404"

    def test_modern_tools_list_over_http(self):
        st, tools = _modern_tools(None)
        assert st == 200 and tools and len(tools) >= 18, (
            f"modern tools/list over HTTP ({len(tools) if tools else 0} tools)"
        )

    def test_list_virtual_services_served_over_http(self):
        _st, tools = _modern_tools(None)
        names = {t["name"] for t in tools} if tools else set()
        assert "list_virtual_services" in names, "list_virtual_services served over HTTP"

    def test_modern_tools_call_list_namespaces(self):
        st, text = _modern_call_list_namespaces(None)
        assert st == 200 and text and "NAMESPACES" in text, f"modern tools/call list_namespaces -> {str(text)[:40]!r}"

    def test_legacy_2025_era_tools_list(self):
        ok, ct = _legacy_tools_list(None)
        assert ok, f"legacy 2025-era tools/list (no envelope, ctype={ct.split(';')[0]})"


# ── Scenario 2: console + mcp, API key enforced — checks 12-25 ────────────
# (the same console/MCP checks re-run under the key, plus the auth trio)


@pytest.fixture(scope="class")
def srv_key():
    _install_kubernetes_stubs()
    import server

    ensure_exec_state(server, False)
    srv = _start_server(api_key="smoke-secret", console=True)
    yield srv
    _stop_server(srv)


@pytest.mark.usefixtures("srv_key")
class TestConsoleAndMcpApiKeyEnforced:
    def test_get_root_redirects_to_ui(self):
        st, hdrs, _body = get("/", no_redirect=True)
        assert st == 302 and hdrs.get("Location", hdrs.get("location")) == "/ui/", "GET / -> 302 /ui/"

    def test_get_ui_serves_the_shell(self):
        st, _hdrs, body = get("/ui/")
        html = body.decode()
        assert st == 200 and "HPE Kubernetes Ops Console" in html, "GET /ui/ serves the shell"

    def test_shell_carries_csp_headers(self):
        _st, hdrs, _body = get("/ui/")
        assert "content-security-policy" in {k.lower() for k in hdrs}, "shell carries CSP headers"

    def test_style_css_served_as_text_css(self):
        st, hdrs, _body = get("/ui/style.css")
        assert st == 200 and "text/css" in hdrs.get("Content-Type", hdrs.get("content-type", "")), (
            "style.css served with text/css"
        )

    def test_app_js_served_as_javascript(self):
        st, hdrs, _body = get("/ui/app.js")
        assert st == 200 and "javascript" in hdrs.get("Content-Type", hdrs.get("content-type", "")), (
            "app.js served with javascript content type"
        )

    def test_path_traversal_404(self):
        st, _, _ = get("/ui/../server.py")
        assert st == 404, "path traversal /ui/../server.py -> 404"

    def test_encoded_traversal_404(self):
        st, _, _ = get("/ui/%2e%2e/server.py")
        assert st == 404, "encoded traversal /ui/%2e%2e/server.py -> 404"

    def test_modern_tools_list_over_http(self):
        st, tools = _modern_tools("smoke-secret")
        assert st == 200 and tools and len(tools) >= 18, (
            f"modern tools/list over HTTP ({len(tools) if tools else 0} tools)"
        )

    def test_list_virtual_services_served_over_http(self):
        _st, tools = _modern_tools("smoke-secret")
        names = {t["name"] for t in tools} if tools else set()
        assert "list_virtual_services" in names, "list_virtual_services served over HTTP"

    def test_modern_tools_call_list_namespaces(self):
        st, text = _modern_call_list_namespaces("smoke-secret")
        assert st == 200 and text and "NAMESPACES" in text, f"modern tools/call list_namespaces -> {str(text)[:40]!r}"

    def test_legacy_2025_era_tools_list(self):
        ok, ct = _legacy_tools_list("smoke-secret")
        assert ok, f"legacy 2025-era tools/list (no envelope, ctype={ct.split(';')[0]})"

    def test_console_shell_reachable_without_key(self):
        st, _, _ = get("/ui/")
        assert st == 200, "console shell reachable WITHOUT key (inert, no data)"

    def test_mcp_without_key_401(self):
        st, _, _ = post_mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}, key=None, modern=False)
        assert st == 401, "/mcp without key -> 401 (shell exemption did not weaken auth)"

    def test_mcp_with_wrong_key_401(self):
        st, _, _ = post_mcp(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}, key="wrong", modern=False
        )
        assert st == 401, "/mcp with wrong key -> 401"


# ── Scenario 3: console disabled — check 26 ───────────────────────────────


@pytest.fixture(scope="class")
def srv_off():
    _install_kubernetes_stubs()
    import server

    ensure_exec_state(server, False)
    srv = _start_server(api_key=None, console=False)
    yield srv
    _stop_server(srv)


@pytest.mark.usefixtures("srv_off")
class TestConsoleDisabled:
    def test_console_disabled_via_flag_404(self):
        st, _, _ = get("/ui/")
        assert st == 404, "console disabled via K8S_MCP_CONSOLE_ENABLED=false -> 404"
