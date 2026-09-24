# Deploying SQLhandler on PCAI

Full deployment guide for **PCAI (HPE Private Cloud AI / HPE Ezmeral Unified
Analytics)**. Field reference for every key: [`../helm/values.yaml`](../helm/values.yaml) ·
paste-ready examples: [`../helm/values-examples/`](../helm/values-examples/) ·
post-deploy checks: [VERIFICATION.md](VERIFICATION.md).

SQLhandler ships as a **PCAI MCP 2.0** server — built on the `mcp>=2.0` SDK's
low-level `Server`, serving the standard MCP protocol (initialize handshake)
over stateless streamable-http at `/mcp`, so any standard MCP client can
connect (DSH, official Python/TS SDKs, MCP Inspector, OWUI).

## 1. How deployment works on PCAI

PCAI is a Kubernetes wrapper: **users never run `helm install` or
`kubectl apply`**. The whole deployment is driven by the chart's values:

1. **Import** the packaged `sqlhandler` chart (`.tar.gz`) into PCAI once.
2. Open the deployment's **Helm Values** editor (or use the PCAI API).
3. **Paste a full values document** (start from
   [`helm/values-examples/`](../helm/values-examples/)) and adjust the
   `# SITE:` lines.
4. **Apply.** PCAI resolves `${DOMAIN_NAME}` before rendering.

There is one chart; the "variant" is the **data-source backend** you select
(`backend:` + its credential block). Everything else — service, Istio, probes,
security profile — is shared.

To change anything later, **edit the same values document and re-apply** — that
is the upgrade (see §9).

## 2. Credentials first (out-of-band Secret)

The chart defaults to `credentialsSecret.create: false`: you create the
Kubernetes Secret yourself so credentials never pass through values files or
the Helm release secret (which is readable with `helm get values <release>
-a`). In PCAI, create the Secret in the deployment's namespace out-of-band, or
— for throwaway test clusters only — set `create: true` and let the chart
render it from values.

```bash
# S3 / MinIO (keys must match s3.credentialsSecret.accessKeyKey/secretKeyKey)
kubectl -n <namespace> create secret generic s3-credentials \
  --from-literal=access-key=<S3_ACCESS_KEY> \
  --from-literal=secret-key=<S3_SECRET_KEY>

# OneLake / Fabric service principal
kubectl -n <namespace> create secret generic fabric-credentials \
  --from-literal=tenant-id=<tenant-id> \
  --from-literal=client-id=<client-id> \
  --from-literal=client-secret=<client-secret>
```

Read back / rotate:

```bash
kubectl -n <namespace> get secret s3-credentials -o jsonpath='{.data.access-key}' | base64 -d; echo
```

The Deployment template carries `checksum/secret` + `checksum/config`
annotations, so a rotated Secret rolls the pod on the next re-apply. Full
convention: `helm/local/README.md` (local, not packaged).

## 3. Required values

These are the keys a working deployment cannot do without; everything else has
a safe chart default. The chart **fails the render** (not a warning) when a
required key is missing or empty:

| Key | Required when | Render failure message |
|---|---|---|
| `ezua.virtualService.endpoint` | `ezua.enabled: true` (default) | `Valid .Values.ezua.virtualService.endpoint is required when ezua is enabled !` |
| `ezua.virtualService.istioGateway` | `ezua.enabled: true` (default) | `Valid .Values.ezua.virtualService.istioGateway is required when ezua is enabled !` |
| `nfs.mount.pvcName` | `nfs.mount.enabled: true` | `nfs.mount.pvcName is required when nfs.mount.enabled` |

The chart defaults satisfy the `ezua.*` keys (used by the VirtualService and
the gateway AuthorizationPolicy); a full-values paste that accidentally
empties them fails loud at render time. `nfs.mount.pvcName` is read by the
Deployment's volume section, so the failure surfaces when `nfs.mount.enabled`
is set without a claim. The image tag is packaged with the chart
(`image.tag: v2.4.0` for the 2.4.0 chart) — set it only to track a newer
released image.

```yaml
# Data source — pick ONE backend (credential wiring is gated on the choice):
backend: s3            # s3 | minio | onelake | iceberg | nfs
s3:                    # when backend: s3
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"
  bucket: "<bucket>"           # required
  prefix: ""                   # optional sub-tree
  credentialsSecret:
    create: false              # Secret created out-of-band (§2)
    name: s3-credentials

# PCAI exposure — required while ezua.enabled (default true):
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "sqlhandler.${DOMAIN_NAME}"     # MCP clients: https://<endpoint>/mcp
    istioGateway: "istio-system/ezaf-gateway"
```

When using `backend: onelake` you **must set it explicitly** — the chart
default is `s3` and the per-backend credential wiring (and Secret creation) is
gated on the backend.

## 4. Optional values

