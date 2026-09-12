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
manage, requests are self-contained). The `Accept` header must include both
`application/json` and `text/event-stream`:

```bash
curl -s https://<endpoint>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-03-26","capabilities":{},
                 "clientInfo":{"name":"verify","version":"0"}}}'
```

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
| Gateway auth (oauth2-proxy enabled) | Unauthenticated external calls are redirected/challenged; with a valid PCAI token everything above works |
| In-cluster direct call | `curl http://<service>.<namespace>.svc.cluster.local:9097/ready` needs no auth while the ingress NetworkPolicy is off (default) |
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

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/ready` → 503 "not ready" + backend error | Wrong credentials/endpoint in the Secret; bucket/URL typo; NFS mount absent | Verify the Secret keys match `credentialsSecret.<x>Key` values; fix values + re-apply |
| Pod never becomes Ready, startup probe kills it | Cold discovery/prewarm outliving the probe window | Keep `startupProbe` enabled (24 × 5s); check logs for slow listing |
| MCP POST returns 405 through the gateway | Gateway tier flap window (transport-level, not the app) | Retry — clients drop the pooled connection and retry automatically |
| 401/302 to login on every route | oauth2-proxy AuthorizationPolicy on, caller unauthenticated | Use a valid PCAI token, or (in-cluster only) call the ClusterIP service directly |
| In-cluster caller suddenly gets 403 | Ingress NetworkPolicy was enabled without the caller's namespace | Add the namespace to `security.networkPolicy.allowedNamespaces` |
| `list_tables` empty but pods healthy | Wrong `backend:` for the data (e.g. chart default `s3` while data is on OneLake) | Set `backend: onelake` explicitly — credential wiring is gated on it |
| Federated table not found by bare name | Bare name ambiguous across sources | Use the source-qualified name (`sales_orders`) |
| Catalog edits never appear | Uploaded store overriding values catalog, or upload disabled | Check `GET /api/semantic-catalog` for the live source; `SQLHANDLER_CATALOG_UPLOAD=0` disables applying |
| Catalog PVC stuck Pending / render fails on access modes | RWO StorageClass for a scale-out deployment | `semanticCatalog.store.accessModes: [ReadWriteMany]` + an RWX-capable StorageClass |
| Release apply fails on a Kyverno hook | `kyverno.io` CRD missing on the cluster | `ezua.kyverno.enabled: false` (labels are still set by the chart helpers) |
| HPA shows `<unknown>/80%` | metrics-server absent/unreachable | Install metrics-server, or scale with `replicaCount` alone |
| Slow first queries after a restart | Cold caches (metadata + result caches are in-process) | Expected; `cache.prewarmTables` + disk-warm cache soften it, warm floor is ~ms |
| p95 latency collapses under many concurrent clients with one replica | Single replica serializes DuckDB queries per process | Scale out (HPA) — evidence and numbers in [BENCHMARKS.md](BENCHMARKS.md) |
