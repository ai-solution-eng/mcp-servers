# Verifying a SQLhandler deployment

Post-deploy checks, in order. All routes below are served by the single
sqlhandler container (port 9097 in-cluster; `https://<endpoint>` through the
PCAI gateway). Replace `<endpoint>` with the value of
`ezua.virtualService.endpoint` (e.g. `sqlhandler.pcai-se-ai-application.hst.rdlabs.hpecorp.net`).

## 1. Health & readiness

```bash
curl -s https://<endpoint>/health    # {"status":"ok"}          — liveness (process up)
curl -s https://<endpoint>/ready     # {"status":"ready"}       — backend-aware readiness
```

`/ready` performs a real connectivity check on the configured backend (OneLake
DFS token+list, S3 list, Iceberg catalog, NFS root). A `503` with
`{"status":"not ready","error":...}` means the data source itself is
unreachable — the pod is drained from the Service by the readiness probe while
it fails.

## 2. MCP handshake at `/mcp`

The endpoint speaks standard MCP (stateless streamable-http — no session id to
manage, requests are self-contained). Two headers are required: the `Accept`
header must include both `application/json` and `text/event-stream`, and
`/mcp` is API-key-gated (mandatory in the chart), so every request carries
`X-API-Key: <key>` (or `Authorization: Bearer <key>`):

```bash
curl -s https://<endpoint>/mcp \
  -H "X-API-Key: $SQLHANDLER_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-03-26","capabilities":{},
                 "clientInfo":{"name":"verify","version":"0"}}}'
```

A `401` here means the key is missing or not in the Secret's comma-separated
list — not that the server is down.

Expect a result with `serverInfo.name = "sqlhandler"` (plus protocolVersion and
capabilities). Follow with `tools/list` — expect `list_tables`,
`search_tables`, `describe_table`, `profile_table`, `run_sql`, `scan_table`.

## 3. Sample query

Fastest check — the JSON API (same engine as the MCP tools):

```bash
curl -s https://<endpoint>/api/query \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT 1 AS one"}'
# {"columns":["one"],"rows":[{"one":1}],...}
```

Then a real table, via the API or an MCP `tools/call`:

```bash
curl -s https://<endpoint>/api/tables | head -c 400        # table list
curl -s https://<endpoint>/api/status                      # version + backend
curl -s https://<endpoint>/api/query \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT count(*) FROM <table>"}'
```

## 4. Web UI, metrics, auth surfaces

| Check | Expected |
|---|---|
| `https://<endpoint>/ui` (browser) | Data explorer loads; table list renders (search, schema view, SQL editor) |
| `https://<endpoint>/metrics` | Prometheus text exposition: `sqlhandler_queries_total{outcome}`, query-duration histogram, cache hit/miss counters |
| `/mcp` API-key gate | No key → `401`; a key from the `sqlhandler-apikey` Secret → the handshake in §2 succeeds |
| Gateway auth (oauth2-proxy enabled) | Unauthenticated external calls are redirected/challenged; with a valid PCAI token everything above works |
| In-cluster direct call | `/ready` needs no auth (probe path). `/api/*` and `/mcp` **do**: since the 2026-11-12 audit the chart sets `SQLHANDLER_REQUIRE_IDENTITY=1`, so an anonymous `curl http://<service>.<namespace>.svc.cluster.local:9097/api/tables` returns 401 — add `-H "X-API-Key: <key>"` (or set `security.identity.requireIdentity: false` for a gateway-only posture) |
| Read-only guard | `POST /api/query` with `DELETE FROM ...` → rejected (only plain `SELECT` / `EXPLAIN SELECT` pass) |
| Semantic catalog | `curl -s https://<endpoint>/api/semantic-catalog` → live catalog source + table count (empty until §6 of DEPLOYMENT.md is done) |

## 5. Operator checks (optional — kubectl)

PCAI users normally never need these; for operators on the cluster:

```bash
kubectl -n <namespace> get pods -l app.kubernetes.io/instance=<release>   # Ready n/n
kubectl -n <namespace> get virtualservice,authorizationpolicy,hpa,pdb     # PCAI + scale-out objects
kubectl -n <namespace> logs deploy/<release>-sqlhandler --tail=100
kubectl -n <namespace> port-forward svc/<release>-sqlhandler 9097:9097    # then use http://localhost:9097/...
kubectl -n <namespace> get hpa <release>-sqlhandler -w                    # replica count under load
kubectl -n <namespace> top pods -l app.kubernetes.io/instance=<release>   # CPU/memory vs requests
```

## 6. Verification-time symptoms

Prerequisite and install failures — missing Secret, PVC `Pending`, `403` at the
gateway, Kyverno hook, `/ready` 503 — are catalogued once, with their causes, in
[DEPLOYMENT.md §6](DEPLOYMENT.md#6-troubleshooting). The table below covers
symptoms that only show up *while verifying a running deployment*.

| Symptom | Likely cause | Fix |
|---|---|---|
| `/mcp` returns `401` | Missing `X-API-Key` header, or the key is not in the Secret's comma-separated list | Add the header (§2); read the Secret back and compare |
| MCP POST returns 405 through the gateway | Gateway tier flap window (transport-level, not the app) | Retry — clients drop the pooled connection and retry automatically |
| Pod never becomes Ready, startup probe kills it | Cold discovery/prewarm outliving the probe window | Keep `startupProbe` enabled (24 × 5s); check logs for slow listing |
| `list_tables` empty but pods healthy | Wrong `backend:` for the data (e.g. chart default `s3` while the data is on OneLake) | Set `backend: onelake` explicitly — credential wiring is gated on it |
| Federated table not found by bare name | Bare name ambiguous across sources | Use the source-qualified name (`sales_orders`) |
| Catalog edits never appear | Uploaded store overriding the values catalog, or uploads disabled | Check `GET /api/semantic-catalog` for the live source; `SQLHANDLER_CATALOG_UPLOAD=0` disables applying |
| Slow first queries after a restart | Cold caches (metadata and result caches are in-process) | Expected; `cache.prewarmTables` + the disk-warm cache soften it |
| p95 collapses under many concurrent clients with one replica | One replica serializes DuckDB queries per process | Scale out — evidence and numbers in [BENCHMARKS.md](BENCHMARKS.md) |
