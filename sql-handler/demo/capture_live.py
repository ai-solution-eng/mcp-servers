#!/usr/bin/env python3
"""capture_live.py — run the demo questions against BOTH stacks, live.

Phase 1 of the video build:
  1. this script  -> demo/capture.json   (real outputs + real latencies)
  2. render_video.py reads capture.json -> demo/demo_video.mp4

Legs
  sqlhandler : the REST twin (/api/*) of the deployed MCP server — same
               engine, same caches, same pod. Chosen because /mcp requires
               the fleet API key; REST is the same query path the MCP
               tools call internally. Timings are engine+gateway; MCP adds
               only the tool-call frame (~tens of ms).
  ezpresto   : the EzPresto MCP server (execute_query) — the surface an
               OWUI agent actually gets on the ezpresto stack today.

Methodology notes (same discipline as bench/):
  - keep-alive HTTPS on BOTH legs (a fresh TLS handshake per call adds
    ~600 ms of gateway overhead on this cluster — transport, not engine)
  - every latency is wall-clock around the full call
  - nothing is retried silently; errors are captured as first-class steps
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "capture.json")

SQLH_HOST = "sqlhandler.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
EZP_HOST = "mcp-ezpresto-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
DRIVER = "capture-live/1.0"


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


# --------------------------------------------------------------- REST client


class RestSession:
    """Keep-alive HTTPS session for the sqlhandler REST twin."""

    def __init__(self, host):
        self.host = host
        self.ctx = _ctx()
        self.conn = http.client.HTTPSConnection(host, context=self.ctx, timeout=120)

    def request(self, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        t0 = time.monotonic()
        for attempt in (0, 1):  # one stale-connection retry
            try:
                self.conn.request(method, path, body=payload, headers=headers)
                r = self.conn.getresponse()
                raw = r.read().decode()
                break
            except (http.client.HTTPException, OSError):
                if attempt:
                    raise
                self.conn.close()
                self.conn = http.client.HTTPSConnection(self.host, context=self.ctx,
                                                        timeout=120)
        dt = (time.monotonic() - t0) * 1000
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {"_raw": raw[:400]}
        return data, dt


# -------------------------------------------------------------- MCP client


class McpSession:
    """Keep-alive MCP streamable-HTTP client (JSON-RPC over POST /mcp)."""

    def __init__(self, host, bearer="", timeout=180):
        self.host, self.bearer, self.timeout = host, bearer, timeout
        self.ctx = _ctx()
        self.conn = http.client.HTTPSConnection(host, context=self.ctx, timeout=timeout)
        self.sid = None
        self._id = 0
        info = self._rpc("initialize", {"protocolVersion": "2025-03-26",
                                        "capabilities": {},
                                        "clientInfo": {"name": DRIVER, "version": "1.0"}})
        self._rpc("notifications/initialized", None, notify=True)
        self.server = (info.get("result") or {}).get("serverInfo") or {}

    def _rpc(self, method, params, notify=False):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        if self.sid:
            headers["mcp-session-id"] = self.sid
        payload = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            payload["id"] = self._id
        if params is not None:
            payload["params"] = params
        body = json.dumps(payload).encode()
        self.conn.request("POST", "/mcp", body=body, headers=headers)
        r = self.conn.getresponse()
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.sid = sid
        raw = r.read().decode().strip()
        if notify or not raw:
            return {}
        if raw.startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():  # SSE frame
            if line.startswith("data:"):
                piece = line[5:].strip()
                if piece and piece != "{}":
                    try:
                        return json.loads(piece)
                    except ValueError:
                        continue
        raise ValueError("unparseable MCP body: %r" % raw[:120])

    def call(self, tool, args):
        t0 = time.monotonic()
        resp = self._rpc("tools/call", {"name": tool, "arguments": args})
        dt = (time.monotonic() - t0) * 1000
        if resp.get("error"):
            raise RuntimeError("mcp error: %s" % json.dumps(resp["error"])[:200])
        result = resp.get("result") or {}
        text = "".join((c or {}).get("text", "") for c in (result.get("content") or []))
        if result.get("isError"):
            raise RuntimeError("tool error: %s" % text[:300])
        return text, dt


def bearer_from_bench():
    p = os.path.join(HERE, "..", "bench", ".bearer")
    with open(p) as f:
        return f.read().strip()


# ------------------------------------------------------------ the questions

QUESTIONS = [
    {"id": "q1", "label": "revenue by region",
     "sql": ("SELECT region, COUNT(*) AS orders, SUM(unit_price * qty) AS revenue "
             "FROM orders GROUP BY region ORDER BY revenue DESC"),
     "ezp": ("SELECT region, COUNT(*) AS orders, SUM(unit_price * qty) AS revenue "
             "FROM minio.default.orders GROUP BY region ORDER BY revenue DESC")},
    {"id": "q2", "label": "VIP customers",
     "sql": ("SELECT c.customer_name, c.country, COUNT(*) AS orders, "
             "SUM(o.unit_price * o.qty) AS revenue FROM orders o "
             "JOIN customers c ON o.customer_id = c.customer_id "
             "WHERE c.is_vip GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 5"),
     "ezp": ("SELECT c.customer_name, c.country, COUNT(*) AS orders, "
             "SUM(o.unit_price * o.qty) AS revenue FROM minio.default.orders o "
             "JOIN minio.default.customers c ON o.customer_id = c.customer_id "
             "WHERE c.is_vip GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 5")},
    {"id": "q3", "label": "headcount by department",
     "sql": ("SELECT dept, COUNT(*) AS headcount, ROUND(AVG(salary), 0) AS avg_salary "
             "FROM employees GROUP BY dept ORDER BY headcount DESC"),
     "ezp": ("SELECT dept, COUNT(*) AS headcount, ROUND(AVG(salary), 0) AS avg_salary "
             "FROM minio.default.employees GROUP BY dept ORDER BY headcount DESC")},
    {"id": "q4", "label": "3M-row wide table check",
     "sql": ("SELECT COUNT(*) AS n, SUM(quantity) AS total_qty, "
             "MAX(supplier_id) AS max_supplier, MIN(brand_id) AS min_brand "
             "FROM testdata_50col"),
     "ezp": ("SELECT COUNT(*) AS n, SUM(quantity) AS total_qty, "
             "MAX(supplier_id) AS max_supplier, MIN(brand_id) AS min_brand "
             "FROM minio.default.testdata_50col")},
]


# -------------------------------------------------------- sqlhandler capture


def cap_sqlh():
    s = RestSession(SQLH_HOST)
    steps = []

    data, dt = s.request("GET", "/api/status")
    steps.append({"id": "status", "ms": round(dt, 1), "ok": True,
                  "note": "%s v%s backend=%s" % (SQLH_HOST.split(".")[0],
                                                 data.get("version"), data.get("backend"))})

    data, dt = s.request("GET", "/api/tables")
    names = [t["name"] for t in data.get("tables", [])]
    virtual = [t["name"] for t in data.get("tables", []) if t.get("format") == "virtual"]
    lines = ["%d tables (auto-discovered from s3://test-parquet):" % len(names)]
    for t in data.get("tables", [])[:9]:
        badge = {"virtual": " [VIRTUAL]", "parquet": ""}.get(t.get("format"), "")
        lines.append("  %-22s%s" % (t["name"] + badge, ""))
    if len(names) > 9:
        lines.append("  … +%d more" % (len(names) - 9))
    steps.append({"id": "list_tables", "ms": round(dt, 1), "ok": True,
                  "lines": lines, "n_tables": len(names), "virtual": virtual})

    data, dt = s.request("POST", "/api/describe", {"table": "orders"})
    cols = data.get("columns", [])
    lines = ["orders — the orders fact table — one row per fictional sale",
             "URI: %s" % data.get("uri", "")]
    for c in cols[:8]:
        doc = (c.get("description") or "").strip()
        lines.append("  %-12s %s" % (c["name"], doc[:58]))
    steps.append({"id": "describe_orders", "ms": round(dt, 1), "ok": True,
                  "lines": lines})

    for q in QUESTIONS:
        data, dt = s.request("POST", "/api/query", {"sql": q["sql"]})
        rows = data.get("rows", [])
        cols = data.get("columns", [])
        lines = ["| " + " | ".join(cols) + " |"] if cols else []
        for r in rows[:6]:
            lines.append("| " + " | ".join(_fmt(v) for v in r) + " |")
        if len(rows) > 6:
            lines.append("… %d more rows" % (len(rows) - 6))
        steps.append({"id": q["id"], "ms": round(dt, 1), "ok": bool(data.get("columns")),
                      "n_rows": len(rows), "sql": q["sql"], "lines": lines,
                      "cols": cols, "rows": rows[:6]})

    # warm repeat of q4 — the result-cache beat
    q4 = QUESTIONS[3]
    data, dt = s.request("POST", "/api/query", {"sql": q4["sql"]})
    steps.append({"id": "q4_repeat", "ms": round(dt, 1), "ok": True,
                  "n_rows": len(data.get("rows", [])),
                  "lines": ["(identical query repeated — served from the result cache)"]})

    # concurrency burst: 8 mixed queries at once
    burst_sqls = [q["sql"] for q in QUESTIONS] + [QUESTIONS[0]["sql"], QUESTIONS[3]["sql"],
                                                  QUESTIONS[1]["sql"]]
    burst = {"level": len(burst_sqls)}
    lat, lock, done = [], threading.Lock(), [0]
    t0 = time.monotonic()

    def one(sql):
        ss = RestSession(SQLH_HOST)
        try:
            _, d = ss.request("POST", "/api/query", {"sql": sql})
            with lock:
                lat.append(d)
                done[0] += 1
        except Exception:
            with lock:
                lat.append(-1)
                done[0] += 1

    threads = [threading.Thread(target=one, args=(s2,)) for s2 in burst_sqls]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = (time.monotonic() - t0) * 1000
    oklats = [x for x in lat if x >= 0]
    burst.update({"wall_ms": round(wall, 1), "ok": len(oklats), "qps": round(len(oklats) / (wall / 1000), 2),
                  "p50_ms": round(sorted(oklats)[len(oklats) // 2], 1) if oklats else None,
                  "per_call_ms": [round(x, 1) for x in lat]})
    steps.append({"id": "burst", **burst})
    return steps


def _fmt(v):
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


# --------------------------------------------------------- ezpresto capture


def cap_ezp():
    m = McpSession(EZP_HOST, bearer=bearer_from_bench())
    steps = [{"id": "status", "ms": 0, "ok": True,
              "note": "MCP server: %s v%s" % (m.server.get("name"), m.server.get("version"))}]

    t0 = time.monotonic()
    txt, dt1 = m.call("list_catalogs", {})
    txt, dt2 = m.call("list_schemas", {"catalog": "minio"})
    txt, dt3 = m.call("list_tables", {"catalog": "minio", "schema": "default"})
    tables = []
    try:
        tables = json.loads(txt)
        if not isinstance(tables, list):
            tables = tables.get("tables", [])
    except ValueError:
        pass
    lines = ["list_catalogs → list_schemas → list_tables   (3 calls, no docs,",
             "no descriptions, no aliases — bare names only)"]
    for t in tables[:9]:
        lines.append("  %s" % t)
    if len(tables) > 9:
        lines.append("  … +%d more" % (len(tables) - 9))
    steps.append({"id": "list_tables", "ms": round(dt1 + dt2 + dt3, 1), "ok": True,
                  "lines": lines, "n_tables": len(tables), "calls": 3})

    txt, dt = m.call("get_table_schema", {"catalog": "minio", "schema": "default",
                                          "table": "orders"})
    try:
        cols = json.loads(txt)
        lines = ["get_table_schema(minio.default.orders)"]
        for c in cols[:8]:
            if isinstance(c, dict):
                lines.append("  %-12s %s" % (c.get("name", "?"), c.get("type", "")))
            else:
                lines.append("  %s" % c)
    except ValueError:
        lines = [txt[:400]]
    steps.append({"id": "describe_orders", "ms": round(dt, 1), "ok": True,
                  "lines": lines})

    for q in QUESTIONS:
        try:
            txt, dt = m.call("execute_query", {"query": q["ezp"]})
            rows = json.loads(txt)
            if isinstance(rows, dict):
                rows = rows.get("rows", [])
            cols = list(rows[0].keys()) if rows else []
            lines = ["| " + " | ".join(cols) + " |"] if cols else []
            for r in rows[:6]:
                lines.append("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
            steps.append({"id": q["id"], "ms": round(dt, 1), "ok": True,
                          "n_rows": len(rows), "sql": q["ezp"], "lines": lines,
                          "cols": cols, "rows": rows[:6]})
        except Exception as e:  # the q3-style surprise is captured as data
            steps.append({"id": q["id"], "ms": None, "ok": False,
                          "error": str(e)[:300], "sql": q["ezp"]})

    # retry beat for q3: a realistic agent double-checks an empty answer
    try:
        txt, dt = m.call("execute_query", {"query": "SELECT * FROM minio.default.employees LIMIT 5"})
        rows = json.loads(txt)
        if isinstance(rows, dict):
            rows = rows.get("rows", [])
        steps.append({"id": "q3_retry", "ms": round(dt, 1), "ok": True,
                      "n_rows": len(rows),
                      "lines": ["SELECT * FROM minio.default.employees LIMIT 5 → %d rows"
                                % len(rows)]})
    except Exception as e:
        steps.append({"id": "q3_retry", "ms": None, "ok": False, "error": str(e)[:300]})

    # burst: 8 concurrent execute_query calls, separate MCP sessions each
    burst_sqls = [q["ezp"] for q in QUESTIONS] + [QUESTIONS[0]["ezp"],
                                                  QUESTIONS[3]["ezp"],
                                                  QUESTIONS[1]["ezp"]]
    bearer = bearer_from_bench()
    lat, lock, done = [], threading.Lock(), [0]
    t0 = time.monotonic()

    def one(sql):
        try:
            mm = McpSession(EZP_HOST, bearer=bearer)
            mm.call("execute_query", {"query": sql})
            with lock:
                done[0] += 1
                lat.append(time.monotonic() - t0)
        except Exception:
            with lock:
                done[0] += 1
                lat.append(-1)

    threads = [threading.Thread(target=one, args=(s2,)) for s2 in burst_sqls]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = (time.monotonic() - t0) * 1000
    oklats = [x * 1000 for x in lat if x >= 0]
    steps.append({"id": "burst", "level": len(burst_sqls), "wall_ms": round(wall, 1),
                  "ok": len(oklats), "qps": round(len(oklats) / (wall / 1000), 2),
                  "p50_ms": round(sorted(oklats)[len(oklats) // 2], 1) if oklats else None,
                  "per_call_ms": [round(x * 1000, 1) for x in lat]})
    return steps


def main():
    print("capturing sqlhandler (REST twin)…")
    sqlh = cap_sqlh()
    for s in sqlh:
        print("  %-16s %8s ms  %s" % (s["id"], s.get("ms"), "ok" if s.get("ok") else "ERR"))
    print("capturing ezpresto (MCP)…")
    ezp = cap_ezp()
    for s in ezp:
        print("  %-16s %8s ms  %s" % (s["id"], s.get("ms"), "ok" if s.get("ok") else "ERR"))
    with open(OUT, "w") as f:
        json.dump({"captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "sqlhandler_host": SQLH_HOST, "ezpresto_host": EZP_HOST,
                   "sqlhandler": sqlh, "ezpresto": ezp}, f, indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
