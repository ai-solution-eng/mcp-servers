#!/usr/bin/env python3
"""Benchmark SQLhandler vs EzPresto (PrestoDB) against the same dataset.

Both engines are driven over their native client interfaces:

  * SQLhandler — MCP streamable-http (`tools/call` -> `run_sql`) on /mcp,
    or the REST API on /api/query.
  * EzPresto  — its own MCP streamable-http server (`execute_query` tool;
    tool/argument auto-discovered from tools/list), or the native Presto
    HTTP API: POST /v1/statement, poll `nextUri` until the query finishes.

All HTTP goes through a persistent-connection client (one pooled TLS
connection per thread, one retry on stale) — a fresh connection per request
adds hundreds of ms of gateway handshake per call, which measures the
transport instead of the engines.

Suites:
  latency      run a fixed query mix N times per engine; report p50/p95/min/max
  concurrency  fixed query pool at levels 1,2,4,...; report wall, qps, p50/p95
  throughput   full-scan aggregates; report rows/s and query wall time

`--cache-bust` appends a unique SQL comment per rep: sqlhandler's result
cache keys on raw SQL text, so reps become cache misses (cold engine
numbers) while the workload stays semantically identical. Default off —
warm repeats are the honest agent-facing experience (Snowflake-style result
cache), but report both numbers.

Results are printed as markdown tables and dumped as JSON for comparison.

Examples:
  export EZPRESTO_BEARER=<keycloak UA access token>
  python bench/ezpresto_vs_sqlhandler.py probe
  python bench/ezpresto_vs_sqlhandler.py run --suite all \
      --workload bench/workload_g2.json \
      --sqlhandler-url https://sqlhandler.<domain>/mcp --sqlhandler-mode mcp \
      --reps 5 --levels 1,4,8,16 --json-out bench/results.json
  python bench/ezpresto_vs_sqlhandler.py run --suite latency --cache-bust --reps 5

Stdlib only.
"""

import argparse
import concurrent.futures
import http.client
import json
from pathlib import Path
import os
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE  # internal PCAI CA; HTTP API only, read-only queries

DEFAULT_WORKLOAD = os.path.join(os.path.dirname(__file__), "workload_g2.json")


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------

