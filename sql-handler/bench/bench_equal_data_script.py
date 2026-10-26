#!/usr/bin/env python3
"""In-cluster equal-data head-to-head: sqlhandler (REST twin) vs ezpresto (MCP).

Runs on a Job pod in the sqlhandler namespace against ClusterIP endpoints:
  sqlhandler : http://sqlhandler.sqlhandler.svc.cluster.local:9097  (REST twin —
               v2.3.2 gates /mcp behind the fleet API key in-cluster too; the
               REST path is the same engine, caches, and pods the MCP tools use)
  ezpresto   : http://mcp-ezpresto-server...svc:9097/mcp            (MCP execute_query)

Data-equality gate: row counts for the three demo tables are printed; the
September run's count_small caveat (empty employees mirror) is now fixed.
"""
import json
import os
import statistics
import threading
import time
import urllib.request
import http.client

SH_HOST = "sqlhandler.sqlhandler.svc.cluster.local"
SH_PORT = 9097
EZP = "http://mcp-ezpresto-server.mcp-ezpresto-server.svc.cluster.local:9097/mcp"

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

PARITY_SQL = [
    ("employees", "SELECT COUNT(*) AS c FROM employees",
     "SELECT COUNT(*) AS c FROM minio.default.employees"),
    ("orders", "SELECT COUNT(*) AS c FROM orders",
     "SELECT COUNT(*) AS c FROM minio.default.orders"),
    ("customers", "SELECT COUNT(*) AS c FROM customers",
     "SELECT COUNT(*) AS c FROM minio.default.customers"),
    ("testdata_50col", "SELECT COUNT(*) AS c FROM testdata_50col",
     "SELECT COUNT(*) AS c FROM minio.default.testdata_50col"),
]


def rest_query(sql):
    c = http.client.HTTPConnection(SH_HOST, SH_PORT, timeout=300)
    body = json.dumps({"sql": sql}).encode()
    t0 = time.monotonic()
    c.request("POST", "/api/query", body=body,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    d = json.loads(r.read())
    c.close()
    return (time.monotonic() - t0) * 1000, d


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
    def __init__(self, url, bearer=""):
        self.url, self.bearer, self.sid = url, bearer, None
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "incluster-bench", "version": "2.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, payload):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        if self.sid:
            headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=300) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.sid = sid
            body = r.read().decode()
        return _parse(body) if body.strip() else {}

    def query(self, sql, tool="execute_query", arg="query"):
        t0 = time.monotonic()
        resp = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": {arg: sql}}})
        if resp.get("error"):
            raise RuntimeError("mcp error")
        result = resp.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            raise RuntimeError("sql error: %s" % text[:150])
        if text.strip().startswith('{"status"'):
            raise RuntimeError("echo state")
        rows = 0
        try:
            data = json.loads(text)
            rows = len(data if isinstance(data, list) else data.get("rows") or [])
        except (ValueError, TypeError):
            lines = [l for l in text.splitlines() if l.strip().startswith("|")]
            rows = max(0, len(lines) - 2)
        return time.monotonic() - t0, rows


def median_ok(reps):
    ok = [x for x in reps if x]
    return round(statistics.median(ok), 1) if ok else None, "%d/5" % len(ok)


def pctl(vals, q):
    ok = sorted(x for x in vals if x)
    return round(ok[int(q * len(ok)) - 1], 1) if ok else None


def main():
    results = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "mode": "sqlhandler=REST twin (v2.3.2 API-key gate on /mcp in-cluster); "
                       "ezpresto=MCP execute_query; both ClusterIP in-cluster"}

    # ---------------- data-equality gate ----------------
    parity = {}
    for name, sq, eq in PARITY_SQL:
        _, d = rest_query(sq)
        sh_n = d.get("rows", [[None]])[0][0]
        eng = Mcp(EZP, os.environ.get("EZPRESTO_BEARER", ""))
        _, txt = eng.query(eq)
        try:
            ez = json.loads(txt)
            ez_n = (ez if isinstance(ez, list) else ez.get("rows", [[None]]))[0][0]
        except (ValueError, TypeError, IndexError):
            ez_n = None
        parity[name] = {"sqlhandler": sh_n, "ezpresto": ez_n,
                        "equal": sh_n == ez_n}
        print("parity|%s|sh=%s|ezp=%s|%s" % (name, sh_n, ez_n,
                                             "EQUAL" if sh_n == ez_n else "MISMATCH"))
    results["parity"] = parity

    # ---------------- sqlhandler leg (REST twin) ----------------
    out = {"leg": "sqlhandler-rest"}
    lat_w, lat_c = [], []
    for name, sq, eq in WL:
        rep_w, rep_c, first_err = [], [], None
        for rep in range(5):
            try:
                dt, _ = rest_query(sq)
                rep_w.append(round(dt, 1))
            except Exception as e:
                rep_w.append(None)
                if first_err is None:
                    first_err = str(e)[:160]
            try:
                dt, _ = rest_query(sq + " /* bust-%d */" % rep)
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
    jobs = [sq for _, sq, _ in WL][:8]
    res, lock = [], threading.Lock()

    def run(sql):
        try:
            dt, _ = rest_query(sql)
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
    results["sqlhandler"] = out

    # ---------------- ezpresto leg (MCP) ----------------
    eng = Mcp(EZP, os.environ.get("EZPRESTO_BEARER", ""))
    out = {"leg": "ezpresto-mcp"}
    try:
        eng.query("SELECT 1")
    except Exception as e:
        out["error"] = "smoke: %s" % str(e)[:150]
        results["ezpresto-mcp"] = out
    else:
        lat_w, lat_c = [], []
        for name, sq, eq in WL:
            rep_w, rep_c, first_err = [], [], None
            for rep in range(5):
                try:
                    w, _ = eng.query(eq)
                    rep_w.append(round(w * 1000, 1))
                except Exception as e:
                    rep_w.append(None)
                    if first_err is None:
                        first_err = str(e)[:160]
                try:
                    w, _ = eng.query(eq + " /* bust-%d */" % rep)
                    rep_c.append(round(w * 1000, 1))
                except Exception:
                    rep_c.append(None)
            if first_err:
                out.setdefault("errors", {})[name] = first_err
            w50, wok = median_ok(rep_w)
            c50, cok = median_ok(rep_c)
            lat_w.append({"q": name, "p50": w50, "ok": wok})
            lat_c.append({"q": name, "p50": c50, "ok": cok})
        out["latency_warm"], out["latency_cold"] = lat_w, lat_c
        jobs = [eq for _, _, eq in WL][:8]
        res, lock = [], threading.Lock()

        def run2(sql):
            try:
                w, _ = eng.query(sql)
                with lock:
                    res.append(w * 1000)
            except Exception:
                pass

        t0 = time.monotonic()
        ths = [threading.Thread(target=run2, args=(s,)) for s in jobs]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.monotonic() - t0
        out["conc_L4"] = {"qps": round(len(res) / wall, 2) if wall else 0,
                          "p50": round(statistics.median(res), 1) if res else None,
                          "p95": pctl(res, 0.95), "ok": len(res)}
        results["ezpresto-mcp"] = out

    print("BENCH_JSON_BEGIN")
    print(json.dumps(results, indent=1))
    print("BENCH_JSON_END")
    print("COMPACT_BEGIN")
    for leg_name in ("sqlhandler", "ezpresto-mcp"):
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
