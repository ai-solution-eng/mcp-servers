"""End-to-end smoke test for the built-in console (run against the real server).

Boots the actual ASGI wiring (__main__ equivalent) with kubernetes stubbed,
then exercises: static shell, traversal guard, console toggle, and the MCP
endpoint in both modern (2026-07-28 envelope) and legacy eras, with and
without API-key auth.
"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# kubernetes stubs (same approach as test_namespace_policy.py)
_src = open(os.path.join(HERE, "test_namespace_policy.py")).read()
exec(_src.split("def main()")[0])
_install_kubernetes_stubs()

import server  # noqa: E402

PORT = 9191
BASE = f"http://127.0.0.1:{PORT}"


def start_server(api_key=None, console=True):
    os.environ.pop("K8S_MCP_API_KEY", None)
    os.environ.pop("K8S_MCP_CONSOLE_ENABLED", None)
    if api_key:
        os.environ["K8S_MCP_API_KEY"] = api_key
    if not console:
        os.environ["K8S_MCP_CONSOLE_ENABLED"] = "false"
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
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if modern and method_name:
        headers["Mcp-Protocol-Version"] = "2026-07-28"
        headers["Mcp-Method"] = method_name
        if named:
            headers["Mcp-Name"] = named
    req = urllib.request.Request(BASE + "/mcp", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.headers.get("content-type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()


results = []


def check(cond, label):
    results.append((bool(cond), label))
    print(("  PASS  " if cond else "  FAIL  ") + label)


def run(api_key=None, console=True):
    srv = start_server(api_key=api_key, console=console)
    try:
        if not console:
            st, _, _ = get("/ui/")
            check(st == 404, "console disabled via K8S_MCP_CONSOLE_ENABLED=false -> 404")
            return
        # ── static shell ──
        st, hdrs, body = get("/", no_redirect=True)
        check(st == 302 and hdrs.get("Location", hdrs.get("location")) == "/ui/",
              "GET / -> 302 /ui/")
        st, hdrs, body = get("/ui/")
        html = body.decode()
        check(st == 200 and "HPE Kubernetes Ops Console" in html, "GET /ui/ serves the shell")
        check("content-security-policy" in {k.lower() for k in hdrs}, "shell carries CSP headers")
        st, hdrs, body = get("/ui/style.css")
        check(st == 200 and "text/css" in hdrs.get("Content-Type", hdrs.get("content-type", "")),
              "style.css served with text/css")
        st, hdrs, body = get("/ui/app.js")
        check(st == 200 and "javascript" in hdrs.get("Content-Type", hdrs.get("content-type", "")),
              "app.js served with javascript content type")
        st, _, _ = get("/ui/../server.py")
        check(st == 404, "path traversal /ui/../server.py -> 404")
        st, _, _ = get("/ui/%2e%2e/server.py")
        check(st == 404, "encoded traversal /ui/%2e%2e/server.py -> 404")

        # ── MCP: modern envelope ──
        meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                          "io.modelcontextprotocol/clientCapabilities": {"tools": {}}}}
        st, ct, body = post_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": dict(meta)},
                                key=api_key, modern=True, method_name="tools/list")
        tools = None
        try:
            tools = json.loads(body)["result"]["tools"]
        except Exception:
            pass
        check(st == 200 and tools and len(tools) >= 18,
              f"modern tools/list over HTTP ({len(tools) if tools else 0} tools)")
        names = {t["name"] for t in tools} if tools else set()
        check("list_virtual_services" in names, "list_virtual_services served over HTTP")

        call_params = {"name": "list_namespaces", "arguments": {}}
        call_params.update(meta)
        st, ct, body = post_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": call_params},
                                key=api_key, modern=True, method_name="tools/call",
                                named="list_namespaces")
        ok = st == 200
        text = None
        try:
            text = json.loads(body)["result"]["content"][0]["text"]
        except Exception:
            ok = False
        check(ok and text and "NAMESPACES" in text, f"modern tools/call list_namespaces -> {str(text)[:40]!r}")

        # ── MCP: legacy (no envelope) — the legacy stateless path answers SSE ──
        st, ct, body = post_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                                key=api_key, modern=False)
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
        check(ok, f"legacy 2025-era tools/list (no envelope, ctype={ct.split(';')[0]})")

        # ── auth behavior ──
        if api_key:
            st, _, _ = get("/ui/")
            check(st == 200, "console shell reachable WITHOUT key (inert, no data)")
            st, _, _ = post_mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                                key=None, modern=False)
            check(st == 401, "/mcp without key -> 401 (shell exemption did not weaken auth)")
            st, _, _ = post_mcp({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
                                key="wrong", modern=False)
            check(st == 401, "/mcp with wrong key -> 401")
    finally:
        srv.should_exit = True
        time.sleep(0.4)


print("[console + mcp, no auth (dev mode)]")
run(api_key=None)
print("[console + mcp, API key enforced]")
run(api_key="smoke-secret")
print("[console disabled]")
run(api_key=None, console=False)

fails = [lbl for ok, lbl in results if not ok]
print()
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print(f"All {len(results)} smoke checks passed.")
