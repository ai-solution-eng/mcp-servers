# SQLhandler

SQLhandler is a **natural-language-to-SQL MCP server + API** over configured
SQL/data sources: an **MCP 2.0 streamable-http server** (standard MCP protocol
at `/mcp`), a read-only **JSON API**, and a bundled **web UI** that all answer
questions over the same engine. Data sources — Microsoft Fabric OneLake (Delta
over ABFS), S3-compatible object storage (MinIO/AWS Parquet, Delta), Apache
Iceberg catalogs, mounted NFS/PVC directories — are read directly with pyarrow
and queried with **DuckDB**, pushing predicates and column projection into the
scan; Postgres/MySQL servers attach read-only and join with lake tables in one
query. Built for **HPE PCAI** (Private Cloud AI / Ezmeral Unified Analytics) as
a drop-in EzPresto/PrestoDB-class MCP data tool.

**Chart/image 1.6.1** (`ghcr.io/ai-solution-eng/sqlhandler`) · MCP at `/mcp` ·
health `/health` · readiness `/ready` · UI `/ui` · metrics `/metrics`

## What problem(s) it solves

- **Natural-language querying of governed data, no SQL expertise required** —
  an agent connects over standard MCP and answers questions in place; no data
  copy, no new SQL client for the human.
- **The LLM writes correct SQL on the first try** — the schema-aware
  **semantic catalog** merges human-written table/column docs, aliases, and
  virtual tables into `list_tables`/`describe_table`/MCP resources, and
  `profile_table` gives value-level statistics before a query is written;
  did-you-mean errors and query memory let agents self-correct in one
  round-trip.
- **Safe, read-only access** — every statement is parsed with DuckDB's own
  grammar and only plain `SELECT` (plus `EXPLAIN SELECT`) is accepted; DuckDB
  local-file access and runtime extension downloads are locked down; table
  names are traversal-guarded; the container ships hardened (non-root,
  read-only rootfs, no ServiceAccount token) and external callers authenticate
  at the PCAI gateway (oauth2-proxy).
- **Fast at the source** — reads only needed columns/filters from Parquet/Delta
  instead of round-tripping rows over JDBC; result/dataset/metadata caches with
  snapshot-aware invalidation, a `count(*)` metadata fast-path, and
  cgroup-proportional DuckDB memory budgets with spill-to-disk. Measured:
  cold 3M-row scans 1.0–2.0 s p50, identical queries ~100 ms warm.
- **Scales horizontally** — benchmarked: a single replica collapses under
  concurrency (5.5 → 2.7 qps from 1 → 16 clients, p95 17.8 s) while 4 replicas
  hold a flat ~6 qps at ~0.24 cores / 1.3 GiB fleet-wide. The chart ships
  HPA + PDB + topology spread. See [documentation/BENCHMARKS.md](documentation/BENCHMARKS.md).

## Features

**Data sources** (one engine; selected by the chart's `backend:` value)

- `onelake` (Fabric OneLake, Delta over ABFS) · `s3`/`minio` (Parquet +
  auto-detected Delta-on-S3 via `_delta_log`) · `iceberg` (REST/SQL catalog) ·
  `nfs` (mounted Delta/Parquet) — all with `version_as_of` time travel where
  the format supports it.
- **Federated multi-source** — `sources:` federates several buckets/sources,
  any mix of backends, behind one endpoint with cross-source `JOIN`s and
  source-qualified table names.
- **External databases** — `databases:` attaches Postgres/MySQL strictly
  read-only (`ATTACH ... READ_ONLY`); lake + OLTP joins in one query.

**MCP surface (agents)**

- Tools: `list_tables`, `search_tables`, `describe_table`, `profile_table`,
  `run_sql`, `scan_table` — markdown/JSON/CSV output, bind `params`, row caps.
- Resources: `sqlhandler://catalog`, `sqlhandler://table/{table}/schema`,
  `sqlhandler://query-memory` · prompts: `explore-data`, `analyze-table`.
- **Stateless** streamable-http at `/mcp` — any MCP 2.0 client (DSH, official
  Python/TS SDKs, MCP Inspector, OWUI); replicas need no session affinity.

**Semantic catalog** — attach routes: chart values (native YAML), an existing
ConfigMap, or upload from the web UI / `POST /api/semantic-catalog`
(JSON/YAML, per-table upsert, browser editors); hot-reloaded with no pod
restart; optional shared-PVC store (`semanticCatalog.store`) makes uploads
durable and cross-pod. Spec: [docs/semantic-catalog.md](docs/semantic-catalog.md).

**Web UI + JSON API** (humans and scripts, same engine + caches): searchable
table list, schema view, column stats, read-only SQL editor, charts, CSV/Parquet
export, saved queries/history at `/ui`; `/api/*` for status, tables,
describe, query, async query jobs (submit/poll/rows/cancel), preview, profile,
export, semantic-catalog upload/edit. An optional `SQLHANDLER_API_TOKEN`
bearer gate protects `/api/*` on deployments not behind the gateway.

**Ops** — Prometheus `/metrics` (query counters/duration histogram, cache
hit/miss, RSS gauges), JSONL audit log (`SQLHANDLER_AUDIT_LOG`), per-query
timeout and per-pod concurrency cap, backend-aware `/ready` probe, hardened
workload profile by default.

## Deploy on PCAI

> **PCAI is a Kubernetes wrapper — you never run `helm install` or
> `kubectl apply`.** Import the packaged chart (`.tar.gz`) into PCAI once,
> then drive the whole deployment from the chart's **Helm Values** editor (or
> the PCAI API): paste a full values document, adjust the `# SITE:` lines,
> apply. PCAI resolves `${DOMAIN_NAME}` before rendering. To change anything
> later, edit the same values document and re-apply.

