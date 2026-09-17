#!/usr/bin/env python3
"""Live end-to-end check of the HTTP surfaces with a REAL server + REAL curl.

Run:  python3 live_check.py        (needs: pip install uvicorn)

The MCP SDK is stubbed (only the HTTP surfaces are exercised — /ui/,
/api/managed, /upload), the MCP app itself is a dummy, and ChartMuseum is
replaced by a local stub that can script status codes. Verifies with real
curl against real uvicorn:

  1. POST /upload without key            -> 401
  2. POST /upload with key (raw .tgz)    -> 200 uploaded + JSON
  3. POST /upload again                  -> 409 exists (stub)
  4. POST /upload?force=true (unmanaged) -> 403 refused
  5. GET  /ui/                           -> 200 text/html
  6. GET  /api/managed with key          -> 200 listing incl. the uploaded chart
  7. GET  /mcp on the dummy app          -> 200 (auth passes through)
"""

import base64
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types


def _install_mcp_stubs():
    if "mcp" in sys.modules:
        return
    mcp_pkg = types.ModuleType("mcp")
    server_pkg = types.ModuleType("mcp.server")

    class MCPServer:
        def __init__(self, *args, **kwargs):
            self.tools = []

            def tool():
                def decorator(fn):
                    self.tools.append(fn)
                    return fn
                return decorator

            self.tool = tool

    mcpserver_mod = types.ModuleType("mcp.server.mcpserver")
    mcpserver_mod.MCPServer = MCPServer

    class CacheHint:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    caching_mod = types.ModuleType("mcp.server.caching")
    caching_mod.CacheHint = CacheHint

    class TransportSecuritySettings:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    transport_mod = types.ModuleType("mcp.server.transport_security")
    transport_mod.TransportSecuritySettings = TransportSecuritySettings

    mcp_pkg.server = server_pkg
    server_pkg.mcpserver = mcpserver_mod
    server_pkg.caching = caching_mod
    server_pkg.transport_security = transport_mod
    sys.modules["mcp"] = mcp_pkg
    sys.modules["mcp.server"] = server_pkg
    sys.modules["mcp.server.mcpserver"] = mcpserver_mod
    sys.modules["mcp.server.caching"] = caching_mod
    sys.modules["mcp.server.transport_security"] = transport_mod


_install_mcp_stubs()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

try:
    import uvicorn  # noqa: F401
except ImportError:
    print("uvicorn is required: pip install uvicorn")
    sys.exit(2)

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

PASS, FAIL = 0, 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}  {detail}")


def make_chart_tgz():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("live-app/Chart.yaml", "apiVersion: v2\nname: live-app\nversion: 9.9.9\n"),
            ("live-app/values.yaml", "replicaCount: 1\n"),
        ):
            data = payload.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeLedger:
    """In-memory ledger (kubectl isn't available on a laptop)."""

    name = "live-ledger"
    namespace = "default"

    def __init__(self):
        self.charts, self.apps = {}, {}

    async def load(self):
        return dict(self.charts), dict(self.apps)

    async def save(self, charts, apps):
        self.charts, self.apps = dict(charts), dict(apps)

    async def record_chart(self, n, v, filename=""):
        c, a = await self.load()
        c[f"{n}/{v}"] = {"uploaded_at": "t", "filename": filename}
        await self.save(c, a)

    async def record_ezappconfig(self, name, chart, cv, ns):
        c, a = await self.load()
        a[name] = {"applied_at": "t", "chart": chart,
                   "chart_version": cv, "target_namespace": ns}
        await self.save(c, a)

    async def forget_chart(self, n, v):
        c, a = await self.load()
        c.pop(f"{n}/{v}", None)
        await self.save(c, a)

    async def forget_ezappconfig(self, name):
        c, a = await self.load()
        a.pop(name, None)
        await self.save(c, a)

    async def has_chart(self, n, v):
        c, _ = await self.load()
        return f"{n}/{v}" in c

    async def has_ezappconfig(self, name):
        _, a = await self.load()
        return name in a


