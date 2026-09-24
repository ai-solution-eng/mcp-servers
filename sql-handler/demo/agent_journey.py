#!/usr/bin/env python3
"""agent_journey.py — the live "LLM analyst" demo driver.

One script, two stacks, the SAME business questions:

  A. sqlhandler MCP  — sqlhandler's agent surface
     (list_tables / search_tables / describe_table / profile_table /
      run_sql / async jobs / saved queries)
  B. ezpresto-mcp    — the EzPresto MCP server (today's agent-facing SQL):
     (list_catalogs / list_schemas / list_tables / get_table_schema /
      execute_query — no profile, no search, no jobs, no saved queries)

Each question runs top-down against BOTH stacks, narrating:
  1. what the agent SEES  (table docs + column meanings + stats vs bare names)
  2. what the agent CAN DO (guided workflows vs a single query tool)
  3. how LONG it takes    (per-question wall clock, warm vs cache-busted cold)
  4. the live data trap   (ezpresto's stale metastore says employees = 0)

Outputs per-stack JSON timelines: demo/journey_results.json and demo/journey_results.md.

Auth:
  sqlhandler needs the fleet API key (SQLHANDLER_API_KEYS / MCP_API_KEYS):
  pass --api-key or env SQLHANDLER_DEMO_KEY. Never printed or logged.
  ezpresto needs the UA bearer: --bearer, env EZPRESTO_BEARER, or bench/.bearer.
TLS: PCAI gateways present self-signed certs; --insecure (default) skips verify.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SQLH_URL = "https://sqlhandler.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp"
EZP_URL = "https://mcp-ezpresto-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp"
PRESTO_URL = "https://ezpresto.pcai-se-ai-application.hst.rdlabs.hpecorp.net/v1/statement"
DRIVER = "agent-journey/1.0"

# --------------------------------------------------------------- MCP client


class Mcp:
    """Minimal MCP streamable-HTTP client (JSON-RPC over POST /mcp)."""

    def __init__(self, url, bearer="", api_key="", timeout=180, insecure=True):
        self.url, self.bearer, self.api_key, self.timeout = url, bearer, api_key, timeout
        self.sid = None
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self._id = 0
        self._init()

    def _post(self, payload):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.sid:
            headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.sid = sid
            raw = r.read().decode()
        raw = raw.strip()
        if not raw:
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
        raise ValueError("unparseable MCP response: %r" % raw[:120])

    def _init(self):
        resp = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": DRIVER, "version": "1.0"}}})
        info = (resp.get("result") or {}).get("serverInfo") or {}
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return info

    def tools(self):
        self._id += 1
        resp = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/list"})
        return [t.get("name") for t in (resp.get("result") or {}).get("tools", [])]

    def call(self, tool, args):
        """tools/call -> (latency_ms, text, is_error)."""
        self._id += 1
        t0 = time.monotonic()
        resp = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
        dt = (time.monotonic() - t0) * 1000
        if resp.get("error"):
            raise RuntimeError("mcp error: %s" % json.dumps(resp["error"])[:200])
        result = resp.get("result") or {}
        text = "".join((c or {}).get("text", "")
                       for c in (result.get("content") or []))
        if result.get("isError"):
            raise RuntimeError("tool error: %s" % text[:300])
        return dt, text


def parse_rows(text, fmt):
    """Parse a run_sql markdown table (or JSON) into (columns, rows)."""
    if fmt == "json":
        d = json.loads(text)
        return d.get("columns", []), d.get("rows", [])
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    cols = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[2:]:
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    return cols, rows


def summarize(text, max_lines=4):
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    head = lines[:max_lines]
    if len(lines) > max_lines:
        head.append("… (%d more lines)" % (len(lines) - max_lines))
    return "\n".join(head)


# ------------------------------------------------------------------- stack A

class SqlhandlerStack:
    """sqlhandler MCP — the guided, caching, self-describing surface."""

    name = "sqlhandler-mcp"
    engine = "sqlhandler (DuckDB over pyarrow columnar scans)"

    def __init__(self, mcp):
        self.mcp = mcp

    def discover(self):
        steps = []
        t, txt = self.mcp.call("list_tables", {})
        steps.append(("list_tables", t, summarize(txt, 6)))
        # semantic catalog doc for orders + employees lands in the same listing
        return steps

    def find_table(self, query):
        t, txt = self.mcp.call("search_tables", {"query": query})
        return t, txt

    def describe(self, table):
        t, txt = self.mcp.call("describe_table", {"table": table})
        return t, txt

    def profile(self, table, columns=None):
        args = {"table": table}
        if columns:
            args["columns"] = columns
        t, txt = self.mcp.call("profile_table", args)
        return t, txt

    def column_stats(self, table, column):
        t, txt = self.mcp.call("column_stats", {"table": table, "column": column})
        return t, txt

    def run(self, sql, params=None, bust=None):
        if bust:
            sql = sql + " /* bust-%s */" % bust
        args = {"sql": sql, "output_format": "json"}
        if params:
            args["params"] = params
        t, txt = self.mcp.call("run_sql", args)
        return t, txt

    def run_rows(self, sql, bust=None):
        t, txt = self.run(sql, bust=bust)
        cols, rows = parse_rows(txt, "json")
        return t, cols, rows


class EzprestoStack:
    """ezpresto-mcp — the one-tool surface (execute_query), Presto dialect."""

    name = "ezpresto-mcp"
    engine = "EzPresto (PrestoDB over the MinIO hive catalog)"

    def __init__(self, mcp):
        self.mcp = mcp

    def discover(self):
        steps = []
        t, txt = self.mcp.call("list_catalogs", {})
        steps.append(("list_catalogs", t, summarize(txt, 3)))
        t, txt = self.mcp.call("list_schemas", {"catalog": "minio"})
        steps.append(("list_schemas(minio)", t, summarize(txt, 3)))
        t, txt = self.mcp.call("list_tables", {"catalog": "minio", "schema": "default"})
        steps.append(("list_tables(minio.default)", t, summarize(txt, 6)))
        return steps

    def find_table(self, keywords):
        # closest analog: list_tables and hope — no keyword search exists
        t, txt = self.mcp.call("list_tables", {"catalog": "minio", "schema": "default"})
        return t, "[no keyword search tool — full table list returned]\n" + summarize(txt, 6)

    def describe(self, table):
        t, txt = self.mcp.call("get_table_schema", {"catalog": "minio", "schema": "default",
                                                    "table": table})
        return t, txt

    def run(self, sql, bust=None):
        if bust:
            sql = sql + " /* bust-%s */" % bust
        t, txt = self.mcp.call("execute_query", {"query": sql})
        return t, txt

    def run_rows(self, sql, bust=None):
        t, txt = self.run(sql, bust=bust)
        try:
            d = json.loads(txt)
            rows = d if isinstance(d, list) else d.get("rows", [])
            cols = list(rows[0].keys()) if rows else []
            rows = [[r.get(c) for c in cols] for r in rows]
            return t, cols, rows
        except ValueError:
            return t, [], []


# --------------------------------------------------------------- the journey

# Each question: id, the business ask, and per-stack narration hooks.
QUESTIONS = [
    {
        "id": "q1_revenue_by_region",
        "ask": "What is our revenue by sales region?",
        "sqlh_sql": ("SELECT region, COUNT(*) AS orders, "
                     "SUM(unit_price * qty) AS revenue "
                     "FROM orders GROUP BY region ORDER BY revenue DESC"),
        "ezp_sql": ("SELECT region, COUNT(*) AS orders, "
                    "SUM(unit_price * qty) AS revenue "
                    "FROM minio.default.orders GROUP BY region ORDER BY revenue DESC"),
    },
    {
        "id": "q2_vip_revenue",
        "ask": "Which VIP customers generate the most revenue?",
        "sqlh_sql": ("SELECT c.customer_name, c.country, COUNT(*) AS orders, "
                     "SUM(o.unit_price * o.qty) AS revenue "
                     "FROM orders o JOIN customers c ON o.customer_id = c.customer_id "
                     "WHERE c.is_vip GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 5"),
        "ezp_sql": ("SELECT c.customer_name, c.country, COUNT(*) AS orders, "
                    "SUM(o.unit_price * o.qty) AS revenue "
                    "FROM minio.default.orders o JOIN minio.default.customers c "
                    "ON o.customer_id = c.customer_id "
                    "WHERE c.is_vip GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 5"),
    },
    {
        "id": "q3_headcount",   # the live trap: ezpresto's metastore says 0
        "ask": "How many employees do we have, by department?",
        "sqlh_sql": ("SELECT dept, COUNT(*) AS headcount, ROUND(AVG(salary), 0) AS avg_salary "
                     "FROM employees GROUP BY dept ORDER BY headcount DESC"),
        "ezp_sql": ("SELECT dept, COUNT(*) AS headcount, ROUND(AVG(salary), 0) AS avg_salary "
                    "FROM minio.default.employees GROUP BY dept ORDER BY headcount DESC"),
    },
    {
        "id": "q4_wide_agg",
        "ask": "Quick health check on the wide test table (3M rows, 50 cols).",
        "sqlh_sql": ("SELECT COUNT(*) AS n, SUM(quantity) AS total_qty, "
                     "MAX(supplier_id) AS max_supplier, MIN(brand_id) AS min_brand "
                     "FROM testdata_50col"),
        "ezp_sql": ("SELECT COUNT(*) AS n, SUM(quantity) AS total_qty, "
                    "MAX(supplier_id) AS max_supplier, MIN(brand_id) AS min_brand "
                    "FROM minio.default.testdata_50col"),
    },
    {
        "id": "q5_orders_per_hour",  # open-ended: needs schema discovery first
        "ask": "How have order volumes trended by day recently?",
        "sqlh_sql": ("SELECT CAST(order_ts AS DATE) AS day, COUNT(*) AS orders "
                     "FROM orders GROUP BY 1 ORDER BY day DESC LIMIT 7"),
        "ezp_sql": ("SELECT CAST(order_ts AS DATE) AS day, COUNT(*) AS orders "
                    "FROM minio.default.orders GROUP BY 1 ORDER BY day DESC LIMIT 7"),
    },
]


def narrate_discovery(stack, story):
    story.append("## Discovery — what the agent sees first (%s)" % stack.name)
    for tool, dt, out in stack.discover():
        story.append("- `%s` %.0f ms\n```text\n%s\n```" % (tool, dt, out))
    return story


def narrate_find(stack, query, story):
    t, txt = stack.find_table(query)
    story.append("- `find tables %r` → %s (%.0f ms)\n```text\n%s\n```"
                 % (query, stack.name, t, summarize(txt, 5)))


def run_question(stack, q, story, reps=1, bust=False):
    """Run one question; returns dict with per-rep latencies + the story bits."""
    entry = {"id": q["id"], "ask": q["ask"], "stack": stack.name, "reps": []}
    story.append("\n### %s — %s" % (q["id"], q["ask"]))
    for rep in range(reps):
        b = ("%d-%d" % (int(time.time()), rep)) if bust else None
        sql = q["sqlh_sql"] if isinstance(stack, SqlhandlerStack) else q["ezp_sql"]
        t0 = time.monotonic()
        try:
            t, cols, rows = stack.run_rows(sql, bust=b)
            dt = (time.monotonic() - t0) * 1000
            first = rows[0] if rows else []
            entry["reps"].append({"ms": round(dt, 1), "tool_ms": round(t, 1),
                                  "ok": True, "n_rows": len(rows)})
            story.append("- %s: **%.0f ms** — %d row(s): `%s`"
                         % ("cold" if bust else "warm", dt, len(rows),
                            ", ".join("%s=%s" % (c, v) for c, v in zip(cols, first))[:160]))
        except Exception as e:
            dt = (time.monotonic() - t0) * 1000
            entry["reps"].append({"ms": round(dt, 1), "ok": False, "error": str(e)[:200]})
            story.append("- %s: **FAILED after %.0f ms** — %s"
                         % ("cold" if bust else "warm", dt, str(e)[:220]))
    return entry


def race(stack, questions, levels=(1, 4), reps=1, bust=False):
    """Concurrent burst of the question pool at each level; returns qps stats."""
    out = []
    for level in levels:
        batch = []
        lock = threading.Lock()

        def one(sql):
            t0 = time.monotonic()
            try:
                stack.run_rows(sql, bust=("%d" % int(time.time() * 1000)) if bust else None)
                ok, lat = True, (time.monotonic() - t0) * 1000
            except Exception:
                ok, lat = False, (time.monotonic() - t0) * 1000
            with lock:
                batch.append((ok, lat))

        sqls = []
        for q in questions:
            for _ in range(level):
                sqls.append(q["sqlh_sql"] if isinstance(stack, SqlhandlerStack) else q["ezp_sql"])
        t0 = time.monotonic()
        threads = [threading.Thread(target=one, args=(s,)) for s in sqls]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        wall = time.monotonic() - t0
        lats = [lat for ok, lat in batch]
        ok_n = sum(1 for ok, _ in batch if ok)
        out.append({"level": level, "calls": len(sqls), "ok": ok_n,
                    "wall_s": round(wall, 2), "qps": round(len(sqls) / wall, 2) if wall else 0,
                    "p50_ms": round(statistics.median(lats), 1) if lats else None,
                    "p95_ms": round(sorted(lats)[int(0.95 * len(lats)) - 1], 1) if lats else None})
    return out


# ------------------------------------------------------------------- output

def render_md(results, path):
    lines = ["# Live agent journey — sqlhandler vs ezpresto-mcp (G2)",
             "", "Generated %s by `%s`." % (time.strftime("%Y-%m-%d %H:%M:%S"), DRIVER), ""]
    for stack_name, sres in results.items():
        lines.append("## %s" % stack_name)
        lines.append("")
        for q in sres["questions"]:
            reps = q["reps"]
            lat = [r["ms"] for r in reps if r.get("ok")]
            ok = sum(1 for r in reps if r.get("ok"))
            med = "%.0f ms" % statistics.median(lat) if lat else "n/a"
            lines.append("- **%s** — %s → %s (%d/%d ok)" % (q["id"], q["ask"], med, ok, len(reps)))
        for rc in sres["race"]:
            lines.append("- concurrency L%d: %.1f qps (p50 %s ms, %d/%d ok)"
                         % (rc["level"], rc["qps"], rc["p50_ms"], rc["ok"], rc["calls"]))
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api-key", default=os.environ.get("SQLHANDLER_DEMO_KEY", ""),
                    help="sqlhandler /mcp API key (env SQLHANDLER_DEMO_KEY)")
    ap.add_argument("--bearer", default=os.environ.get("EZPRESTO_BEARER", ""),
                    help="ezpresto UA bearer (env EZPRESTO_BEARER or bench/.bearer)")
    ap.add_argument("--questions", default=",".join(q["id"] for q in QUESTIONS),
                    help="comma-separated question ids to run")
    ap.add_argument("--reps", type=int, default=1, help="reps per question per mode")
    ap.add_argument("--levels", default="1,4", help="concurrency levels for the race")
    ap.add_argument("--skip-race", action="store_true")
    ap.add_argument("--stacks", default="both", choices=["both", "sqlhandler", "ezpresto"],
                    help="which stack(s) to run (default both)")
    ap.add_argument("--out", default=os.path.join(HERE, "journey_results.json"))
    args = ap.parse_args()

    bearer = args.bearer or open(os.path.join(HERE, "..", "bench", ".bearer")).read().strip()
    want_sh = args.stacks in ("both", "sqlhandler")
    want_ez = args.stacks in ("both", "ezpresto")
    if want_sh and not args.api_key:
        print("warning: no SQLHANDLER_DEMO_KEY/--api-key — skipping the sqlhandler leg "
              "(its /mcp requires the fleet API key)", file=sys.stderr)
        want_sh = False
        if not want_ez:
            sys.exit("error: nothing to run")

    sel = [q for q in QUESTIONS if q["id"] in set(args.questions.split(","))]

    results = {}
    for stack_cls, enabled in ((SqlhandlerStack, want_sh), (EzprestoStack, want_ez)):
        if not enabled:
            continue
        url = SQLH_URL if stack_cls is SqlhandlerStack else EZP_URL
        key = args.api_key if stack_cls is SqlhandlerStack else ""
        mcp = Mcp(url, bearer=bearer, api_key=key)
        stack = stack_cls(mcp)
        story = ["# Agent journey — %s" % stack.name,
                 "engine: %s" % stack.engine, ""]
        narrate_discovery(stack, story)
        narrate_find(stack, "revenue", story)

        questions = []
        for q in sel:
            questions.append(run_question(stack, q, story, reps=args.reps, bust=False))
        for q in sel:
            questions.extend([run_question(stack, q, story, reps=1, bust=True)])

        race_res = [] if args.skip_race else race(stack, sel, levels=tuple(
            int(x) for x in args.levels.split(",")), bust=True)
        for rc in race_res:
            story.append("- race L%d: %.1f qps (p50 %s ms, %d/%d ok)"
                         % (rc["level"], rc["qps"], rc["p50_ms"], rc["ok"], rc["calls"]))
        results[stack.name] = {"questions": questions, "race": race_res,
                               "story": story}
        with open(args.out, "w") as f:
            json.dump(results, f, indent=1)
        render_md(results, args.out.replace(".json", ".md"))
        print("\n".join(story))
        print("\n[saved %s + .md]" % args.out)


if __name__ == "__main__":
    main()