Full guide (credentials, per-backend setup, semantic catalog, Istio/oauth2/
Kyverno, upgrading): [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) ·
paste-ready values: [helm/values-examples/](helm/values-examples/)

### Required values

The image tag is packaged with the chart (`image.tag: v1.6.1`) — set it only
to track a newer released image. A working deployment needs the data source
and the PCAI endpoint:

```yaml
# Data source — pick ONE backend; credential wiring is gated on the choice.
backend: s3                # s3 | onelake | iceberg | nfs  (onelake must be set explicitly)
s3:
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"
  bucket: "<bucket>"           # required
  prefix: ""                   # optional sub-tree
  credentialsSecret:
    create: false              # Secret created out-of-band — never in values history
    name: s3-credentials       # keys: access-key / secret-key

# PCAI exposure (required while ezua.enabled — the default):
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "sqlhandler.${DOMAIN_NAME}"   # MCP clients connect to https://<endpoint>/mcp
    istioGateway: "istio-system/ezaf-gateway"
```

### Optional values

```yaml
# Scale out — the benchmark-backed posture (see Scaling below):
autoscaling: { enabled: true, minReplicas: 4, maxReplicas: 8, targetCPUUtilizationPercentage: 80 }
replicaCount: 4               # advisory while the HPA is on
podDisruptionBudget: { enabled: true, minAvailable: 2 }

# Resources: requests are scheduler reservations; raise LIMITS (not requests)
# for wide tables / large joins — DuckDB's budget keys off the memory limit.
resources: { requests: { cpu: 250m, memory: 1Gi }, limits: { cpu: "4", memory: 16Gi } }

# Semantic catalog (docs merged into list/describe; agents see business meaning):
semanticCatalog:
  enabled: true
  tables:
    workorder/work_order:
      description: Maintenance work order headers, one row per order
      columns: { amount: Order total in USD }
  store: { enabled: true }    # 1Gi shared RWX PVC — durable, cross-pod uploads

# Gateway authn — oauth2-proxy AuthorizationPolicy (chart default: on; keep on
# wherever external callers reach the endpoint):
ezua:
  authorizationPolicy: { enabled: true, namespace: istio-system, providerName: oauth2-proxy }

# Production network posture (default off = in-cluster callers friction-free):
security:
  networkPolicy: { enabled: true, allowedNamespaces: ["<client-namespace>"] }
```

## Deployment targets

### SE G2

- **Cluster** `pcai-se-ai-application.hst.rdlabs.hpecorp.net`, deployment in a
  `project-user-*` namespace; **data source** is the in-cluster MinIO
  (`backend: s3`, bucket `test-parquet` benchmark dataset).
- **Values** [helm/values-examples/values.g2.yaml](helm/values-examples/values.g2.yaml)
  (sanitized site example) — carries the benchmark-derived scaling settings
  (4 replicas + HPA 4–8 @ 80% CPU, PDB, topology spread) and the shared
  catalog store.
- **Auth posture**: gateway AuthorizationPolicy off — in-cluster callers reach
  the ClusterIP service directly; external access rides the PCAI gateway.

### Hosted trial

- **Customer PCAI**: endpoint `sqlhandler.${DOMAIN_NAME}` (PCAI resolves the
  variable before rendering) with the gateway **oauth2-proxy
  AuthorizationPolicy on** — external callers authenticate with a PCAI token.
- **Values** [helm/values-examples/values.hosted-trial.yaml](helm/values-examples/values.hosted-trial.yaml)
  — full paste-ready document from the chart defaults with credential fillers.

Connecting an MCP client (either target):

```json
{
  "mcpServers": {
    "sqlhandler": {
      "url": "https://<endpoint>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

The header is needed only through the gateway; in-cluster callers need none
while the ingress NetworkPolicy is off (default). Humans use the same URL with
`/ui`.

## Scaling

Scale **out**, not up. The G2 benchmark (2026-09-08) showed a single replica's
throughput collapsing under concurrent clients (5.5 qps at 1 → 2.7 qps at 16,
p95 17.8 s) because the embedded DuckDB engine serializes requests per process
— while 4 replicas held a flat ~6 qps (p50 3.1–4.0 s) on ~0.24 cores / 1.3 GiB
fleet-wide peak; CPU/memory bumps on one replica buy nothing. The **HPA owns
the replica count** (4–8 on G2); its CPU@80% target is **runaway protection,
not load-following** — steady-state CPU sits near ~8% of limits, so it fires
only on heavy scan bursts. Full tables, the 800m-CPU variant, and the
cache-benchmark campaign: [documentation/BENCHMARKS.md](documentation/BENCHMARKS.md).

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Full PCAI deployment guide: backends + credentials, semantic catalog, ezua/Istio + oauth2 + Kyverno, upgrading, values reference |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | Post-deploy checks (MCP handshake, sample query, health/ready), optional operator kubectl checks, troubleshooting table |
| [documentation/FEATURES.md](documentation/FEATURES.md) | Feature deep-dive: MCP tools/resources, engine capabilities, web UI/API, observability, configuration knobs |
| [documentation/BENCHMARKS.md](documentation/BENCHMARKS.md) | Scale-out (HPA) + cache benchmarks with tables; reproducible from `bench/` |
| [docs/semantic-catalog.md](docs/semantic-catalog.md) | Semantic catalog spec for data owners — JSON/YAML format, table keys, virtual tables, troubleshooting |
| [helm/values-examples/](helm/values-examples/) | Paste-ready, secret-free values examples (SE G2 + hosted trial) and how to use them in PCAI |

## Development

```bash
uv pip install -e '.[dev]'
ruff check src
pytest                 # unit tests; bench/ holds the benchmark harness + raw results
```
