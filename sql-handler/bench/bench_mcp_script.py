#!/usr/bin/env python3
"""In-cluster MCP-to-MCP head-to-head: sqlhandler (run_sql) vs ezpresto (execute_query).

Both legs speak MCP over streamable-HTTP against ClusterIP endpoints:
  sqlhandler : http://sqlhandler.sqlhandler.svc.cluster.local:9097/mcp  (X-API-Key)
  ezpresto   : http://mcp-ezpresto-server...svc:9097/mcp                (UA bearer)
Bootstrap: this file is fetched from MinIO at runtime by the Job pod.
"""
import json
import os
import statistics
import threading
import time
import urllib.request
import ssl

SH = "http://sqlhandler.sqlhandler.svc.cluster.local:9097/mcp"
EZP = "http://mcp-ezpresto-server.mcp-ezpresto-server.svc.cluster.local:9097/mcp"
SH_KEY = os.environ["SQLHANDLER_API_KEY"]
EZP_BEARER = os.environ.get("EZPRESTO_BEARER", "")

WL = [
    ("count_small", "SELECT COUNT(*) AS c FROM employees",
     "SELECT COUNT(*) AS c FROM minio.default.employees"),
    ("range_filter", "SELECT COUNT(*) AS c FROM orders WHERE order_id BETWEEN 100000 AND 100099",
     "SELECT COUNT(*) AS c FROM minio.default.orders WHERE order_id BETWEEN 100000 AND 100099"),
    ("filtered_agg", "SELECT region, COUNT(*) AS c, SUM(qty) AS s FROM orders WHERE unit_price > 250 GROUP BY region",
     "SELECT region, COUNT(*) AS c, SUM(qty) AS s FROM minio.default.orders WHERE unit_price > 250 GROUP BY region"),
    ("groupby_agg", "SELECT region, product, COUNT(*) AS c, AVG(unit_price) AS a FROM orders GROUP BY region, product",
     "SELECT region, product, COUNT(*) AS c, AVG(unit_price) AS a FROM minio.default.orders GROUP BY region, product"),
    ("join_count", "SELECT COUNT(*) AS c FROM orders o JOIN customers c ON o.customer_id = c.customer_id",
     "SELECT COUNT(*) AS c FROM minio.default.orders o JOIN minio.default.customers c ON o.customer_id = c.customer_id"),
    ("join_agg", "SELECT c.segment, COUNT(*) AS n, SUM(o.qty * o.unit_price) AS rev FROM orders o JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.segment",
     "SELECT c.segment, COUNT(*) AS n, SUM(o.qty * o.unit_price) AS rev FROM minio.default.orders o JOIN minio.default.customers c ON o.customer_id = c.customer_id GROUP BY c.segment"),
    ("count_big_3m", "SELECT COUNT(*) AS c FROM testdata_50col",
     "SELECT COUNT(*) AS c FROM minio.default.testdata_50col"),
    ("col_proj_agg_2cols", "SELECT SUM(quantity) AS s, AVG(returns_qty) AS a FROM testdata_50col",
     "SELECT SUM(quantity) AS s, AVG(returns_qty) AS a FROM minio.default.testdata_50col"),
    ("filtered_group_3m", "SELECT tenant_id, SUM(quantity) AS s FROM testdata_50col WHERE region_id = 11 GROUP BY tenant_id",
     "SELECT tenant_id, SUM(quantity) AS s FROM minio.default.testdata_50col WHERE region_id = 11 GROUP BY tenant_id"),
    ("wide_agg_4cols", "SELECT COUNT(*) AS c, SUM(quantity) AS s, MAX(supplier_id) AS mx, MIN(brand_id) AS mn FROM testdata_50col",
     "SELECT COUNT(*) AS c, SUM(quantity) AS s, MAX(supplier_id) AS mx, MIN(brand_id) AS mn FROM minio.default.testdata_50col"),
]

PARITY = [
    ("employees", "SELECT COUNT(*) AS c FROM employees",
     "SELECT COUNT(*) AS c FROM minio.default.employees"),
    ("orders", "SELECT COUNT(*) AS c FROM orders",
     "SELECT COUNT(*) AS c FROM minio.default.orders"),
    ("customers", "SELECT COUNT(*) AS c FROM customers",
     "SELECT COUNT(*) AS c FROM minio.default.customers"),
    ("testdata_50col", "SELECT COUNT(*) AS c FROM testdata_50col",
     "SELECT COUNT(*) AS c FROM minio.default.testdata_50col"),
]


def _parse(body):
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            p = line[5:].strip()
            if p and p != "{}":
                try:
                    return json.loads(p)
                except ValueError:
                    continue
    raise ValueError("unparseable MCP body")


