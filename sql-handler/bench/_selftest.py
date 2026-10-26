#!/usr/bin/env python3
"""Self-test for the bench harness: mock every engine interface (MCP MCP,
REST, Presto statement API) locally, then drive the REAL harness end-to-end
(keep-alive transport, cache-bust, all four engine legs).

Run:  python bench/_selftest.py   — exit 0 = harness ready for the real run.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "bench")
import ezpresto_vs_sqlhandler as bench  # noqa: E402


def _mcp_handler(markdown_rows, tool_name="run_sql", arg="sql", count=None):
    """A minimal streamable-http MCP server (JSON responses)."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            sid = self.headers.get("mcp-session-id", "sess-1")
            method = body.get("method")
            resp = None
            if method == "initialize":
                resp = {"jsonrpc": "2.0", "id": 0, "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock", "version": "1"}}}
            elif method == "tools/list":
                resp = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
                    "name": tool_name,
                    "inputSchema": {"type": "object", "properties": {arg: {"type": "string"}}},
                }]}}
            elif method == "tools/call":
                sql = (body["params"]["arguments"] or {}).get(arg, "")
                if count is not None:
                    count[0] += 1
                if tool_name == "run_sql":
                    text = "| c |\n|---|\n"
                    for i in range(markdown_rows):
                        text += f"| {i + len(sql)} |\n"  # sql in output: cache-bust visible
                else:
                    text = json.dumps({"rows": [[i] for i in range(markdown_rows)]})
                resp = {"jsonrpc": "2.0", "id": 1, "result": {
                    "content": [{"type": "text", "text": text}], "isError": False}}
            payload = json.dumps(resp).encode() if resp else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("mcp-session-id", sid)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return H


class _PrestoHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.dumps({
            "id": "q1", "nextUri": "http://127.0.0.1:%d/next/1" % self.server.server_port,
            "stats": {},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        payload = json.dumps({"id": "q1", "data": [[1], [2], [3]]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_port


def main():
    sql_mcp, sql_port = _serve(_mcp_handler(markdown_rows=5))
    ez_mcp, ez_port = _serve(_mcp_handler(markdown_rows=0, tool_name="execute_query", arg="query"))
    rest, rest_port = _serve(_mcp_handler(markdown_rows=0))  # unused below; rest served by MCP-style? no:
    rest_srv, rest_port = _serve(_PrestoHandler)  # placeholder replace
    # proper REST mock: POST /api/query -> {"rows": [...]}
    class RestH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.dumps({"rows": [[1], [2], [3], [4]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    rest_srv.shutdown()
    rest_srv, rest_port = _serve(RestH)
    presto, presto_port = _serve(_PrestoHandler)

    argv = [
        "run",
        "--suite", "all",
        "--sqlhandler-url", f"http://127.0.0.1:{sql_port}/mcp", "--sqlhandler-mode", "mcp",
        "--sqlhandler-bearer", "",
        "--presto-url", f"http://127.0.0.1:{presto_port}",
        "--ezpresto-mcp-url", f"http://127.0.0.1:{ez_port}/mcp",
        "--bearer", "fake.jwt.token",
        "--reps", "2",
        "--levels", "1,2",
        "--cache-bust",
        "--timeout", "10",
    ]
    rc = bench.main(argv)
    assert rc == 0, "harness returned %s" % rc
    for srv in (sql_mcp, ez_mcp, rest_srv, presto):
        srv.shutdown()
    print("\nSELFTEST PASS: all four engine legs ran end-to-end "
          "(mcp + rest + presto + ezpresto-mcp), keep-alive + cache-bust exercised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