```yaml
# Scale out (see documentation/BENCHMARKS.md — one replica collapses under
# concurrency; the HPA owns the replica count):
replicaCount: 4              # advisory while the HPA is on
autoscaling:
  enabled: true              # HPA 4-8 replicas, CPU@80% — burst/runaway protection
  minReplicas: 2             # HPA floor (autoscaling/v2 spec.minReplicas)
  maxReplicas: 8             # HPA ceiling (autoscaling/v2 spec.maxReplicas)
  targetCPUUtilizationPercentage: 80   # CPU target; burst protection only (see below)
  behavior:                  # HPA scaleUp/scaleDown stabilization windows
    scaleUpStabilizationSeconds: 30     # scale-up reacts quickly
    scaleDownStabilizationSeconds: 300  # scale-down is slow by design: a burst
                                        # of MCP tool calls must not thrash the fleet
podDisruptionBudget:
  enabled: true              # keep 2 pods through node drains
topologySpread:
  enabled: true              # spread replicas across nodes
  maxSkew: 1                             # max replica-count difference between domains
  topologyKey: kubernetes.io/hostname    # domain to spread across (per node)
  whenUnsatisfiable: ScheduleAnyway      # permissive — keeps single-node clusters schedulable

# Resources (chart default below; raise LIMITS — not requests — for wide
# tables / large joins; DuckDB's memory budget is duckdbMemoryFraction × the
# memory limit):
resources:
  requests: { cpu: 250m, memory: 1Gi }
  limits:   { cpu: "4",  memory: 16Gi }

# Gateway authn: oauth2-proxy AuthorizationPolicy (enabled by default; keep ON
# wherever external callers reach the endpoint through the PCAI gateway):
ezua:
  authorizationPolicy:
    enabled: true
    namespace: istio-system
    providerName: oauth2-proxy

# Semantic catalog (see §6) + metadata/query caches:
semanticCatalog:
  enabled: true
  tables:
    <schema/name>:
      description: ...
cache:
  ttl: 3600
  prewarmTables: "table_a,table_b"

# Security posture (default: hardened profile, ingress NetworkPolicy OFF so
# in-cluster callers work with zero friction):
security:
  networkPolicy:
    enabled: true                 # for production / real data
    allowedNamespaces: ["<client-namespace>"]
  # OPTIONAL key gate on /mcp — unset by default, the endpoint runs open
  # with a loud startup warning (gateway-fronted deployments). To switch it
  # on, pre-deploy the Secret and point the value at it; the chart never
  # creates or inlines the key:
  #   kubectl -n <ns> create secret generic mcp-fleet-apikeys \
  #     --from-literal='api-keys=<key1>,<key2>'
  apiKey:
    existingSecret: ""            # e.g. "mcp-fleet-apikeys" (key: api-keys)
```

Set `ezua.enabled: false` to drop the PCAI integration (VirtualService,
AuthorizationPolicy, Kyverno) and deploy as a plain MCP server.

### 4.1 Every values key by group

The full field reference (defaults + rationale comments) stays
[`../helm/values.yaml`](../helm/values.yaml); these tables map every key so
PCAI values documents can be checked against documentation. Behavioral keys
that map to engine features are additionally described in
[FEATURES.md](FEATURES.md) (§5 Performance for the cache layers).

**Scaling & scheduling**

| Key | Default | Effect |
|---|---|---|
| `autoscaling.minReplicas` / `autoscaling.maxReplicas` | 2 / 8 | HPA floor/ceiling (autoscaling/v2); the Deployment omits `spec.replicas` while the HPA is on |
| `autoscaling.targetCPUUtilizationPercentage` | 80 | CPU target. Steady-state CPU is ~8% of the limit even under full load (BENCHMARKS.md), so this fires only on heavy scan bursts — not on request volume. Load-following scale-out needs a custom metric via `autoscaling.extraMetrics` (prometheus-adapter) |
| `autoscaling.behavior.scaleUpStabilizationSeconds` | 30 | HPA scale-up stabilization window: how long a CPU burst must persist before scaling up |
| `autoscaling.behavior.scaleDownStabilizationSeconds` | 300 | HPA scale-down stabilization window: deliberately slow so a burst of MCP tool calls doesn't thrash the fleet |
| `topologySpread.maxSkew` | 1 | Max replica-count difference between topology domains when `topologySpread.enabled` |
| `topologySpread.topologyKey` | `kubernetes.io/hostname` | Topology domain to spread across (one domain per node) |
| `topologySpread.whenUnsatisfiable` | `ScheduleAnyway` | Permissive — scheduling proceeds even when the spread cannot be honored (single-node clusters) |

**Standard Kubernetes knobs** (pod-template / workload boilerplate; the chart
passes each through verbatim — cover the paths explicitly for PCAI values
audits):

