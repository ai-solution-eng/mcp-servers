# SQLhandler — configuration reference

Every knob this package has, what it changes, and its default. The operator of
a PCAI deployment never sets these directly: **the chart renders them from
values**, so the values key is the surface (see
[DEPLOYMENT.md](DEPLOYMENT.md#3-values)). This page is the map from the
values key to the environment variable the process actually reads, and the
home for the knobs the chart does not expose yet.

Two rules that hold for every variable below:

- **Read per call, not at startup.** The fleet env convention: a knob is
  re-read on each request, and a garbage value degrades to the documented
  default, never to "unlimited".
- **A variable the chart does not render is unreachable on PCAI.** The chart
  sets a fixed env set (`helm/templates/configmap.yaml` plus the Secret-sourced
  ones in `helm/templates/deployment.yaml`); there is no `extraEnv` escape
  hatch, and editing the packaged chart is not the PCAI path. §9 lists what is
  not wired.

---

## 1. Data source selection

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_BACKEND` | `onelake` in the image; the chart sets `s3` | `onelake`, `s3`/`minio`/`parquet`, `iceberg`, `nfs`/`file`/`local` — plus `adls`, `gcs`, `sharing`, which the image accepts but the chart does not expose (§9). Chart: `backend`. |
| `SQLHANDLER_SOURCES` | unset | JSON array federating several sources behind one endpoint; **overrides `SQLHANDLER_BACKEND`**. Chart: `sources:`. |
| `SQLHANDLER_ATTACH` / `SQLHANDLER_ATTACH_FILE` | unset | Read-only Postgres/MySQL attach config (JSON, or a path to it). `password_env` names only — a literal `password` key is rejected at startup. Chart: `databases:` (renders `SQLHANDLER_ATTACH`). |
| `FABRIC_TENANT_ID` / `FABRIC_CLIENT_ID` / `FABRIC_CLIENT_SECRET` | — | Entra service principal for OneLake. Chart: `fabric.credentialsSecret` (Secret). |
| `FABRIC_LAKEHOUSE_ABFSS_URL` | — | Full ABFS URL to the lakehouse (preferred). Chart: `fabric.lakehouseAbfssUrl`. |
| `FABRIC_WORKSPACE_ID` / `FABRIC_LAKEHOUSE_ID` | — | Fallback coordinates when no full URL is set. |
| `FABRIC_AUTHORITY` | `onelake.dfs.fabric.microsoft.com` | OneLake authority host. |
| `S3_ENDPOINT_URL` | — | S3 endpoint, e.g. `http://minio.minio.svc.cluster.local:9000`. Scheme may be omitted (`http` assumed unless `S3_USE_SSL`). |
| `S3_REGION` | `us-east-1` | S3 region. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_SESSION_TOKEN` | — | Credentials. Chart: `s3.credentialsSecret` (Secret); session token is image-only. |
| `S3_BUCKET` / `S3_PREFIX` | — / empty | Table root inside the object store. |
| `S3_ANONYMOUS` | `false` | Read a public bucket without keys. |
| `S3_USE_SSL` | `false` | Force `https` when the endpoint has no scheme. |
| `S3_PATH_STYLE` | `true` | Path-style addressing (what MinIO uses). |
| `S3_FORMAT` | `auto` | `auto` (detect Delta by `_delta_log`), `parquet`, or `delta`. Image-only (§9) — the chart has no `s3.format` value. |
| `ICEBERG_CATALOG_TYPE` | `rest` | `rest` or `sql` (SQLite/Postgres catalog for local dev). Chart: `iceberg.catalogType`. |
| `ICEBERG_CATALOG_URI` | — | REST endpoint, or `sqlite:///path` for a SQL catalog. |
| `ICEBERG_CATALOG_TOKEN` | — | REST bearer token. Chart: `iceberg.credentialsSecret` (Secret). |
| `ICEBERG_CATALOG_NAME` | `sqlhandler` | Catalog name; SQL catalogs partition by it. |
| `ICEBERG_WAREHOUSE` / `ICEBERG_NAMESPACE` | — | Optional location root / namespace filter for `list_tables`. |
| `ICEBERG_NESSIE_REF` | — | Nessie branch/tag reference, when the REST catalog is Nessie. |
| `NFS_ROOT` | `/data` | Mounted directory holding the tables (nfs backend, required). Chart: `nfs.rootDir`. |
| `SQLHANDLER_RAW_FORMATS` | `true` | Discover raw-format landing-zone files (CSV/JSON) as tables, size-capped. Image-only (§9). |
| `SQLHANDLER_RAW_MAX_FILE_MB` | `64` | Size cap for a raw-format table (`0` = no cap). |
| `SQLHANDLER_S3_OPTIONS` | `{}` | JSON merged into pyarrow's `S3FileSystem` kwargs — timeouts, retry limits. Chart: `s3.options`. |
| `SQLHANDLER_ONELAKE_STORAGE_OPTIONS` | `{}` | JSON merged into delta-rs storage options. Chart: `fabric.storageOptions`. |
| `SQLHANDLER_ADLS_OPTIONS` / `SQLHANDLER_GCS_OPTIONS` | `{}` | Same, for the image-only `adls` / `gcs` backends. |
| `SQLHANDLER_ALLOW_LOCAL_FS` | off | Escape hatch for the DuckDB filesystem lockdown: if the engine cannot disable local file access on a connection, it **refuses the query** unless this is set — a silent fallback would leave file reads open while the operator believed the lockdown was on. |

## 2. Query execution, safety and fairness

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_MCP_READONLY` | `1` | **MCP `run_sql` is SELECT-only** — the same DuckDB-parser guard the web API uses rejects multi-statement DDL, INSERT/UPDATE/DELETE, ATTACH, COPY, PRAGMA/SET (decision D2). `0` restores arbitrary SQL for trusted callers; attached external catalogs stay read-only in **both** modes. Chart: `mcp.readonly`. |
| `SQLHANDLER_QUERY_TIMEOUT` | `600` | Per-query wall-clock timeout (seconds; `0` = none). Enforced by interrupting the query inside DuckDB, and by a watchdog for async jobs, so a runaway query cannot hold a concurrency slot forever (decision D5). Chart: `query.timeoutSeconds`. |
| `SQLHANDLER_MAX_CONCURRENT_QUERIES` | `8` | Simultaneous DuckDB executions per pod (`0` = unlimited); excess queues. Covers the query path **and** the profile / column-stats / sample / scan / virtual-materialization compute paths. Always the **outer** bound, whatever the per-caller cap is. Chart: `query.maxConcurrent`. |
| `SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER` | = the global cap | Per-caller concurrency bucket keyed on the resolved identity (subject / key fingerprint; no identity = the global bucket) — or on the effective rules under `SQLHANDLER_FAIRNESS_SCOPE=rules`. Opt in for fairness; `0` = no per-caller gate. A caller queued on its own bucket never holds a global slot. Chart: `query.maxConcurrentPerCaller` (empty = the global cap). |
| `SQLHANDLER_FAIRNESS_SCOPE` | `identity` | The bucket key for BOTH per-caller caps: `identity` = one quota per subject/key fingerprint (default); `rules` = the caller's effective-policy hash, so users sharing a rule set (a credential group) share one concurrency/job budget. Falls back to identity when enforcement is off or the caller resolves to no rules. Cache scoping is always rule-based and unaffected. Chart: `query.fairnessScope`. |
| `SQLHANDLER_QUEUE_TIMEOUT` | `30` | Seconds a compute call may wait for a slot before failing with a clean error. Chart: `query.queueTimeoutSeconds`. |
| `SQLHANDLER_CANCEL_GRACE_SECONDS` | `5` | After a timeout-triggered interrupt, how long to wait for the query thread to unwind before reporting the timeout anyway (the result is discarded either way). Chart: `query.cancelGraceSeconds`. |
| `SQLHANDLER_MAX_ROWS` | `1000` | Server-side row cap for `run_sql` (`0` = unlimited). Also clamps `scan_table` when its `limit` is missing or negative (decision D4). Chart: `query.maxRows`. |
| `SQLHANDLER_MAX_OUTPUT_ROWS` | `1000` | Max rows **rendered** as markdown to a client. Chart: `query.maxOutputRows`. |
| `SQLHANDLER_MAX_RESULT_BYTES` | `100000` | Byte ceiling for ONE tool result (`0` = off; a configured value below 1024 is raised to it). Over-ceiling results are cut at a row/line boundary and end with a deterministic truncation marker carrying `total`, `returned`, `offset`, `next_offset` and a resume recipe. Chart: `query.maxResultBytes`. |
| `SQLHANDLER_MAX_PAGE_ROWS` | `1000` | Max rows per page from `GET /api/query/{id}/rows`. Chart: `query.maxPageRows`. |
| `SQLHANDLER_MAX_SCOPES` | `256` | Distinct caller scopes with their own concurrency bucket and metric series; beyond it new identities collapse onto `other` (shared, more restrictive). Chart: `query.maxScopes`. |
| `SQLHANDLER_ASK_MAX_TABLES` / `_COLUMNS` / `_PROFILE_COLUMNS` | `5` / `20` / `6` | `ask_data` context budget: candidate tables listed, columns described per candidate, columns profiled. Chart: `query.askMaxTables` / `query.askMaxColumns` / `query.askMaxProfileColumns`. |
| `SQLHANDLER_STRUCTURED_ERRORS` | `1` | Append one machine-readable tail line (`{"error": {"code": …, "fix_hints": […]}}`) to error messages. The human message stays primary and byte-identical; an unrecognised error gets no tail rather than a guessed code. Image-only (§9). |
| `SQLHANDLER_DUCKDB_FILE_ACCESS` | off | `1` re-enables DuckDB's own file/table functions (`read_csv`, `COPY … TO`, URL reads), which are locked down by default. |
| `SQLHANDLER_ALLOWED_ORIGINS` | none (same-origin) | Extra browser origins allowed on `/api/*`, `/ui` and `/mcp` (CORS + `/mcp` Origin validation, decision D3). The legacy `SQLHANDLER_CORS_ORIGINS` is still honoured when set; its old `*` default is gone. Chart: `mcp.allowedOrigins`. |
| `SQLHANDLER_ALLOWED_HOSTS` | unset | Strict `Host` allowlist for `/mcp` (DNS-rebinding defense; mismatch → 421). `host:*` allows any port. Unset = no pinning, because a pod cannot know its externally visible hostnames. Chart: `mcp.allowedHosts`. |
| `SQLHANDLER_EXPORT_DEFAULT_ROWS` / `_MAX_ROWS` | `10000` / `100000` | CSV/Parquet export default and hard row cap (`0` on the max = hard 1M ceiling). Chart: `query.exportDefaultRows` / `query.exportMaxRows`. |
| `SQLHANDLER_COMPRESSION` / `_MIN_SIZE` | `gzip` / `1024` | Compress HTTP responses above the byte threshold; `off` disables. Image-only. |

## 3. Async query jobs

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_MAX_JOBS` | `8` | Cap on tracked async jobs (MCP `query_submit` + `/api/jobs`). Beyond it a submit is refused with a clear message; garbage or non-positive values fall back to the default. The registry is bounded **by design**. Chart: `query.maxJobs`. |
| `SQLHANDLER_MAX_JOBS_PER_CALLER` | = `SQLHANDLER_MAX_JOBS` | Per-caller cap on **running** jobs, keyed on the same bucket as the concurrency cap (`SQLHANDLER_FAIRNESS_SCOPE`; `0` = no per-caller check). The global cap still applies on top. Chart: `query.maxJobsPerCaller` (empty = the global cap). |
| `SQLHANDLER_MAX_ASYNC_QUERIES` / `_PER_CALLER` | `100` / = global | The web UI's `/api/query/async` registry (separate from the MCP job registry above): tracked jobs and the per-caller **running** cap (`0` = no per-caller check). Chart: `query.maxAsyncQueries` / `query.maxAsyncQueriesPerCaller`. |
| `SQLHANDLER_ASYNC_JOB_TTL` | `900` | Seconds a finished job stays fetchable, shared by the web registry and the MCP jobs registry. |
| `SQLHANDLER_JOBS_DIR` | unset | Directory every replica can read-write (an RWX PVC). Finished jobs publish state + result there, so **any** replica can serve `query_status`/`query_result`/`query_cancel`; the fetch-once claim is serialised cluster-wide. Unset = the historical in-memory, per-replica behaviour. Image-only (§9) — the multi-replica correctness knob. |

## 4. Caches and performance

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_CACHE_TTL` | `3600` | Seconds `list_tables`/`describe_table` results stay in memory (`0` disables). Chart: `cache.ttl`. |
| `SQLHANDLER_LIST_ASYNC_REFRESH` | `true` | Serve `list_tables` from cache immediately and refresh in the background (plus once per TTL). Chart: `cache.listAsyncRefresh`. |
| `SQLHANDLER_DATASET_CACHE_TTL` | `3600` | Seconds an open Dataset handle is reused (`0` disables). |
| `SQLHANDLER_DATASET_CACHE_TABLES` | `8` | Max tables held in the dataset LRU. |
| `SQLHANDLER_VERSION_CHECK_INTERVAL` | `10` | How often a Delta table's snapshot version is re-checked for invalidation (`0` = every query). Chart: `cache.versionCheckInterval`. |
| `SQLHANDLER_CACHE_KEY_VERSION_THROTTLE` | off | Opt-in: throttle the version check that keys cached **results** (one provider call per table per interval instead of one per query). Off preserves the ratified contract — an ETL commit invalidates a cached result immediately. |
| `SQLHANDLER_RESULT_CACHE_TTL` | `3600` | Seconds an identical query's result is served from memory (keyed by sql/params/limits + base-snapshot versions + policy hash). `0` disables. Chart: `cache.resultCacheTtl`. |
| `SQLHANDLER_RESULT_CACHE_MAX_BYTES` | `268435456` (256 MiB) | In-memory cap for cached results, LRU-evicted. |
| `SQLHANDLER_SINGLEFLIGHT` / `_TIMEOUT` | `1` / derived | Coalesce identical **cold** queries: one leader executes and callers with the same effective rules wait for the cache fill instead of re-running (a cache stampede becomes one execution). Keyed on the result-cache key — masked and unmasked never coalesce. Waiters block before taking a concurrency slot; on leader failure/timeout they run their own copy. Timeout defaults to the query watchdog + cancel grace. Chart: `query.singleflight` / `query.singleflightTimeout`. |
| `SQLHANDLER_VIRTUAL_CACHE_TTL` | `3600` | Seconds a virtual table's materialized result is reused (keyed by definition + base-snapshot versions). `0` disables. |
| `SQLHANDLER_VIRTUAL_CACHE_DIR` | the cache dir | Where materializations are written; point at an RWX PVC to materialize once per deployment. |
| `SQLHANDLER_VIRTUAL_CACHE_MAX_BYTES` | 2 GiB | Skip caching larger virtual results — served live instead (`0` = unlimited). |
| `SQLHANDLER_VIRTUAL_CACHE_SORT` | on | Cluster materialized results by their lowest-cardinality columns so row-group statistics prune filtered reads. |
| `SQLHANDLER_L2_ENABLED` / `_DIR` / `_TTL` / `_MIN_BYTES` / `_MAX_BYTES` / `_WRITE_ASYNC` / `_METADATA` | off / — / 3600 / 262144 / 2 GiB / on / on | Shared cross-replica result cache on an RWX directory. Chart: `cache.l2.*`. Requires a genuinely shared volume — on an RWO claim it looks shared and shares nothing. |
| `SQLHANDLER_BLOCK_CACHE` / `_DIR` / `_BLOCK_SIZE` / `_MAX_BYTES` / `_INCLUDE_LOCAL` | off / `<tmp>/sqlhandler-block-cache` / 8 MiB / 4 GiB / off | Disk cache for parquet footers and column chunks read through pyarrow (s3 + iceberg; the OneLake/Delta data path is cached snapshot-version-scoped, delta-log IO stays in delta-rs). Chart: `cache.blockCache.*`. |
| `SQLHANDLER_PREWARM_TABLES` | unset | Comma-separated tables warmed at first request. Unset = the busiest tables of the previous run are warmed (usage counts persisted with the disk-warm cache). Chart: `cache.prewarmTables`. |
| `SQLHANDLER_PREWARM_ROWGROUPS` | `1` | Row groups read per prewarmed table (`0` = metadata only, the historical behaviour). Chart: `cache.prewarmRowgroups`. |
| `SQLHANDLER_PROFILE_MAX_ROWS` | `1000000` | Sample cap for `profile_table`/`column_stats` (`0` = full table). Chart: `cache.profileMaxRows`. |
| `SQLHANDLER_PROFILE_CACHE_SIZE` | `512` | How many profile/column_stats results to keep (keyed by source, path, column subset, policy hash). `0` = unbounded. Chart: `cache.profileCacheSize`. |
| `SQLHANDLER_PREVIEW_FASTPATH` | on | First-row-group fast path for bare `LIMIT` previews. `0` disables. |
| `SQLHANDLER_QUERY_MEMORY_SIZE` | `50` | Recent query outcomes kept for the `sqlhandler://query-memory` resource (`0` disables). |
| `SQLHANDLER_DUCKDB_MEMORY_FRACTION` | `0.6` | DuckDB's in-query budget as a fraction of the pod's memory **limit**; the engine spills to the temp dir instead of being OOMKilled. `0` disables the cap. Chart: `cache.duckdbMemoryFraction`. |
| `SQLHANDLER_DUCKDB_TEMP_DIR` | `/tmp/sqlhandler-duckdb-spill` | DuckDB spill directory. Chart: `cache.spillDir`. |
| `SQLHANDLER_CACHE_DIR` | `/tmp/sqlhandler-cache` | Disk-warm metadata cache (usage counts, catalog store fallback, saved-query fallback). Chart: `cache.cacheDir`. |

**Policy-scoped caches (policy-as-code on).** Every cache identity — result L1/L2, virtual materialization, describe, profile, column_stats and the shared metadata tier — folds in `policy=<sha256>` computed from the caller's **resolved** effective rules: the sorted, de-duplicated group specs. Group names and the caller's identity are not part of it, so callers whose rules are identical share entries (the same named group, the same `datasets` grants, or the `default_group` fallback), while any rule difference forks them. Enforcement off omits the part entirely, keeping the historical byte-identical keys. The **file** hash still folds the whole policy document (hot-reload on any edit); the **effective** hash does not, so an unrelated assignment edit no longer rekeys existing callers. Upgrading from a build that hashed group names/the document rekeys once — old entries age out via TTL, no purge is needed.

**Profile / column_stats sharing.** Both are GLOBAL caches, not per-user ones: the in-process tier is keyed on (source, path, column subset, policy hash) with a ``SQLHANDLER_CACHE_TTL`` TTL, and the shared metadata tier adds the table's snapshot version so an ETL commit invalidates immediately across replicas. Virtual-table entries are keyed on the definition digest plus every BASE table's snapshot version (a virtual table has no snapshot of its own), so base commits invalidate them too. Attached-database profiles stay per-replica (their catalogs version differently). Two callers with the same effective rules share; masked and unmasked callers never do.

**Shared-disk trust boundary.** The L2 result/metadata tiers and the virtual-materialization directory are access-controlled by cache KEY (which carries the policy hash), not by filesystem ACL per caller: any process that can read the volume can read every artifact. Keep the PVC pod-private — the pod creates its cache directories 0o700 and writes artifacts 0o600, best-effort on volumes that ignore modes.

## 5. Semantic catalog

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_CATALOG` | unset | Path to a catalog JSON/YAML file; merged into `list`/`describe` and hot-reloaded on mtime. Chart: `semanticCatalog.*` (ConfigMap mount). |
| `SQLHANDLER_CATALOG_STORE` | `<cache-dir>/semantic-catalog.json` | Writable path for UI/API uploads; **overrides `SQLHANDLER_CATALOG` while present**. Chart: `semanticCatalog.store` (RWX PVC). |
| `SQLHANDLER_CATALOG_UPLOAD` | on | `0` disables the catalog mutation endpoints (upload/clear and the browser editor's apply/remove); reading still works. |

## 6. Saved queries

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_SAVED_QUERIES_PATH` | next to the cache dir | JSON file for the saved-parameterized-query store. Writes are atomic (temp+rename, mode 600) and a corrupt file degrades to empty rather than erroring. Chart: `semanticCatalog.store.savedQueriesPath` (same PVC as the catalog store). |
| `SQLHANDLER_SAVED_QUERIES_MAX` | `500` | Hard bound on stored entries — the store is rewritten in full on every save, so an unbounded map is a growth channel. |

## 7. Identity, policy and writes

| Variable | Default | Purpose |
|---|---|---|
| `MCP_API_KEYS` (or `SQLHANDLER_API_KEYS`) | chart-wired, **required** | Comma-separated `/mcp` API keys, compared in constant time. The chart wires `MCP_API_KEYS` from the operator's Secret unconditionally, so the pod does not start without it. Rotation needs a rollout restart (a `secretKeyRef` env var is frozen at container start). Chart: `security.apiKey`. |
| `SQLHANDLER_API_TOKEN` | unset | Optional shared-token gate on `/api/*` (comma-separated list allowed, so it can carry the same rotation Secret as `MCP_API_KEYS`; accepted as `X-API-Key`, `X-API-Token` or `Authorization: Bearer`, compared constant-time). Unset = no token gate; the `/api` surface is still identity-gated by `SQLHANDLER_REQUIRE_IDENTITY` (chart default ON) and an explicit API key authenticates there. Image-only. | Bearer/`X-API-Token` gate for `/api/*`. Unset = no token check (the gateway is expected to authenticate). |
| `SQLHANDLER_METRICS_AUTH` | `false` | Gate `/metrics` + `/ready` behind the API token or an MCP API key. Gate on with no credential configured → 401 for everyone (fail closed). Chart: `security.metricsAuth`. |
| `SQLHANDLER_REQUIRE_IDENTITY` | off (chart default: **on**) | Refuse anonymous callers with 401 on `/mcp` and `/api/*`. Probes never gate, and the UI shell stays open (data is gated at `/api`). The chart turns this on by default since the 2026-11-12 audit (P1-6: the `/api` data plane was anonymous in the chart posture); set `security.identity.requireIdentity: false` only when the gateway authenticates every caller and nothing else can reach the pod. Image-only. |
| `SQLHANDLER_TRUST_BROWSER_HEADERS` | off | Trust oauth2-proxy's browser identity headers. Only valid with the workload AuthorizationPolicy enabled — otherwise any caller can assert any identity. Image-only. |
| `SQLHANDLER_RELAY_HMAC_SECRET` | unset | HMAC key for the gateway's caller-relay attribution (`X-MCP-Caller-Subject`). **SET:** the header is honored only with a valid `X-MCP-Caller-Sig`/`X-MCP-Caller-Ts` pair (±300 s). **UNSET:** the header is **REFUSED** — the attributed subject feeds authorization (policy `admins`, subject grants, owner scopes, write namespaces), so an unsigned claim would let any key holder impersonate anyone, admins included (audit 2026-11-12, P1-5). Image-only. |
| `SQLHANDLER_TRUST_RELAY_HEADERS` | off | Legacy escape hatch: `1`/`true`/`yes`/`on` trusts an **UNSIGNED** `X-MCP-Caller-Subject` over a valid key (pre-2026-11 posture; any key holder can claim any subject). Only for a gateway that cannot sign AND whose ingress is pinned to it. Logs one WARNING per process. Image-only. |
| `SQLHANDLER_OIDC_ENABLED` / `_ISSUER` / `_AUDIENCE` / `_IDENTITY_CLAIM` / `_JWKS_URL` / `_JWKS_REFRESH_SECONDS` / `_FETCH_TIMEOUT_SECONDS` / `_CLOCK_SKEW_SECONDS` | off / — / `ua` / `preferred_username` / — / 3600 / 3 / 60 | Resource-server JWT rung: verify a caller's SSO bearer token against the issuer's JWKS. No client secret, no redirect URI. Chart: `security.oidc.*`. **`aud` is REQUIRED** (string or list) and must match the token's audience; `azp` is checked in addition when present. Keycloak access tokens default to `aud: ["account"]`, so a Keycloak deployment must either add an audience mapper for `ua` or set `SQLHANDLER_OIDC_AUDIENCE=account` — otherwise every JWT is declined and (with `requireIdentity` on) callers get bare 401s. Declines are logged at DEBUG only; raise the log level while bringing a deployment up. |
| `SQLHANDLER_OIDC_SSO_*` (`ENABLED`, `PROVIDER_URL`, `AUTHORIZATION_ENDPOINT`, `TOKEN_ENDPOINT`, `CLIENT_ID`, `CLIENT_SECRET`, `REDIRECT_URI`, `SCOPES`, `COOKIE_NAME`, `COOKIE_SECURE`, `COOKIE_MAX_AGE`) | off | Browser SSO authorization-code flow (fleet decision D22) — the app is an OIDC **client**, and the resulting cookie is one more credential envelope through the same identity resolution. Image-only. |
| `SQLHANDLER_POLICY_ENABLED` / `_FILE` / `_SEED_FILE` | off / — / — | Policy-as-code: per-caller row filters, column masks, hidden tables and dataset ACLs from an operator-authored document, hot-reloaded on mtime. A present-but-invalid file fails **closed** — including a mask spec that cannot compile (`sha256:12x`), which is refused at LOAD rather than silently unmasking at query time. Chart: `security.policy.*`. |
| `SQLHANDLER_MASK_HASH_SECRET` | unset | Keying secret for `hash` column masks. **Set:** masks render `sha256(secret \|\| value)` — keyed, so low-entropy values (SSN/email/salary shapes) cannot be enumerated, and digests are not comparable across deployments; rotating it rekeys every masked cache entry. **Unset:** the plain digest, with one WARNING at policy load. Image-only. |
| `SQLHANDLER_TRUST_RELAY_HEADERS` | off | Legacy escape hatch for UNSIGNED relay attribution — see the relay row above. Image-only. |
| `SQLHANDLER_ICEBERG_ALLOW_LOCAL_FILES` | off | Allow Iceberg catalog-manifest paths to reach the pod's local filesystem (contained inside `ICEBERG_WAREHOUSE` by realpath). Off = `file://`/absolute/schemeless paths are refused (they bypassed the DuckDB filesystem lockdown). Image-only. |
| `SQLHANDLER_LISTING_MAX_ENTRIES` | 250000 | Cap on entries materialized by one discovery listing (S3/NFS/Iceberg). Past the cap the listing is REFUSED with a clear error rather than silently truncated; `0` = unlimited, negative falls back to the default with a warning. Image-only. |
| `SQLHANDLER_SHARING_ALLOW_PRIVATE_URLS` | off | Allow Delta Sharing presigned download URLs that resolve to private/loopback addresses (on-prem proxy-download servers). Off = such URLs are refused (SSRF guard); the configured sharing endpoint's own origin is always allowed. Image-only. |
| `SQLHANDLER_ADMIN_KEYS_FILE` | unset | JSON store the self-mint writes minted keys to (must be writable; a subpath of an RWX claim). Without it self-mint answers 503. Chart: `security.adminKeys.*`. |
| `SQLHANDLER_SELF_MINT_MAX_KEYS` | `1` | Minting beyond this revokes the subject's oldest self-minted key. Chart: `security.selfMintMaxKeys`. |
| `SQLHANDLER_WRITES_ENABLED` / `_SCRATCH_ROOTS` / `_LEASE_TTL` / `_ICEBERG_CATALOG_URI` / `_ICEBERG_WAREHOUSE` | off / empty (fail closed) / 300 / — / — | Agent-facing scratch writes (CTAS / INSERT / COPY INTO) confined to subject-scoped namespaces under an allowlisted root. Chart: `writes.*`. |

**Group membership is policy-file data, not an IdP claim.** The policy's `subjects`/`key_fps` bindings (plus `default_group`, and the compiled `datasets.assignments` in the ACL vocabulary) are the only thing that resolves a caller's groups — and therefore the only thing that scopes cache sharing. The `Caller.groups` field carried by the browser/oauth2-proxy rung is informational and does **not** affect enforcement or cache keys. To make SSO group membership authoritative, map the subjects into policy groups in the file; do not rely on a forwarded header.

## 8. Observability and runtime

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_AUDIT_LOG` | unset (off) | Path to a JSONL audit file: one line per query outcome (`ts`, `event`, `sql`, `state`, `duration_ms`, `n_rows`, `error`), one per scratch write, and one per admin mutation (fingerprint only, never the key). Best-effort — a write failure never breaks a query. Needs a durable volume. **Chart:** `security.audit.enabled` + `security.audit.existingClaim`/`security.audit.pvc.create` (the render refuses an ephemeral trail). |
| `SQLHANDLER_READINESS_CHECK` | on | `0` disables the backend-aware `/ready` connectivity probe. |
| `SQLHANDLER_READY_DEGRADED_GRACE` | `60` | Seconds a failing backend check reports **DEGRADED** (still ready) before flipping to **NOT READY**. `0` = binary behaviour. The grace exists so one flaky storage check does not drain every replica at once, while a persistent failure still does within a minute. |
| `SQLHANDLER_TRANSPORT` | `stdio` | Default MCP transport for the CLI (`streamable-http` in the chart). Chart: `mcp.transport`. |
| `SQLHANDLER_ENV_FILE` | auto-discovered `config/.env` | Path to a `.env` file. Local development only. |
| `SQLHANDLER_API_BODY_MAX_BYTES` | `2097152` (2 MiB) | Request-body ceiling for `/api/*`; an oversized body is refused 413 from `Content-Length` alone, or mid-read when the length is unknown. |
| `SQLHANDLER_DUCKDB_EXTENSION_DIR` | image path | Where the preinstalled scanner extensions live; runtime extension downloads stay disabled. |
| `SQLHANDLER_POD_NAME` | `HOSTNAME` | Pod identity reported by `/api/status` and the audit/metrics labels. Set by the kubelet; not a values key. |
| `SQLHANDLER_PYG_STYLE_DARK` / `_LIGHT` | `monokai` / `default` | Pygments style for the UI editors' server-side highlighting. Cosmetic; highlighting degrades to plain text without pygments. |

## 9. What the chart does not wire

The chart renders a fixed environment (`helm/templates/configmap.yaml` +
Secret-sourced values in `helm/templates/deployment.yaml`). There is **no
`extraEnv`**, and the PCAI path has no way to add one — so the variables
marked *image-only* above are unreachable in a PCAI deployment. That is a
chart gap, not a documentation gap, and it is reported as such rather than
worked around:

| Capability | Env it needs | Consequence today |
|---|---|---|
| Structured error tails | `SQLHANDLER_STRUCTURED_ERRORS` | On by default, so agents already get them; only the *off* switch is unreachable. |
| Cross-replica async jobs | `SQLHANDLER_JOBS_DIR` | On `replicaCount > 1`, a poll routed to another replica returns *Unknown job id*. **Keep jobs and scale-out apart** until the chart exposes an RWX jobs dir. |
| Audit log | `SQLHANDLER_AUDIT_LOG` | Off by default; now chart-wired (`security.audit.*`) so enabling it needs values, not a hand-patched Deployment. Off = no per-query compliance trail (metrics still count outcomes). |
| ADLS / GCS / Delta Sharing backends | `SQLHANDLER_BACKEND=adls`/`gcs`/`sharing` + their options | The image supports them; the chart's `backend:` does not, so they cannot be selected on PCAI. |
| Raw-format landing zones | `SQLHANDLER_RAW_FORMATS`, `_RAW_MAX_FILE_MB` | On by default in the image; no values key. |
| Browser SSO (OIDC code flow) | `SQLHANDLER_OIDC_SSO_*` | The resource-server JWT rung (`security.oidc.*`) is wired; the browser flow is not. |
| Response compression | `SQLHANDLER_COMPRESSION` | gzip is on by default; only the off switch is unreachable. |

Non-PCAI deployments (a plain Kubernetes cluster, `docker run`, the CLI) set
any of these directly. That is the developer path, not the deployment path —
see [DEPLOYMENT.md](DEPLOYMENT.md#5-upgrade-rollback-and-uninstall).