class _StubChartMuseum:
    def __init__(self):
        self.script = [201, 409, 201, 201]   # consumed in request order
        self.state = {"i": 0}

        class Handler(BaseHTTPRequestHandler):
            def _respond(inner):
                code = self.script[min(self.state["i"], len(self.script) - 1)]
                self.state["i"] += 1
                inner.send_response(code)
                body = b'{"data":{}}'
                inner.send_header("Content-Length", str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)

            do_POST = _respond

            def log_message(inner, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _dummy_mcp(scope, receive, send):
    body = b'{"jsonrpc":"2.0","dummy":true}'
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def curl(args):
    return subprocess.run(["curl", "-sS", *args], capture_output=True, text=True)


def main():
    museum = _StubChartMuseum()
    port = free_port()
    os.environ["EZAPP_MCP_API_KEY"] = "live-key"
    os.environ["CHARTMUSEUM_URL"] = museum.url
    os.environ["EZAPP_MCP_LEDGER_NAMESPACE"] = "default"
    server._ledger_instance = _FakeLedger()

    app = server._UiRouterApp(
        server._ApiKeyAuthMiddleware(server._ApiOrMcpApp(_dummy_mcp)),
        server._UiApp(server._UI_DIR),
    )

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    threading.Thread(target=uv.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):  # wait for uvicorn to accept connections
        try:
            probe = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", base + "/ui/"],
                capture_output=True, timeout=2)
            if probe.returncode == 0:
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.1)

    tgz = make_chart_tgz()
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as fh:
        fh.write(tgz)
        tgz_path = fh.name

    print(f"1. auth gate")
    out = curl(["-X", "POST", "--data-binary", f"@{tgz_path}",
                f"{base}/upload", "-w", "\n%{http_code}"])
    code = out.stdout.rsplit("\n", 1)[-1]
    check("POST /upload without key → 401", code == "401", out.stdout[-200:])

    print(f"2. upload (raw curl, real uvicorn)")
    out = curl(["-X", "POST", "-H", "Authorization: Bearer live-key",
                "--data-binary", f"@{tgz_path}",
                f"{base}/upload", "-w", "\n%{http_code}"])
    body, code = out.stdout.rsplit("\n", 1)
    check("POST /upload with key → 200 uploaded", code == "200"
          and '"status": "uploaded"' in body
          and '"chart": "live-app"' in body
          and '"version": "9.9.9"' in body, f"code={code} body={body[:300]}")
    check("ledger recorded the upload",
          "live-app/9.9.9" in server._ledger_instance.charts,
          str(server._ledger_instance.charts))

    print("3. duplicate version")
    out = curl(["-X", "POST", "-H", "Authorization: Bearer live-key",
                "--data-binary", f"@{tgz_path}",
                f"{base}/upload", "-w", "\n%{http_code}"])
    body, code = out.stdout.rsplit("\n", 1)
    check("re-upload → 409 with a bump/force hint", code == "409"
          and '"status": "exists"' in body and "force=true" in body,
          f"code={code} body={body[:300]}")

    print("4. force gate on an unmanaged chart")
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as fh:
        fh.write(tgz)   # same bytes; the ledger entry is popped below to force "unmanaged"
        force_path = fh.name
    server._ledger_instance.charts.pop("live-app/9.9.9", None)
    out = curl(["-X", "POST", "-H", "Authorization: Bearer live-key",
                "--data-binary", f"@{force_path}",
                f"{base}/upload?force=true", "-w", "\n%{http_code}"])
    body, code = out.stdout.rsplit("\n", 1)
    check("force=true on unmanaged → 403", code == "403"
          and "refusing to overwrite" in body, f"code={code} body={body[:300]}")

    print("5. UI + data API")
    out = curl([f"{base}/ui/", "-w", "\n%{http_code}"])
    code = out.stdout.rsplit("\n", 1)[-1]
    check("GET /ui/ → 200 html", code == "200", out.stdout[-120:])
    out = curl(["-H", "Authorization: Bearer live-key",
                f"{base}/api/managed", "-w", "\n%{http_code}"])
    body, code = out.stdout.rsplit("\n", 1)
    check("GET /api/managed → 200 JSON", code == "200"
          and '"apps"' in body and '"charts"' in body, f"code={code}")

    print("6. EzAppConfig staging (/manifest)")
    cr_path = os.path.join(tempfile.gettempdir(), "live-check-ezappconfig.yaml")
    with open(cr_path, "w") as f:
        f.write("apiVersion: ezconfig.hpe.ezaf.com/v1alpha1\n"
                "kind: EzAppConfig\n"
                "metadata:\n  name: live-check-app\n"
                "spec:\n  name: live-app\n  chartVersion: 9.9.9\n"
                "  install: true\n"
                "  options:\n    namespace: live-check-ns\n")
    out = curl(["-X", "POST", "-H", "Authorization: Bearer live-key",
                "--data-binary", f"@{cr_path}",
                f"{base}/manifest", "-w", "\n%{http_code}"])
    body, code = out.stdout.rsplit("\n", 1)
    check("POST /manifest → 200 with manifest_id", code == "200"
          and '"manifest_id"' in body and '"name": "live-check-app"' in body,
          f"code={code} body={body[:200]}")
    bad_path = os.path.join(tempfile.gettempdir(), "live-check-bad.yaml")
    with open(bad_path, "w") as f:
        f.write("apiVersion: v1\nkind: Secret\nmetadata:\n  name: nope\n")
    out = curl(["-X", "POST", "-H", "Authorization: Bearer live-key",
                "--data-binary", f"@{bad_path}",
                f"{base}/manifest", "-w", "\n%{http_code}"])
    body, code = out.stdout.rsplit("\n", 1)
    check("POST /manifest with a non-EzAppConfig → 400 with reason", code == "400"
          and "only kind" in body, f"code={code} body={body[:200]}")
    os.unlink(cr_path)
    os.unlink(bad_path)

    print("7. MCP path reaches the (dummy) MCP app")
    out = curl(["-X", "POST", "-H", "Authorization: Bearer live-key",
                f"{base}/mcp", "-w", "\n%{http_code}"])
    code = out.stdout.rsplit("\n", 1)[-1]
    check("POST /mcp with key → 200", code == "200", out.stdout[-120:])

    os.unlink(tgz_path)
    os.unlink(force_path)
    museum.stop()
    uv.should_exit = True
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