class Mcp:
    def __init__(self, url, bearer="", api_key=""):
        self.url, self.bearer, self.api_key, self.sid = url, bearer, api_key, None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "mcp-bench", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, payload):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.bearer:
            h["Authorization"] = "Bearer " + self.bearer
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if self.sid:
            h["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=h)
        with urllib.request.urlopen(req, timeout=300, context=self.ctx) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.sid = sid
            body = r.read().decode()
        return _parse(body) if body.strip() else {}

    def call(self, tool, args):
        t0 = time.monotonic()
        resp = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
        if resp.get("error"):
            raise RuntimeError("mcp error: %s" % json.dumps(resp["error"])[:150])
        result = resp.get("result") or {}
        text = "".join((c or {}).get("text", "") for c in (result.get("content") or []))
        if result.get("isError"):
            raise RuntimeError("tool error: %s" % text[:200])
        if text.strip().startswith('{"status"'):
            raise RuntimeError("echo state")
        return (time.monotonic() - t0) * 1000, text


def count_from(text):
    """Extract the first numeric cell from a tool result (json or markdown)."""
    try:
        d = json.loads(text)
        rows = d if isinstance(d, list) else d.get("rows", [])
        if rows and isinstance(rows[0], (list, dict)):
            v = rows[0][0] if isinstance(rows[0], list) else list(rows[0].values())[0]
            return int(v)
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    for line in text.splitlines():
        if line.strip().startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            for c in cells:
                try:
                    return int(c)
                except ValueError:
                    continue
    return None


def median_ok(reps):
    ok = [x for x in reps if x]
    return round(statistics.median(ok), 1) if ok else None, "%d/5" % len(ok)


def pctl(vals, q):
    ok = sorted(x for x in vals if x)
    return round(ok[int(q * len(ok)) - 1], 1) if ok else None


def bench(sh, ezp, results):
    for label, client, tool, argof, pick in (
            ("sqlhandler-mcp", sh, "run_sql", lambda s: {"sql": s, "output_format": "json"}, 0),
            ("ezpresto-mcp", ezp, "execute_query", lambda s: {"query": s}, 1)):
        out = {"leg": label}
        lat_w, lat_c = [], []
        for name, sq, eq in WL:
            sql = (sq, eq)[pick]
            rep_w, rep_c, first_err = [], [], None
            for rep in range(5):
                try:
                    dt, _ = client.call(tool, argof(sql))
                    rep_w.append(round(dt, 1))
                except Exception as e:
                    rep_w.append(None)
                    if first_err is None:
                        first_err = str(e)[:160]
                try:
                    dt, _ = client.call(tool, argof(sql + " /* bust-%d */" % rep))
                    rep_c.append(round(dt, 1))
                except Exception:
                    rep_c.append(None)
            if first_err:
                out.setdefault("errors", {})[name] = first_err
            w50, wok = median_ok(rep_w)
            c50, cok = median_ok(rep_c)
            lat_w.append({"q": name, "p50": w50, "ok": wok})
            lat_c.append({"q": name, "p50": c50, "ok": cok})
        out["latency_warm"], out["latency_cold"] = lat_w, lat_c
        jobs = [(sq, eq)[pick] for _, sq, eq in WL][:8]
        res, lock = [], threading.Lock()

        def run(sql):
            try:
                dt, _ = client.call(tool, argof(sql))
                with lock:
                    res.append(dt)
            except Exception:
                pass

        t0 = time.monotonic()
        ths = [threading.Thread(target=run, args=(s,)) for s in jobs]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.monotonic() - t0
        out["conc_L4"] = {"qps": round(len(res) / wall, 2) if wall else 0,
                          "p50": round(statistics.median(res), 1) if res else None,
                          "p95": pctl(res, 0.95), "ok": len(res)}
        results[label] = out


def main():
    results = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "mode": "MCP-to-MCP in-cluster: sqlhandler run_sql (X-API-Key) vs "
                       "ezpresto execute_query (UA bearer); both ClusterIP"}
    sh = Mcp(SH, api_key=SH_KEY)
    ezp = Mcp(EZP, bearer=EZP_BEARER)

    parity = {}
    for name, sq, eq in PARITY:
        _, t1 = sh.call("run_sql", {"sql": sq, "output_format": "json"})
        _, t2 = ezp.call("execute_query", {"query": eq})
        a, b = count_from(t1), count_from(t2)
        parity[name] = {"sqlhandler": a, "ezpresto": b, "equal": a == b}
        print("parity|%s|sh=%s|ezp=%s|%s" % (name, a, b, "EQUAL" if a == b else "MISMATCH"))
    results["parity"] = parity

    bench(sh, ezp, results)

    print("BENCH_JSON_BEGIN")
    print(json.dumps(results, indent=1))
    print("BENCH_JSON_END")
    print("COMPACT_BEGIN")
    for leg_name in ("sqlhandler-mcp", "ezpresto-mcp"):
        leg = results.get(leg_name, {})
        if "error" in leg:
            print("%s|ERROR|%s" % (leg_name, leg["error"]))
            continue
        for w, c in zip(leg.get("latency_warm", []), leg.get("latency_cold", [])):
            print("%s|%s|warm=%s|cold=%s|%s" % (leg_name, w["q"], w["p50"], c["p50"], w["ok"]))
        cl = leg.get("conc_L4", {})
        print("%s|conc|qps=%s|p50=%s|p95=%s|ok=%s" % (leg_name, cl.get("qps"),
                                                      cl.get("p50"), cl.get("p95"), cl.get("ok")))
    print("COMPACT_END")


if __name__ == "__main__":
    main()