class _Http:
    """Persistent-connection HTTP client (stdlib http.client).

    The harness originally opened a fresh TCP+TLS connection per request —
    through the cluster gateway that is ~1-3 extra round-trips (~hundreds of
    ms) on EVERY call, which measured the transport, not the engines. This
    client keeps one connection alive per thread (agent sessions are
    long-lived; the concurrency suite reuses its worker threads) and retries
    once on a stale pooled connection. ALL engines use it, so the comparison
    stays symmetric.
    """

    def __init__(self, url, timeout=300, bearer=""):
        from urllib.parse import urlparse

        u = urlparse(url)
        self.scheme = u.scheme or "https"
        self.base = u.path.rstrip("/")
        self.host = u.hostname
        self.port = u.port or (443 if self.scheme == "https" else 80)
        self.timeout = timeout
        self.bearer = bearer
        self._local = threading.local()

    def _resolve(self, url):
        """Split an absolute URL or a base-relative path."""
        from urllib.parse import urlparse

        if url.startswith(("http://", "https://")):
            u = urlparse(url)
            scheme = u.scheme
            host = u.hostname
            port = u.port or (443 if scheme == "https" else 80)
            return scheme, host, port, u.path or "/"
        return self.scheme, self.host, self.port, url

    def _conn(self, scheme, host, port):
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        conn = pool.get((scheme, host, port))
        if conn is None:
            if scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=CTX)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
            pool[(scheme, host, port)] = conn
        return conn

    def _drop(self, key=None):
        pool = getattr(self._local, "pool", None)
        if not pool:
            return
        if key is None:
            pool.clear()
            return
        conn = pool.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def request(self, url, body=None, headers=None, method=None):
        """One request with one retry on a stale pooled connection.

        ``url`` may be absolute (Presto hands out absolute nextUris, possibly
        on another host) or base-relative. Returns (status, headers-dict,
        body-bytes); HTTP error STATUSES are returned to the caller (Presto's
        401-refresh flow needs them) rather than raised.
        """
        scheme, host, port, path = self._resolve(url)
        key = (scheme, host, port)
        hdrs = {"Accept": "application/json"}
        if self.bearer:
            hdrs["Authorization"] = "Bearer " + self.bearer
        if headers:
            hdrs.update(headers)
        payload = body.encode() if isinstance(body, str) else body
        last = None
        for attempt in (0, 1):
            conn = self._conn(scheme, host, port)
            try:
                conn.request(method or ("POST" if payload is not None else "GET"),
                             path, body=payload, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
            except (http.client.HTTPException, OSError, TimeoutError) as exc:
                last = exc
                self._drop(key)
                if attempt:  # second failure: it is a real error
                    raise
        raise last  # unreachable; keeps the flow explicit


def _parse_mcp_body(body):
    """Parse an MCP HTTP response: plain JSON or SSE (event: message / data:)."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():  # SSE: take the last data: line with JSON
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "{}":
                try:
                    return json.loads(payload)
                except ValueError:
                    continue
    raise ValueError("unparseable MCP response: %r" % body[:200])


def _count_markdown_rows(text):
    """Count data rows in a markdown table (header + separator excluded)."""
    lines = [l for l in text.splitlines() if l.strip().startswith("|")]
    return max(0, len(lines) - 2)


class _McpBase:
    """Shared MCP streamable-http plumbing (session init + tools/call)."""

    def __init__(self, url, bearer="", timeout=300):
        url = url.rstrip("/")
        self.http = _Http(url + ("/mcp" if not url.endswith("/mcp") else ""),
                          timeout=timeout, bearer=bearer)
        self.session_id = None
        self._init_lock = threading.Lock()
        self._init()

    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        body = json.dumps(payload).encode()
        # The ezaf-gateway route table for these hosts ALTERNATES every few
        # minutes between the MCP-aware config and the portal default (which
        # 405s POSTs). Kubernetes VS objects are stable — the flap is in the
        # gateway config push. Strategy: on a bare 405, drop the pooled
        # connection and wait out the window (a bare 405 is never a legitimate
        # MCP response; the app answers JSON-RPC errors instead).
        last_err = None
        for attempt in range(20):
            status, resp_headers, resp_body = self.http.request(
                "", body=body, headers=headers)
            if status == 405:
                self.http._drop()
                last_err = RuntimeError("mcp HTTP 405 (gateway flap window)")
                time.sleep(30)
                continue
            if status >= 400:
                raise RuntimeError("mcp HTTP %s: %s" % (status, resp_body[:200]))
            sid = resp_headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            text = resp_body.decode()
            return _parse_mcp_body(text) if text.strip() else {}
        raise last_err

    def _init(self):
        with self._init_lock:
            self._post({
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "sqlhandler-bench", "version": "1.0"},
                },
            })
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})


class McpSqlhandler(_McpBase):
    """SQLhandler via MCP streamable-http tools/call -> run_sql."""

    name = "sqlhandler-mcp"

    def query(self, sql):
        """Run one query; returns (wall_seconds, rows_returned)."""
        t0 = time.monotonic()
        resp = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_sql", "arguments": {"sql": sql}},
        })
        if resp.get("error"):
            raise RuntimeError("mcp error: %s" % json.dumps(resp["error"])[:200])
        content = (resp.get("result") or {}).get("content") or []
        text = content[0].get("text", "") if content else ""
        if (resp.get("result") or {}).get("isError"):
            raise RuntimeError("sql error: %s" % text[:200])
        rows = _count_markdown_rows(text)
        return time.monotonic() - t0, rows


class McpEzpresto(_McpBase):
    """EzPresto via its own MCP streamable-http server (MCP-to-MCP leg).

    The query tool and its SQL argument name are discovered from
    ``tools/list`` at init (any property named sql/query/statement/q wins;
    otherwise the first tool's first property is used and a warning logs
    the guess). Auth: same Keycloak bearer as the Presto API leg.
    """

    name = "ezpresto-mcp"

    def __init__(self, url, bearer="", timeout=300):
        super().__init__(url, bearer=bearer, timeout=timeout)
        self.tool, self.arg = None, "query"
        self._discover_tool()

    def _discover_tool(self):
        try:
            resp = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = (resp.get("result") or {}).get("tools") or []
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("ezpresto-mcp tools/list failed: %s\n" % str(exc)[:150])
            return
        for t in tools:
            props = (t.get("inputSchema") or {}).get("properties") or {}
            for key in ("sql", "query", "statement", "q"):
                if key in props:
                    self.tool, self.arg = t["name"], key
                    print("ezpresto-mcp: tool %r arg %r" % (self.tool, self.arg), file=sys.stderr)
                    return
        if tools:
            self.tool = tools[0]["name"]
            props = (tools[0].get("inputSchema") or {}).get("properties") or {}
            self.arg = list(props)[0] if props else "query"
            print("ezpresto-mcp: guessing tool %r arg %r (nothing sql-ish in tools/list)"
                  % (self.tool, self.arg), file=sys.stderr)

    def query(self, sql):
        t0 = time.monotonic()
        resp = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": self.tool, "arguments": {self.arg: sql}},
        })
        if resp.get("error"):
            raise RuntimeError("mcp error: %s" % json.dumps(resp["error"])[:200])
        content = (resp.get("result") or {}).get("content") or []
        text = content[0].get("text", "") if content else ""
        if (resp.get("result") or {}).get("isError"):
            raise RuntimeError("sql error: %s" % text[:200])
        if text.strip().startswith('{"status"') and '"dev"' in text:
            # the flapping server's health echo on the MCP path — never a
            # query result; raising keeps it out of the numbers as fake data
            raise RuntimeError("ezpresto-mcp in health-echo state (MCP app down)")
        # rows: try JSON payload first, fall back to markdown counting
        rows = 0
        try:
            data = json.loads(text)
            rows = len(data.get("rows") or data if isinstance(data, list) else data.get("rows") or [])
        except (ValueError, TypeError):
            rows = _count_markdown_rows(text)
        return time.monotonic() - t0, rows


class RestSqlhandler:
    """SQLhandler via the REST JSON API on /api/query."""

    name = "sqlhandler-rest"

    def __init__(self, url, bearer="", timeout=300):
        url = url.rstrip("/")
        if url.endswith("/mcp"):
            url = url[:-4]
        self.http = _Http(url, timeout=timeout, bearer=bearer)

    def query(self, sql):
        body = json.dumps({"sql": sql})
        t0 = time.monotonic()
        status, _hdrs, data = self.http.request(
            "/api/query", body=body, headers={"Content-Type": "application/json"})
        if status >= 400:
            raise RuntimeError("HTTP %s %s" % (status, data[:200]))
        payload = json.loads(data.decode())
        if payload.get("error"):
            raise RuntimeError("sql error: %s" % str(payload["error"])[:200])
        return time.monotonic() - t0, len(payload.get("rows") or [])


class PrestoEngine:
    """EzPresto via the native Presto HTTP statement API.

    Auth: either a static access token (`bearer`), or a Keycloak refresh
    token (`refresh_token`) which is exchanged for fresh access tokens via
    the token endpoint whenever Presto replies 401. Keycloak rotates refresh
    tokens on every exchange, so the newest one is always kept.
    """

    name = "ezpresto"

    def __init__(self, url, bearer="", timeout=300, user="bench",
                 refresh_token="", token_url="", client_id="ua",
                 client_secret=""):
        self.url = url.rstrip("/")
        self.http = _Http(url, timeout=timeout)  # bearer rotates on refresh,
        self.bearer = bearer                     # so it rides per-request below
        self.timeout = timeout
        self.user = user
        self.refresh_token = refresh_token
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token_lock = threading.Lock()
        self._refresh_failures = 0

    def _headers(self):
        headers = {"X-Presto-User": self.user, "X-Presto-Source": "bench"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        return headers

    def _refresh_access_token(self):
        """Exchange the refresh token for a new access token (thread-safe)."""
        if not (self.refresh_token and self.token_url):
            return False
        with self._token_lock:
            if self._refresh_failures >= 3:
                return False
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            }).encode()
            req = urllib.request.Request(
                self.token_url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                    payload = json.loads(r.read().decode())
            except Exception as exc:  # noqa: BLE001
                self._refresh_failures += 1
                sys.stderr.write("token refresh failed (%d): %s\n"
                                 % (self._refresh_failures, str(exc)[:150]))
                return False
            self.bearer = payload.get("access_token") or self.bearer
            self.refresh_token = payload.get("refresh_token") or self.refresh_token
            return True

    def _fetch(self, url, data=None):
        status, _hdrs, body = self.http.request(url, body=data, headers=self._headers())
        if status == 401 and self._refresh_access_token():
            status, _hdrs, body = self.http.request(url, body=data, headers=self._headers())
        if status >= 400:
            raise RuntimeError("presto HTTP %s: %s" % (status, body[:200]))
        return json.loads(body.decode())

    def _run(self, sql):
        """POST /v1/statement then poll nextUri; returns (wall_seconds, rows)."""
        t0 = time.monotonic()
        resp = self._fetch(self.url + "/v1/statement", data=sql.encode())
        rows = []
        while True:
            if resp.get("error"):
                raise RuntimeError("presto error: %s" % json.dumps(resp["error"])[:300])
            for row in resp.get("data") or []:
                rows.append(row)
            nxt = resp.get("nextUri")
            if not nxt:
                break
            resp = self._fetch(nxt)
        return time.monotonic() - t0, rows

    def query(self, sql):
        """Run one query; returns (wall_seconds, rows_returned)."""
        wall, rows = self._run(sql)
        return wall, len(rows)

    def query_rows(self, sql):
        """Run one query; returns (wall_seconds, actual row lists)."""
        return self._run(sql)


# --------------------------------------------------------------------------
# Workload
# --------------------------------------------------------------------------

def load_workload(path):
    with open(path) as fh:
        return json.load(fh)


def engine_for(entry, engine_name):
    q = entry["queries"]
    if engine_name in ("ezpresto", "ezpresto-mcp"):
        # both ezpresto legs speak the Presto dialect (minio.default.orders)
        return q.get("ezpresto") or q["sqlhandler"]
    return q.get(engine_name) or q["sqlhandler"]


def bench_sql(sql, rep, cache_bust):
    """Optionally vary the SQL text per rep with a trailing comment.

    DuckDB/Presto ignore comments, but sqlhandler's result cache keys on the
    raw text — so a unique comment per rep makes that rep a cache MISS.
    Used by --cache-bust to measure cold engine performance; without it the
    latency suite's repeats measure the (Snowflake-style) result cache, which
    is the honest agent-facing experience but not engine speed.
    """
    return sql + (" /* bench-rep-%d */" % rep) if cache_bust else sql


# --------------------------------------------------------------------------
# Suites
# --------------------------------------------------------------------------

def pct(sorted_ms, q):
    if not sorted_ms:
        return 0
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * q))]


def run_one(engine, sql, timeout):
    """Returns (wall_ms, rows, None) or (wall_ms, 0, error_string)."""
    try:
        wall, rows = engine.query(sql)
        return wall * 1000.0, rows, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            detail = ""
        return exc.ms if hasattr(exc, "ms") else 0.0, 0, "HTTP %s %s" % (exc.code, detail)
    except Exception as exc:  # noqa: BLE001 - report anything as a failed call
        return 0.0, 0, str(exc)[:200]


def suite_latency(engines, workload, reps, timeout, cache_bust=False):
    """Query mix, sequential, N reps per engine."""
    print("\n## Latency (sequential, %d reps/query, %s)\n"
          % (reps, "cache-busted" if cache_bust else "warm cache"))
    print("| query | rows scanned | engine | p50 | p95 | min | max | ok |")
    print("|---|---:|---|---:|---:|---:|---:|---:|")
    results = []
    for entry in workload:
        for engine in engines:
            samples, errors, rows_out = [], 0, 0
            for rep in range(reps):
                ms, rows, err = run_one(
                    engine, bench_sql(engine_for(entry, engine.name), rep, cache_bust), timeout)
                if err:
                    errors += 1
                else:
                    samples.append(ms)
                    rows_out = rows
            if not samples:
                print("| %s | %d | %s | ERR | | | | 0/%d |" %
                      (entry["name"], entry.get("rows_scanned", 0), engine.name, reps))
                results.append({"query": entry["name"], "engine": engine.name, "error": errors})
                continue
            s = sorted(samples)
            results.append({
                "query": entry["name"], "engine": engine.name,
                "rows_scanned": entry.get("rows_scanned", 0),
                "p50_ms": round(pct(s, 0.5), 1), "p95_ms": round(pct(s, 0.95), 1),
                "min_ms": round(s[0], 1), "max_ms": round(s[-1], 1),
                "ok": "%d/%d" % (len(samples), reps),
            })
            print("| %s | %s | %s | %.1f ms | %.1f ms | %.1f ms | %.1f ms | %s |" % (
                entry["name"], entry.get("rows_scanned", 0), engine.name,
                pct(s, 0.5), pct(s, 0.95), s[0], s[-1], "%d/%d" % (len(samples), reps)))
    return results


def suite_concurrency(engines, workload, levels, reps, timeout, cache_bust=False):
    """Fixed query pool driven at increasing concurrency levels."""
    print("\n## Concurrency (pool of %d queries, %d batch(es)/level)\n" % (len(workload), reps))
    print("| engine | level | calls | ok | wall/batch | qps | p50 | p95 | max |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    results = []
    for engine in engines:
        base_jobs = [engine_for(e, engine.name) for e in workload]
        for level in levels:
            all_ms, ok, walls = [], 0, []
            for rep in range(reps):
                batch = [(bench_sql(base_jobs[i % len(base_jobs)], rep * level + i, cache_bust),
                          e.get("rows_scanned", 0))
                         for i, e in enumerate([workload[i % len(workload)] for i in range(level)])]
                t0 = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=level) as ex:
                    futs = [ex.submit(run_one, engine, sql, timeout) for sql, _ in batch]
                    outcomes = [f.result() for f in futs]
                walls.append(time.monotonic() - t0)
                for ms, _rows, err in outcomes:
                    if not err:
                        all_ms.append(ms)
                        ok += 1
            s = sorted(all_ms)
            total_calls = level * reps
            wall = sum(walls) / len(walls) if walls else 0
            results.append({
                "engine": engine.name, "level": level, "calls": total_calls, "ok": ok,
                "wall_ms": round(wall * 1000), "qps": round(total_calls / wall, 2) if wall else 0,
                "p50_ms": round(pct(s, 0.5), 1), "p95_ms": round(pct(s, 0.95), 1),
                "max_ms": round(s[-1], 1) if s else 0,
            })
            print("| %s | %d | %d | %d | %.0f ms | %.2f | %.1f ms | %.1f ms | %.1f ms |" % (
                engine.name, level, total_calls, ok, wall * 1000,
                total_calls / wall if wall else 0, pct(s, 0.5), pct(s, 0.95),
                s[-1] if s else 0))
    return results


def suite_throughput(engines, workload, reps, timeout, cache_bust=False):
    """Full-scan aggregates: rows scanned per second."""
    print("\n## Throughput (full-scan aggregates, %d reps)\n" % reps)
    print("| query | rows/rep | engine | wall (mean) | rows/s | ok |")
    print("|---|---:|---|---:|---:|---:|")
    results = []
    for entry in workload:
        if not entry.get("rows_scanned"):
            continue
        for engine in engines:
            walls, errors = [], 0
            for rep in range(reps):
                ms, _rows, err = run_one(
                    engine, bench_sql(engine_for(entry, engine.name), rep, cache_bust), timeout)
                if err:
                    errors += 1
                else:
                    walls.append(ms / 1000.0)
            mean = statistics.mean(walls) if walls else 0
            rps = entry["rows_scanned"] / mean if mean else 0
            results.append({
                "query": entry["name"], "engine": engine.name,
                "rows_scanned": entry["rows_scanned"],
                "wall_ms": round(mean * 1000, 1), "rows_per_s": round(rps),
                "ok": "%d/%d" % (len(walls), reps),
            })
            print("| %s | %s | %s | %.1f ms | %s | %s |" % (
                entry["name"], entry["rows_scanned"], engine.name, mean * 1000,
                "{:,}".format(int(rps)) if rps else "-", "%d/%d" % (len(walls), reps)))
    return results


# --------------------------------------------------------------------------
# Probe: discover what each engine can see (catalogs / tables)
# --------------------------------------------------------------------------

PROBE_SQLS = ["SHOW CATALOGS"]


def _make_presto(args):
    user = args.presto_user
    if args.bearer:
        claims = _jwt_claims(args.bearer)
        user = claims.get("preferred_username") or user
    return PrestoEngine(
        args.presto_url, args.bearer, args.timeout, user,
        refresh_token=args.refresh_token, token_url=args.keycloak_token_url,
        client_id=args.keycloak_client_id, client_secret=args.keycloak_client_secret)


def cmd_probe(args):
    print("== SQLhandler (%s) ==" % args.sqlhandler_url)
    try:
        engine = _make_sqlhandler(args)
        wall, rows = engine.query("SHOW TABLES")
        print("SHOW TABLES ok in %.0f ms:" % (wall * 1000))
        print(json.dumps(rows, indent=1)[:2000])
    except Exception as exc:  # noqa: BLE001
        print("sqlhandler probe FAILED: %s" % str(exc)[:300])

    print("\n== EzPresto (%s) ==" % args.presto_url)
    try:
        engine = _make_presto(args)
        wall, rows = engine.query_rows("SHOW CATALOGS")
        print("SHOW CATALOGS ok in %.0f ms:" % (wall * 1000))
        print(json.dumps(rows, indent=1))
        catalogs = [r[0] for r in rows]
        for cat in catalogs:
            if cat.lower() in ("system", "jmx", "network", "cache"):
                continue
            try:
                _, schemas = engine.query_rows("SHOW SCHEMAS FROM %s" % cat)
                print("catalog %s schemas: %s" % (cat, [s[0] for s in schemas]))
                for sch in [s[0] for s in schemas]:
                    if sch in ("information_schema",):
                        continue
                    _, tables = engine.query_rows("SHOW TABLES FROM %s.%s" % (cat, sch))
                    if tables:
                        print("  %s.%s tables: %s" % (cat, sch, [t[0] for t in tables]))
            except Exception as exc:  # noqa: BLE001
                print("  catalog %s: %s" % (cat, str(exc)[:150]))
    except Exception as exc:  # noqa: BLE001
        print("ezpresto probe FAILED: %s" % str(exc)[:300])

    if args.ezpresto_mcp_url:
        print("\n== EzPresto MCP (%s) ==" % args.ezpresto_mcp_url)
        try:
            engine = McpEzpresto(args.ezpresto_mcp_url, args.bearer, args.timeout)
            print("tools: %r arg %r" % (engine.tool, engine.arg))
            wall, rows = engine.query("SHOW CATALOGS")
            print("SHOW CATALOGS ok in %.0f ms:" % (wall * 1000))
            print(json.dumps(rows, indent=1)[:1500])
        except Exception as exc:  # noqa: BLE001
            print("ezpresto-mcp probe FAILED: %s" % str(exc)[:300])
    return 0


def _jwt_claims(token):
    """Decode a JWT payload without verifying (typ/exp inspection only)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(__import__("base64").urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return {}


def cmd_token(args):
    """Classify a pasted token and/or mint a fresh access token."""
    tok = args.token or args.bearer or args.refresh_token
    if tok:
        claims = _jwt_claims(tok)
        typ = claims.get("typ", "?")
        exp = claims.get("exp")
        remains = ""
        if exp:
            remains = ", expires in %d min" % max(0, int(exp - time.time()) // 60)
        print("token type: %s%s  (iss: %s)" % (typ, remains, claims.get("iss", "?")))
        if typ == "Refresh":
            if not args.refresh_token:
                args.refresh_token = tok
        elif typ not in ("Bearer", "Offline"):
            print("NOTE: unknown typ — treating as refresh token if the mint below fails")

    if not args.refresh_token:
        print("no refresh token available; nothing to mint")
        return 1
    engine = _make_presto(args)
    engine.bearer = ""
    ok = engine._refresh_access_token()
    if not ok:
        print("refresh FAILED — token may be revoked/expired; re-grab from the UI session")
        return 2
    claims = _jwt_claims(engine.bearer)
    exp = claims.get("exp")
    print("minted access token for user %r%s" % (
        claims.get("preferred_username"), 
        (", expires in %d min" % max(0, int(exp - time.time()) // 60)) if exp else ""))
    print("\nEZPRESTO_BEARER=%s" % engine.bearer)
    print("\n(new refresh token for future runs)\nEZPRESTO_REFRESH_TOKEN=%s" % engine.refresh_token)
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def _make_sqlhandler(args):
    if args.sqlhandler_mode == "rest":
        return RestSqlhandler(args.sqlhandler_url, args.sqlhandler_bearer, args.timeout)
    return McpSqlhandler(args.sqlhandler_url, args.sqlhandler_bearer, args.timeout)


def cmd_run(args):
    workload = load_workload(args.workload)["queries"]
    legs = {part.strip() for part in args.only.split(",") if part.strip()}
    if "all" in legs:
        legs.update(("sqlhandler", "presto", "ezpresto-mcp"))
    engines = []
    if "sqlhandler" in legs:
        engines.append(_make_sqlhandler(args))
    if "presto" in legs:
        engines.append(_make_presto(args))
    if "ezpresto-mcp" in legs and args.ezpresto_mcp_url:
        try:
            engines.append(McpEzpresto(args.ezpresto_mcp_url, args.bearer, args.timeout))
        except Exception as exc:  # noqa: BLE001 - a down leg must not sink the run
            print("ezpresto-mcp skipped: %s" % str(exc)[:200], file=sys.stderr)
    if not engines:
        print("no engines selected (--only=%r)" % args.only, file=sys.stderr)
        return 2

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    cb = getattr(args, "cache_bust", False)
    print("# SQLhandler vs EzPresto — %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("\nengines: %s" % ", ".join(e.name for e in engines))
    print("sqlhandler: %s   presto: %s   ezpresto-mcp: %s"
          % (args.sqlhandler_url, args.presto_url, args.ezpresto_mcp_url))
    if cb:
        print("cache-bust: ON — every rep gets a unique SQL comment "
              "(sqlhandler result-cache cold; DuckDB/scan caches stay warm)")

    # Warm both engines once per query so the dataset/metadata caches are
    # comparable. With --cache-bust the warm pass uses its OWN tag so the
    # result cache stays cold for the measured reps.
    print("\nwarming: 1 run/query/engine ...", file=sys.stderr)
    for entry in workload:
        for engine in engines:
            run_one(engine, bench_sql(engine_for(entry, engine.name), -1, cb), args.timeout)

    out = {"workload": os.path.basename(args.workload), "suites": {},
           "cache_bust": cb}
    if args.suite in ("latency", "all"):
        out["suites"]["latency"] = suite_latency(engines, workload, args.reps, args.timeout, cb)
    if args.suite in ("concurrency", "all"):
        out["suites"]["concurrency"] = suite_concurrency(
            engines, workload, levels, args.reps, args.timeout, cb)
    if args.suite in ("throughput", "all"):
        out["suites"]["throughput"] = suite_throughput(
            engines, workload, args.reps, args.timeout, cb)

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=1)
        print("\nJSON results -> %s" % args.json_out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--sqlhandler-url",
                        default=os.environ.get(
                            "SQLHANDLER_URL",
                            "https://sqlhandler.pcai-se-ai-application.hst.rdlabs.hpecorp.net"))
    common.add_argument("--sqlhandler-mode", default="rest", choices=["mcp", "rest"])
    common.add_argument("--sqlhandler-bearer", default=os.environ.get("SQLHANDLER_BEARER", ""))
    common.add_argument("--presto-url",
                        default=os.environ.get(
                            "PRESTO_URL",
                            "https://ezpresto.pcai-se-ai-application.hst.rdlabs.hpecorp.net"))
    common.add_argument("--bearer", default=os.environ.get("EZPRESTO_BEARER", ""),
                        help="Bearer JWT access token for ezpresto (Keycloak UA realm)")
    common.add_argument("--refresh-token", default=os.environ.get("EZPRESTO_REFRESH_TOKEN", ""),
                        help="Keycloak refresh token; auto-mints access tokens on 401")
    common.add_argument("--keycloak-token-url",
                        default=os.environ.get(
                            "KEYCLOAK_TOKEN_URL",
                            "https://keycloak.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
                            "/realms/UA/protocol/openid-connect/token"))
    common.add_argument("--keycloak-client-id", default=os.environ.get("KEYCLOAK_CLIENT_ID", "ua"))
    common.add_argument("--keycloak-client-secret",
                        default=os.environ.get("KEYCLOAK_CLIENT_SECRET", ""))
    common.add_argument("--presto-user", default=os.environ.get("PRESTO_USER", "bench"))
    common.add_argument("--ezpresto-mcp-url",
                        default=os.environ.get(
                            "EZPRESTO_MCP_URL",
                            "https://mcp-ezpresto-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp"),
                        help="EzPresto's MCP streamable-http endpoint (included in `all` runs; "
                             "unset EZPRESTO_MCP_URL / pass empty to skip)")
    common.add_argument("--cache-bust", action="store_true",
                        help="append a unique SQL comment per rep so sqlhandler's result "
                             "cache never hits (cold engine numbers; Presto unaffected)")
    common.add_argument("--timeout", type=int, default=300)

    p_probe = sub.add_parser("probe", parents=[common],
                             help="list catalogs/schemas/tables on both engines")
    p_probe.set_defaults(func=cmd_probe)

    p_tok = sub.add_parser("token", parents=[common],
                           help="classify a pasted token / mint a fresh access token")
    p_tok.add_argument("token", nargs="?", default="",
                       help="access or refresh token (either works)")
    p_tok.set_defaults(func=cmd_token)

    p_run = sub.add_parser("run", parents=[common], help="run the benchmark")
    p_run.add_argument("--suite", default="all",
                       choices=["latency", "concurrency", "throughput", "all"])
    p_run.add_argument("--only", default="all",
                       help="legs to run, comma-separated: all | sqlhandler | presto | "
                            "ezpresto-mcp (e.g. --only sqlhandler,ezpresto-mcp)")
    p_run.add_argument("--workload", default=DEFAULT_WORKLOAD)
    p_run.add_argument("--reps", type=int, default=5)
    p_run.add_argument("--levels", default="1,4,8,16")
    p_run.add_argument("--json-out", default=None)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    # Bearer convenience: an empty --bearer falls back to bench/.bearer
    # (first non-empty, non-comment line; file mode 600) — the token never
    # has to cross the chat, the shell history, or an env dump.
    if not getattr(args, "bearer", ""):
        bearer_file = Path(__file__).resolve().parent / ".bearer"
        try:
            for line in bearer_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    args.bearer = line
                    break
        except OSError:
            pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