| Key | Default | Effect |
|---|---|---|
| `nameOverride` / `fullnameOverride` | "" | Override the release's resource-name derivation (`<release>-sqlhandler` by default) — needed only for DNS-label or name-collision constraints |
| `image.pullPolicy` | `IfNotPresent` | Container `imagePullPolicy` (set `Always` for moving tags) |
| `resources.limits.cpu` / `resources.requests.cpu` | `"4"` / `250m` | CPU limit (DuckDB's thread count) / scheduler reservation — see the resources comment above |
| `service.targetPort` | `9097` | Container port the Service forwards to — kept separate from `service.port` so the listener can move without touching the VirtualService/NetworkPolicy port |
| `ingress.className` / `ingress.hosts` / `ingress.tls` | "" / `sqlhandler.local` + `/` Prefix / [] | Standard Ingress block (`ingress.enabled: true` only; PCAI normally uses the ezua VirtualService) |
| `podAnnotations` | {} | Extra pod-template annotations (e.g. metrics scrape config) |
| `nodeSelector` / `tolerations` / `affinity` | {} / [] / {} | Pod placement: node label constraints, taint tolerations, node/pod affinity |

### 4.2 MCP endpoint exposure

| Key | Default | Effect |
|---|---|---|
| `mcp.allowedOrigins` | "" (same-origin only) | Comma-separated extra browser origins allowed on `/api/*`, `/ui` and `/mcp` (CORS + Origin validation, fleet decision D3). Cross-origin browser reads are refused unless listed; MCP clients (which send no `Origin` header) are unaffected. Rendered as `SQLHANDLER_ALLOWED_ORIGINS` |
| `containerArgs` | `--transport streamable-http --host 0.0.0.0 --port "9097"` | Raw argv list passed to `python -m sqlhandler.server`. Changing `--port` requires matching `mcp.port`, `service.port`, and `service.targetPort` |
| `savedQueriesPath` | "" (next to the cache dir) | Absolute path of the saved-parameterized-queries JSON store (`query_save`/`query_list`/…). MUST sit on a mounted volume (read-only root filesystem leaves only mounts writable) — a reschedule otherwise forgets every saved query. Empty + `semanticCatalog.store.enabled: true` gives the durable default (`<store mountPath>/saved-queries.json`); an explicit path wins over that fallback |

### 4.3 Metrics scraping (PodMonitor)

| Key | Default | Effect |
|---|---|---|
| `metrics.podMonitor.enabled` | `true` | Render a prometheus-operator `PodMonitor` selecting the app's own pods at `/metrics` |
| `metrics.podMonitor.additionalLabels` | {} | Extra labels on the PodMonitor itself. If the operator's `podMonitorSelector` requires a label (commonly `release: prometheus`), set it here — otherwise the target silently never appears |
| `metrics.podMonitor.bearerTokenSecret` | {} | `{name, key}` of a Secret carrying `SQLHANDLER_API_TOKEN` or an `/mcp` API key, wired as the scrape's bearer token — required when `security.metricsAuth` is on (the ServiceAccount token is not accepted) |

### 4.4 Data-source keys

| Key | Default | Effect |
|---|---|---|
| `nfs.mount.mountPath` | `/data` | Container path the NFS PVC is mounted at (only `backend: nfs`); must contain `nfs.rootDir`'s content. Keep it aligned with `nfs.rootDir` |
| `iceberg.catalogName` | `sqlhandler` | SQL-catalog name for partitioning; rendered as `ICEBERG_CATALOG_NAME` (REST catalogs ignore it) |
| `iceberg.credentialsSecret.tokenKey` | `token` | Secret key holding the REST catalog token — create the Secret out-of-band with this key, or let `create: true` render it (bootstrap only) |
| `fabric.credentialsSecret.tenantIdKey` / `.clientIdKey` / `.clientSecretKey` | `tenant-id` / `client-id` / `client-secret` | Secret keys holding the Entra service principal; the deployment maps each to `FABRIC_TENANT_ID` / `FABRIC_CLIENT_ID` / `FABRIC_CLIENT_SECRET` |
| `fabric.storageOptions` | {} | Extra delta-rs storage options (JSON-merged into `SQLHANDLER_ONELAKE_STORAGE_OPTIONS`): object-client retries, timeouts, connection behavior for the OneLake backend |

### 4.5 Semantic catalog & saved queries

| Key | Default | Effect |
|---|---|---|
| `semanticCatalog.mountPath` | `/etc/sqlhandler` | Container dir the values-catalog ConfigMap is mounted at; the file lands at `<mountPath>/semantic-catalog.json`. Rarely changed |
| `semanticCatalog.store.existingClaim` | "" | Mount an existing PVC instead of creating `<release>-catalog`. Must offer RWX when replicas can exceed 1 (render-guarded) |
| `semanticCatalog.store.storageClass` | "" (cluster default) | StorageClass for the chart-created catalog claim; must offer the configured access mode |
| `semanticCatalog.store.mountPath` | `/var/lib/sqlhandler-catalog` | Where the store PVC is mounted; `SQLHANDLER_CATALOG_STORE` points at `<mountPath>/semantic-catalog.json`. Read-write (the upload API writes here) |

### 4.6 Cache layers

| Key | Default | Effect |
|---|---|---|
| `cache.ttl` | 3600 | Seconds list_tables/describe_table metadata is kept in memory (0 disables); maps to `SQLHANDLER_CACHE_TTL` |
| `cache.listAsyncRefresh` | `true` | Serve list_tables from cache immediately and refresh in the background (plus once per `cache.ttl`), so callers never block on slow object-store listings; `false` = fully synchronous |
| `cache.spillDir` | `/tmp/sqlhandler-duckdb-spill` | DuckDB spill directory when a query would push RSS past the pod limit (survives container restarts via the hardened `/tmp` emptyDir) |
| `cache.resultCacheTtl` | 3600 | Seconds an identical query's result is served from memory (keyed by sql/params/limits + base-snapshot versions; 0 disables) |
| `cache.resultCacheMaxBytes` | `"268435456"` (256MiB) | In-memory byte cap for cached query results (LRU-evicted) |
| `cache.virtualCacheTtl` | 3600 | Seconds a virtual table's materialized result is reused; cache key carries the definition + base-snapshot versions, so ETL commits invalidate instantly (0 disables materialization) |
| `cache.virtualCacheSort` | `true` | Cluster materialized virtual results by their lowest-cardinality columns so row-group statistics prune filtered reads (0 disables) |
| `cache.l2.dir` | "" | Shared directory for the cross-replica L2 result cache (zstd parquet + JSON sidecars). REQUIRED for L2 operation — even with `cache.l2.enabled: true` nothing is cached until this points at an RWX volume visible to every replica; the engine refuses to default it to pod-local /tmp (that would look like sharing while sharing nothing) |
| `cache.l2.ttl` | `"3600"` | Seconds a published result stays valid (lazy delete on lookup + daemon sweep). Keys already carry base-snapshot versions, so ETL invalidates instantly — TTL is only a backstop |
| `cache.l2.minBytes` | `"262144"` | Results SMALLER than this skip the L2 (a PVC round-trip costs more than recomputing a small result) |
| `cache.l2.maxBytes` | `"2147483648"` (2GiB) | Results LARGER than this skip the L2 (one runaway result must not fill the shared volume); `"0"` = unlimited |
| `cache.blockCache.dir` | "" (engine default `<tmp>/sqlhandler-block-cache`) | Cache location for the disk block cache (parquet footers/column chunks) |
| `cache.blockCache.blockSize` | `"8388608"` (8MiB) | Bytes per cached block — bigger means fewer remote round-trips |
| `cache.blockCache.maxBytes` | `"4294967296"` (4GiB) | Total cache size; the cache resets itself when exceeded |
| `cache.blockCache.includeLocal` | `false` | `true` also caches LocalFileSystem paths — NFS mounts are LocalFileSystem to pyarrow while being network; real local disk is already covered by the OS page cache |

### 4.7 Security policy files

| Key | Default | Effect |
|---|---|---|
| `security.apiKey.existingSecretKey` | `api-keys` | Key inside `security.apiKey.existingSecret` holding the comma-separated /mcp API-key list (`SQLHANDLER_API_KEYS`) — rotate by appending, then dropping, keys in that value |
| `security.policy.existingConfigMapKey` | `policy.json` | Key inside `security.policy.existingConfigMap` holding the policy document; `SQLHANDLER_POLICY_FILE` resolves to `<mountPath>/<existingConfigMapKey>` |
| `security.policy.mountPath` | `/etc/sqlhandler/policy` | Container mount point for the policy file. Wire the ConfigMap yourself as a volume+mount (the chart renders the path only) |
| `security.networkPolicy.gatewayNamespace` | `istio-system` | Namespace whose pods may call the Service (the Istio gateway) — always allowed by the ingress NetworkPolicy |
| `security.networkPolicy.additionalIngress` | [] | Raw extra NetworkPolicy `from:` peers (CIDRs, podSelectors…), appended verbatim to the ingress rules |

**Feature switches & landing zone**

| Key | Default | Effect |
|---|---|---|
| `rawFiles.enabled` / `rawFiles.maxFileMB` | `true` / `64` | Raw-text landing zone on the s3 + nfs backends: `.csv`/`.tsv`/`.json`/`.ndjson`/`.jsonl` (+ `.gz`) files discover as queryable tables under the same folder conventions as Parquet. `false` hides every raw table; the MB cap (0 = unlimited) skips whole tables containing any larger file (compressed size for `.gz` — approximate). Landing-zone by design: no row-group statistics, whole-file scans; promote large raw data to Parquet via the write tier's `COPY TO` |
| `duckdbFileAccess` | `false` | Opt back IN to DuckDB's own filesystem layer (`parquet_scan`/`COPY`/ducklake row reads). Default locked down: `read_csv('/etc/passwd')`, `COPY TO`, extension URL fetches all fail closed. Enable ONLY when an attached source genuinely needs it (ducklake row reads) |
| `mcp.compression.enabled` / `.minSize` | `true` / `1024` | gzip for MCP/JSON-API/UI responses (5-10x on markdown/JSON); bodies under `minSize` pass through; SSE/streaming excluded |
| `mcp.allowedHosts` / `mcp.allowedOrigins` | "" / "" | Strict `/mcp` Host-header allowlist (DNS-rebinding defense, 421 on mismatch) / extra browser origins for `/api/*`, `/ui` and `/mcp` (CORS + Origin validation). Empty = same-origin only; MCP clients unaffected |
| `savedQueriesPath` | "" | Absolute path of the saved-query JSON store — MUST sit on a mounted volume; empty + `semanticCatalog.store.enabled: true` gives the durable default (`<store mountPath>/saved-queries.json`) |
| `cache.prewarmRowgroups` | `1` | Data prewarm depth: the first N row groups of each `cache.prewarmTables` entry are read through the disk block cache at startup (needs `cache.blockCache.enabled`; local/NFS skipped) |

**Query behavior**

| Key | Default | Effect |
|---|---|---|
| `query.timeoutSeconds` | `"600"` | Per-query wall-clock timeout (D5): the query is interrupted inside DuckDB on expiry; `0` = no timeout. The gateway itself caps calls at 3600 s |
| `query.maxRows` | `"1000"` | Default row cap for `run_sql` / `scan_table` / `query_result` (`0` = uncapped — a runaway `SELECT *` fills the wire and the model's context); per-call `limit` overrides downward |
| `query.profileMaxRows` | `"1000000"` | Row-sample cap for `profile_table` / `column_stats` (`0` = full table; statistics over a bounded sample are what an agent needs — exact counts still come from Parquet/Delta metadata) |
| `query.maxJobs` | `8` | Max concurrent async query jobs (`query_submit`; HTTP 429 beyond it) |
| `query.queryMemorySize` | `50` | Recent query outcomes kept for the `sqlhandler://query-memory` resource (`0` disables recording) |
| `query.previewFastpath` | `true` | Bare `LIMIT n` previews read the first data file's first row group instead of enumerating every fragment of a large table |
| `query.maxConcurrentQueries` | `8` | Per-pod concurrency gate (engine.py): max simultaneous DuckDB queries; excess QUEUE up to `queueTimeoutSeconds` then fail with a clear error. `0` = unlimited (pre-gate behavior — not recommended). Chart wires `SQLHANDLER_MAX_CONCURRENT_QUERIES` |
| `query.queueTimeoutSeconds` | `30` | Seconds a query may wait for a concurrency slot. Chart wires `SQLHANDLER_QUEUE_TIMEOUT` |
| `query.auditLog` | `""` | JSONL query-audit path (one line per outcome — SIEM-friendly; empty = off, the historical default). The chart renders `SQLHANDLER_AUDIT_LOG` and mounts a pod-local writable dir for the path (render guard: must be absolute); durable audit = point it at your own PVC-backed mount |

**Identity & policy-as-code**

| Key | Default | Effect |
|---|---|---|
| `security.identity.trustBrowserHeaders` | `false` | TRUST GATE for the oauth2-proxy browser rung (`X-Auth-Request-User` / `X-Forwarded-Groups`). False = those headers are ignored. True ASSERTS the workload AuthorizationPolicy pins ingress to the gateway — only then can a browser header be trusted (headers are forgeable on any pod reachable without that pin) |
| `security.policy.enabled` | `false` | Policy-as-code: per-caller row filters + column masks + hidden tables from an operator-authored file (hot-reloaded). Enabling requires you to wire `security.policy.existingConfigMap` as a volume+mount yourself (the chart renders `SQLHANDLER_POLICY_FILE` = `<mountPath>/<existingConfigMapKey>` only — the ConfigMap mount is not chart-rendered) |

### 4.8 Write tier (scratch)

| Key | Default | Effect |
|---|---|---|
| `writes.scratch.leaseTtl` | `"300"` | Advisory single-writer lease TTL in seconds — a crashed writer's lease is broken after this long |
| `writes.scratch.icebergCatalogUri` | "" | pyiceberg scratch-catalog DB URI (sqlite) for `iceberg://`-named scratch roots. Unset + a warehouse set = the catalog DB is created next to the warehouse |
| `writes.scratch.icebergWarehouse` | "" | Warehouse root for `iceberg://`-named scratch roots (pyiceberg backend; `SQLHANDLER_WRITE_ICEBERG_WAREHOUSE`). Unset = the scratch path alone defines the table location |
| `writes.scratch.pvc.storageClass` | "" (cluster default) | StorageClass for the chart-created `<release>-scratch` claim — must offer RWX when replicas can exceed 1 (render-guarded) |
| `writes.scratch.pvc.mountPath` | `/scratch` | Container path the scratch volume is mounted at; `writes.scratch.roots` entries must resolve under it |
| `writes.scratch.pvc.existingClaim` | "" | Mount an existing claim instead of creating `<release>-scratch` — RWX discipline still applies |

### 4.9 ezua virtual-service timing

`ezua.virtualService.timeout` (default `60s`) and
`ezua.virtualService.longTimeout` (default `3600s`) set the Istio route
timeouts: `/mcp` and `/api/*` use the long one (agent loops, streaming, long
aggregations), everything else the short one. `ezua.virtualService.endpoint`
and `.istioGateway` are required (§3).

## 5. Data-source setup

All backends share the same SQL engine, caches, MCP tools, and a backend-aware
readiness probe; only the `DataProvider` behind them differs.

### 5.1 S3 / MinIO (`backend: s3` — chart default)

Every Parquet file or folder of Parquet files under `bucket` (+ optional
`prefix`) becomes a table; Hive-partition folders fold into the table. Folders
carrying a Delta `_delta_log` are auto-detected as Delta tables
(`s3.format: auto` → `S3_FORMAT`), so one bucket can mix formats with time
travel where a Delta log exists.

```yaml
backend: s3
s3:
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"   # or AWS/any S3
  bucket: "<bucket>"
  prefix: "<optional/subtree>"
  anonymous: false             # true reads a public bucket without a Secret
  credentialsSecret:
    name: s3-credentials
    create: false
```

### 5.2 OneLake / Fabric (`backend: onelake`)

Delta Lake over ABFS, authenticated with an Entra service principal. Table
discovery uses the OneLake DFS REST API; tables live under
`Tables/<schema>/<table>`.

```yaml
backend: onelake
fabric:
  credentialsSecret:
    name: fabric-credentials
    create: false              # preferred; create: true is bootstrap-only
    tenantIdKey: tenant-id     # Secret keys → FABRIC_TENANT_ID / FABRIC_CLIENT_ID /
    clientIdKey: client-id     # FABRIC_CLIENT_SECRET
    clientSecretKey: client-secret
  lakehouseAbfssUrl: "abfss://<workspace-guid>@onelake.dfs.fabric.microsoft.com/<lakehouse-guid>"
  # or workspaceId + lakehouseId instead of the full URL
```

The chart wires `FABRIC_TENANT_ID` / `FABRIC_CLIENT_ID` / `FABRIC_CLIENT_SECRET`
from the `fabric-credentials` Secret.

### 5.3 Iceberg (`backend: iceberg`)

Tables are discovered through an Iceberg REST (default) or SQL catalog; the
Parquet data files live in your object store.

```yaml
backend: iceberg
iceberg:
  catalogType: rest
  catalogUri: "http://rest-catalog:8181"
  catalogName: sqlhandler           # SQL catalogs partition by this name (REST ignores it)
  credentialsSecret:
    name: iceberg-credentials
    create: false              # REST token + S3 access/secret keys
    tokenKey: token            # Secret key the REST catalog token is read from
  storage:
    endpointUrl: "http://minio:9000"
```

### 5.4 NFS / mounted directory (`backend: nfs`)

Reads Delta **and** Parquet from a mounted directory. No credentials.

```yaml
backend: nfs
nfs:
  rootDir: /data
  mount:
    enabled: true
    pvcName: my-data-pvc       # REQUIRED when enabled (render fails without it)
    mountPath: /data           # container mount point; keep aligned with rootDir
```

The readiness probe verifies the mount is present.

### 5.5 Federated multi-source (`sources:`)

Several buckets/sources (any mix of backends) behind one endpoint —
cross-source `JOIN`s work in a single `run_sql`. When non-empty, `sources:`
overrides `backend:`.

```yaml
sources:
  - name: sales
    backend: s3
    endpointUrl: "http://minio.minio.svc.cluster.local:9000"
    bucket: sales-bucket
  - name: inventory
    backend: s3
    endpointUrl: "http://minio.minio.svc.cluster.local:9000"
    bucket: inventory-bucket
    prefix: raw
```

Tables are source-qualified (`sales_orders`, `inventory_raw_customers`); bare
names resolve only when unique. Source labels must be unique and
DuckDB-identifier-safe. Per-source credentials in `sources:` land in the
ConfigMap as JSON (`SQLHANDLER_SOURCES`) — for production, mount that env var
from a Secret instead of embedding keys in values.

### 5.6 External databases (read-only attach)

External database servers — Postgres, MySQL/MariaDB, SQLite files, SQL Server —
whose tables become queryable — and join-able with lake tables in **one** query
— as `<name>.<schema>.<table>`. Read-only is enforced by DuckDB itself
(`ATTACH ... READ_ONLY`).

```yaml
databases:
  - name: ops
    type: postgres        # postgres | mariadb | mysql | sqlite | sqlserver
                          # (omit `type` to default to postgres)
    host: postgresql.postgresql.svc.cluster.local
    port: 5432
    database: opsdb
    user: ro_user
    password: ""          # "" → manage the <release>-db-credentials Secret out-of-band
    params:               # driver options (TLS etc.) merged into the DSN
      sslmode: verify-full
      sslrootcert: /etc/sqlhandler/certs/ca.crt
```

Valid types are `postgres`, `mariadb`, `mysql`, `sqlite`, `sqlserver`
(`postgresql` is not accepted). The per-entry `params` object carries driver
options — notably TLS, which many production endpoints require — merged over
each type's built-in DSN keys: an existing key is overridden in place, a new
key is appended, no duplicate key is ever emitted. Keys that would carry
secrets or duplicate the entry's own fields (`password`, `user`, `host`,
`port`, `database`, `server`, …) are rejected loudly, and values are restricted
to an injection-safe charset. MySQL/MariaDB TLS rides the libmariadb keys
(`ssl_mode` — default `preferred` — plus `ssl_ca`, `ssl_cert`, `ssl_key`). Note
that the mysql/mariadb scanner takes DSN values bare (whitespace-split, no
quote handling): for those two types the entry fields (`host`/`database`/`user`)
and `params` values must not contain whitespace — a resolved password with
whitespace is rejected at attach time; postgres (libpq-quoted) and sqlserver
(semicolon-delimited) accept spaces.

The generated `SQLHANDLER_ATTACH` config carries env-var **names** only
(`SQLHANDLER_DB_PW_<NAME>`); password values come from the Secret. A database
that is down does not fail the lake.

`type: sqlite` attaches a **file**, not a server: `database` is the sqlite FILE
PATH and there is no host/port/user (`password_env` may be omitted; `params`
are not accepted). The file is read at query time through the extension's own
bundled sqlite3 library — outside DuckDB's filesystem lockdown — but the path
is operator-configured in `SQLHANDLER_ATTACH` (never model-chosen), the same
trust model as the nfs backend. `READ_ONLY` is still enforced.

`type: sqlserver` rides DuckDB's community **`mssql`** extension, baked into
the image (the old ODBC-based `sqlserver` extension does not exist on DuckDB
1.5.x). It speaks native TDS 7.4 — no unixODBC and no Microsoft ODBC driver
are needed. An explicit `user`/`user_env` is required (there is no `sa`
default). TLS is ON by default: `Encrypt=yes;TrustServerCertificate=yes` are
lenient defaults that `params` can override — in the mssql extension the two
keys are synonyms, so set only one of them; see the
[mssql extension docs](https://duckdb.org/community_extensions/extensions/mssql)
for TLS options.

> **DuckDB file access is locked down** on every query connection: local-file
> reads (`read_csv`, `COPY ... TO`) and URL fetches fail closed; all
> object-store IO is done by pyarrow outside DuckDB. (`SQLHANDLER_DUCKDB_FILE_ACCESS=1`
> re-enables it when a query genuinely needs it.)

### 5.7 ADLS Gen2 (`backend: adls`) and GCS (`backend: gcs`)

Both read Parquet **and** Delta (plus the raw landing zone) with the same
folder conventions as s3, using pyarrow's native filesystems:

```yaml
backend: adls
adls:
  enabled: true              # render-guarded: enabling without backend: adls fails the render
  account: "<storage-account>"
  filesystem: "<container>"  # or container: — set exactly one
  prefix: ""                 # optional sub-tree
  auth: client-secret        # or anon for a public container
  tenantId: "<tenant-id>"    # client-secret mode: ids are non-secret (ConfigMap)
  clientId: "<client-id>"
  existingSecret: adls-credentials   # Secret carrying the client secret (key: client-secret)
  # endpointSuffix: core.usgovcloudapi.net   # sovereign clouds
```

The client secret **never renders into values or the ConfigMap** — the chart
wires only the env-var NAME (`ADLS_CLIENT_SECRET_ENV`) and the Deployment maps
`ADLS_CLIENT_SECRET` from your Secret via `secretKeyRef` (the OneLake
credentials pattern). GCS is the same shape minus the secret: `gcs.enabled` +
`gcs.bucket` (+ `prefix`), with `gcs.credentialsFile` pointing at a **mounted**
service-account key file (you supply the volume — a Secret volume works;
key contents never appear in values) or `gcs.anonymous: true` for a public
bucket.

### 5.8 Delta Sharing (`backend: sharing`)

Read-only access to a Delta Sharing server (Databricks sharing, OSCAR, any
open-protocol server). Shares/schemas/tables join the same catalog as every
other backend; tables address as `<share>_<schema>_<table>`. The profile
(endpoint + bearer token) is supplied ONE of two ways — the token never
renders into values:

```yaml
backend: sharing
sharing:
  enabled: true                      # render-guarded with backend: sharing
  existingConfigMap: sharing-profile # a ConfigMap you own, key:
  existingConfigMapKey: delta-sharing.profile
# — or, without a file: a Secret with the two keys below
# sharing:
#   existingSecret: sharing-credentials
#   existingSecretEndpointKey: endpoint
#   existingSecretBearerTokenKey: bearer-token
```

## 6. Semantic catalog

The semantic catalog gives agents business meaning — table/column descriptions
merged into `list_tables` / `describe_table` output and the MCP resources.
Spec and data-owner guide: [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md).
Three attach routes (any backend):

1. **In values (most auditable).** Paste the catalog as **native YAML** under
   `semanticCatalog.tables:` (no stringification) — or as an inline JSON/YAML
   string (`semanticCatalog.json` / `semanticCatalog.yaml`, mutually
   exclusive) for machine-generated catalogs. The chart renders it to a
   ConfigMap mounted read-only and points `SQLHANDLER_CATALOG` at it.

   ```yaml
   semanticCatalog:
     enabled: true
     tables:
       workorder/work_order:
         description: Maintenance work order headers, one row per order
         aliases: [work orders]
         columns:
           amount: Order total in USD
   ```

   Entries with a `definition:` (a single read-only `SELECT`/`WITH`) become
   **virtual tables**. The engine **hot-reloads** the file on change, so a
   catalog edit + re-apply updates the docs **without a pod restart**. A
   broken catalog never breaks queries — it degrades to empty.

2. **Existing ConfigMap.** `semanticCatalog.existingConfigMap: <name>` mounts a
   ConfigMap you own that carries a `semantic-catalog.json` key.

3. **Upload from the UI / API (no file access needed).** Once deployed, anyone
   with UI access can enable it from the **Semantic catalog** panel of the
   data explorer (`/ui`): upload a `.json`/`.yaml` file or paste it — effective
   immediately, no restart. Same operations as API endpoints
   (`POST/GET/DELETE /api/semantic-catalog`, per-table upsert via
   `/api/semantic-catalog/table`).

   By default uploads are **per-replica and pod-local** (emptyDir). Set
   `semanticCatalog.store.enabled: true` to back uploads with a shared RWX PVC
   (1Gi default, `<release>-catalog`): an upload from any replica reaches every
   replica and survives rescheduling, still hot-reloaded. **ReadWriteMany is
   required whenever replicas can exceed 1** — the render fails loudly on an
   RWO store for a scale-out deployment. The upload API is a mutating endpoint
   covered by gateway auth / `SQLHANDLER_API_TOKEN`; `SQLHANDLER_CATALOG_UPLOAD=0`
   disables applying entirely.

## 7. What the chart renders on PCAI

When `ezua.enabled: true` (default), the chart renders:

- **Deployment + Service** (ClusterIP, port 9097, MCP at `/mcp`) — liveness
  probe on `/health` and a **backend-aware readiness probe** on `/ready` that
  performs a cheap connectivity check on the selected backend and returns 503
  when the data source is unreachable, so broken-credential pods are drained
  from the Service instead of serving errors. A startup probe gates both while
  the server warms up.
- **Istio VirtualService** routing `/mcp` and `/api` to the service with a
  **long timeout** (`ezua.virtualService.longTimeout`, default 3600s) for agent
  loops / streaming, short timeout for the rest.
- **Istio AuthorizationPolicy** (when `ezua.authorizationPolicy.enabled`) —
  `action: CUSTOM`, provider `oauth2-proxy`, applied at the `ezaf-gateway`
  ingressgateway so external callers authenticate at the PCAI gateway.
- **Kyverno ClusterPolicy** (when `ezua.kyverno.enabled`) — post-install hook
  tagging the workload `hpe-ezua/type: vendor-service` + `hpe-ezua/app:
  sqlhandler` for PCAI discovery/monitoring. Disable on clusters without the
  `kyverno.io` CRD — the hook would otherwise fail the whole release (the same
  labels are also set directly by the chart helpers).
- **Scale-out objects** when enabled: `HorizontalPodAutoscaler` (autoscaling/v2;
  needs metrics-server), `PodDisruptionBudget`, topology-spread constraints, a
  90s termination grace period for in-flight MCP tool calls, and — with
  `semanticCatalog.store.enabled` — the shared catalog PVC.
- **Prometheus PodMonitor** (`metrics.podMonitor.enabled`, default on) —
  lets the prometheus-operator scrape `/metrics`; see §4.3 for the
  label-selector and bearer-token keys.
- **Write-tier scratch PVC** when `writes.enabled` + `writes.scratch.pvc.enabled`
  (`<release>-scratch`, default 10Gi, `helm.sh/resource-policy: keep` so the
  volume survives an upgrade; RWX render-guarded, see §4.8).
- **Hardened workload profile** (`security.hardened: true`, the default):
  non-root (1000), read-only root filesystem (+ `/tmp` emptyDir), no
  ServiceAccount token, seccomp `RuntimeDefault`, dropped capabilities.

**Network posture:** the ingress NetworkPolicy is **off by default** — any pod
in the cluster can call `http://<service>.<ns>.svc.cluster.local:9097/mcp`
directly, and the ClusterIP Service exposes nothing outside the cluster (right
posture for short-lived trials). For production / real data, enable
`security.networkPolicy.enabled` and allowlist direct callers via
`allowedNamespaces`; egress is never restricted (the server must reach
AAD/OneLake/S3).

## 8. Connect an MCP client

```json
{
  "mcpServers": {
    "sqlhandler": {
      "url": "https://sqlhandler.<your-domain>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Through the PCAI gateway the bearer token comes from PCAI auth (oauth2-proxy);
in-cluster callers need no header. Humans get the same data at
`https://sqlhandler.<your-domain>/ui` behind the same gateway.

## 9. Upgrading

- **Values change** (the normal path): edit the deployment's values document in
  the PCAI Helm Values editor → apply. PCAI re-renders and rolls the pods.
- **Semantic catalog edits** hot-reload on file change — a re-apply updates
  list/describe output without a pod restart; in-flight tool calls are not
  disrupted.
- **Credential rotation**: update the out-of-band Secret, then re-apply the
  deployment (the checksum annotations force the rollout).
- **New chart/image version**: import the new packaged chart and re-apply with
  your values document; the packaged `image.tag` moves with the chart.

## 10. Reference

| What | Where |
|---|---|
| Every values key, with comments | [`../helm/values.yaml`](../helm/values.yaml) |
| Key-by-key doc coverage (this guide) | §4.1–§4.9 above |
| Paste-ready site examples | [`../helm/values-examples/`](../helm/values-examples/) |
| Secret convention (local, not packaged) | `helm/local/README.md` |
| Semantic catalog spec | [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md) |
| Post-deploy verification & troubleshooting | [VERIFICATION.md](VERIFICATION.md) |
| Scaling evidence | [BENCHMARKS.md](BENCHMARKS.md) |
