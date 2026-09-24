# SQLhandler — Features

Fast, direct SQL access to columnar lake data — OneLake/Delta Lake, S3/MinIO Parquet, Delta-on-S3, Iceberg catalogs, and NFS/local directories — exposed as an **MCP server** (MCP 2.0, stateless streamable-http at `/mcp`) with a bundled read-only web UI and JSON API. This document describes the features of the **current stack** (chart/image **1.6.1**; the 0.9.0→1.0.0 feature wave plus the subsequent 1.1–1.6 additions unless marked otherwise).

---

## Backends & data sources

One engine, seven backends (selected by `SQLHANDLER_BACKEND` / the chart's `backend:` value) — they share the same SQL engine, caches, MCP tools, and backend-aware readiness probe; only the `DataProvider` differs:

| Backend | Reads |
|---|---|
| `onelake` | Microsoft Fabric OneLake — Delta Lake over ABFS, Entra service principal |
| `s3` / `minio` | S3-compatible object storage (MinIO/AWS) — Parquet, plus Delta tables via `S3_FORMAT=auto` (`_delta_log` detection, time travel) |
| `adls` | Azure Data Lake Storage Gen2 — Parquet + Delta (time travel), via helm values `adls:` (`adls.enabled` + `adls.account`/`adls.filesystem`/`adls.prefix`, `adls.auth: anon` for a public container or `client-secret` with `adls.tenantId`/`adls.clientId` and the secret in a Secret wired via `adls.existingSecret` — the client secret never renders, `ADLS_CLIENT_SECRET_ENV` carries the env-var NAME; `adls.endpointSuffix` covers sovereign clouds). Env names secondary: `ADLS_ACCOUNT` / `ADLS_CONTAINER` / `ADLS_PREFIX` / `ADLS_AUTH` / `ADLS_TENANT_ID` / `ADLS_CLIENT_ID` / `ADLS_CLIENT_SECRET_ENV` / `ADLS_ENDPOINT_SUFFIX`. pyarrow 25's `AzureFileSystem` implements the client-credentials flow natively |
| `gcs` | Google Cloud Storage — Parquet + Delta (time travel), via helm values `gcs:` (`gcs.enabled` + `gcs.bucket`/`gcs.prefix`; `gcs.credentialsFile` is a MOUNTED service-account key-file path — the operator supplies the volume — or `gcs.anonymous: true` for a public bucket). Env names secondary: `GCS_BUCKET` / `GCS_PREFIX` / `GCS_CREDENTIALS_FILE` / `GCS_ANONYMOUS`. pyarrow 25's `GcsFileSystem` resolves the key file through `GOOGLE_APPLICATION_CREDENTIALS` |
| `iceberg` | Apache Iceberg tables through a catalog — REST (incl. Databricks Unity Catalog), SQL, AWS Glue (AWS_* env / IRSA), Hive metastore (thrift), or Nessie (REST URI + `ICEBERG_NESSIE_REF` branch/tag) — Parquet data files |
| `nfs` / `file` | Mounted directory (NFS/PVC/hostPath) — Delta **and** Parquet |
| `sharing` | Delta Sharing protocol servers (Databricks / OSCAR) — read-only over the open protocol, profile-file auth (YAML or JSON) or env-only endpoint+token; stdlib client, no extra dependency |

Plus two composition mechanisms:

- **Federated multi-source** — `SQLHANDLER_SOURCES` / the chart's `sources:` list federates several buckets/sources (any mix of backends) behind one endpoint: source-qualified tables, one shared cache, cross-source `JOIN`s in a single `run_sql`.
- **External databases (read-only attach)** — `SQLHANDLER_ATTACH` / the chart's `databases:` list attaches external servers read-only (`ATTACH ... READ_ONLY` — writes are rejected by DuckDB itself) — Postgres (plus postgres wire-compatible servers), MySQL/MariaDB, SQLite files, and SQL Server via the native-TDS `mssql` community extension; their tables join with lake tables in the same query as `<db-alias>.<schema>.<table>`. TLS and other driver options flow through a per-entry `params` object. **DuckLake** (`type: ducklake`) attaches DuckDB's lakehouse catalogs the same way — the operator-authored catalog connect string (`sqlite:` / `postgres:` / `md:` forms) rides the `ducklake:` URL scheme; row reads through the catalog go via DuckDB's own fs layer, so they need `SQLHANDLER_DUCKDB_FILE_ACCESS=1`, while read-only is enforced independently by the `READ_ONLY` attach plus the unconditional attached-catalog SQL guard (writes are refused with the flag on or off). **MongoDB** (`type: mongodb`, community `mongo` extension — host + a REQUIRED `database` scoping the attach, Atlas `srv`/`tls` via `params`) and **BigQuery** (`type: bigquery`, community `bigquery` extension — `database` is the GCP `project` or `project.dataset`, optional `billing_project`; auth is Google ADC in-container, or a temporary access token via `password_env` applied as a DuckDB Secret scoped to the attached project) are also full attach types, but their extensions are OPT-IN at build time (not in the default image's bake list — configuring them without extending the bake fails with a clear "extension not baked into this image" error naming the Dockerfile opt-in; see the Dockerfile note). `clickhouse` (no DuckDB 1.5.5 extension with ATTACH support — only `chsql_native` scan functions) and a standalone `motherduck` type (a bare `md:` ATTACH auto-installs a signed extension from MotherDuck's own repo and can hang the pod in an OAuth login) are deliberately NOT types — MotherDuck is reachable through a `ducklake` `md:` catalog with the token via `password_env`.

**Raw-format landing zone (s3 + nfs/file backends)** — small raw-text files are discovered as tables with the exact same folder conventions as Parquet: `.csv` `.tsv` `.json` `.ndjson` `.jsonl` (plus `.gz` variants) become tables — a single file by its stem, a folder of raw files as one table, schema folders as schemas, partition (`dt=…`) folders folded in by directory (hive partition-COLUMN extraction stays Parquet-only; raw tables get no derived `key=value` columns). Gated by design as a LANDING-ZONE feature, never the scan path for big data: Helm values `rawFiles.enabled` (true default; false hides every raw table) and `rawFiles.maxFileMB` (default 64 MB per file, 0 = unlimited — a table with ANY oversized file is skipped whole with a log line; for `.gz` the cap applies to the COMPRESSED size, so it is approximate and a decompression bomb can exceed it), which the chart renders into the `SQLHANDLER_RAW_FORMATS` / `SQLHANDLER_RAW_MAX_FILE_MB` env the server reads. Precedence per table folder: Delta > Parquet > raw (a `.csv` in a Parquet table folder is ignored), and a same-named raw collision (`.csv` + `.json`) resolves first-alphabetically with a warning. Raw tables display a `RAW` badge where virtual tables show `VIRTUAL`, `describe_table` carries the format, and pyarrow reads them as CSV (tab-delimited for `.tsv`) or newline-delimited JSON — schema inference at open, a mixed-type failure surfaces as the standard error shape. Honest performance note: row-group pruning, statistics, and the count(*) metadata fast path are Parquet/Delta-only — a raw scan reads whole files and `count(*)` scans (correct, not free); raw is for small landing-zone files, and the write tier's `COPY TO` Parquet is the promotion path.

---

## Current stack

| Layer | Technology |
|---|---|
| Language | Python ≥ 3.11, fully type-checked (mypy) and linted (ruff) |
| MCP | `mcp>=2.0` SDK — low-level `MCPServer`, stateless streamable-HTTP at `/mcp` |
| SQL engine | DuckDB ≥ 1.0 (per-process connection; aggregations/joins push down to the lake) |
| Data access | pyarrow ≥ 17 (S3/Azure filesystems + dataset engine), `deltalake` ≥ 1.0 (native Delta reader incl. OneLake ABFS), optional `pyiceberg[pyarrow]` (+ SQLAlchemy for SQL catalogs; `pyiceberg[glue]`/`pyiceberg[hive]` extras opt in the AWS Glue / Hive-metastore catalog types — absent, those types fail with an actionable LakehouseError) |
| HTTP | uvicorn + Starlette ASGI app hosting `/mcp`, the JSON API, `/ui`, `/health`, `/ready`, `/metrics` |
| Web UI | one self-contained `index.html` — no build step, no CDN, no framework |
| Deploy | Helm chart for Kubernetes / PCAI (oauth2-proxy gateway, non-root hardened image, health/startup probes, HPA + PDB + topology spread for scale-out, optional shared catalog PVC) |
| Ops | zero extra dependencies — Prometheus text exposition and JSONL audit log are hand-rolled |

The `DataProvider` interface is the only extension point for a new source: each backend (onelake, s3, adls, gcs, iceberg, nfs/file, sharing) is a subclass plus one `make_provider` branch.

---

## 1. Agent ergonomics (MCP surface)

Features that make LLM agents effective against the lake on the first try.

### Tools
- `list_tables` — enumerate tables (any backend), annotated with catalog descriptions when configured; source-qualified names in federated mode.
- `search_tables` — keyword search over table/column names **and** semantic catalog docs, ranked best-first (exact/substring scoring, then a fuzzy layer so typo'd names still match); each hit carries its `matched_on` reasons. For when the table list is long.
- `describe_table` — columns/types/URI + catalog docs.
- `profile_table` — column-level statistics **before** writing SQL: min/max, approx distinct count, null %, avg/std, q25/q50/q75, exact row count from Parquet/Delta metadata. Optional comma-separated `columns` subset. Scans a bounded sample (`SQLHANDLER_PROFILE_MAX_ROWS`, default 1M; 0 = full) and is cached like describe.
- `column_stats` — the same statistics for ONE column, plus top values with counts: distinct count, null count/%, min/max, q25/q50/q75, top-5 values — over the same bounded sample (never a full-table scan beyond the profile cap).
- `run_sql` — DuckDB SQL with `output_format` (**markdown** default / json / csv / arrow), bind `params`, and `version_as_of` time travel.
- `scan_table` — pyarrow column/row pull with row limit; same formats and time travel.
- `query_submit` / `query_status` / `query_result` / `query_cancel` — **async query jobs**: submit returns a `job_id` immediately (read-only guard applied at SUBMIT), status polls state/columns/n_rows, the result is handed over **once** and then freed from memory, cancel interrupts via DuckDB. Same engine path as `run_sql` (timeout watchdog-enforced, `SQLHANDLER_MAX_ROWS`-bounded, audit-logged); in-memory registry capped by `SQLHANDLER_MAX_JOBS` (default 8; a restart clears it). REST twins under `/api/jobs/*`.
- `query_save` / `query_list` / `query_delete` / `query_saved` — **saved parameterized queries**: a name → SQL + default bind params store (`SQLHANDLER_SAVED_QUERIES_PATH` JSON file; atomic writes; a corrupt file degrades to empty). Save-time validation parses the SQL and applies the read-only guard, re-applied at run time; call-time params override stored ones as **bind** parameters (never string-interpolated). Writes are auth-gated: with `SQLHANDLER_API_TOKEN` / `MCP_API_KEYS` / `SQLHANDLER_API_KEYS` configured, an unauthenticated save/delete is refused; with none configured (single-user-local mode) writes are open with a loud startup note. REST twins under `/api/saved-queries/*`.
- `explain_query` — **cost estimate without execution**: referenced tables with metadata row counts and bytes-to-scan (Delta add-action file sizes / Iceberg manifest sizes = `exact`; uncompressed Parquet row-group totals = `approx`; attached databases degrade to `none`), every number carrying its confidence label and keyed to the snapshot version it describes; the **warm/cold band** (is the exact query identity already in the L1 result cache / the shared L2); optional `include_plan` adds DuckDB's own `EXPLAIN (FORMAT JSON)` summary (planning only — the data path never runs, and a virtual table is never materialized by an explain). The same sqlguard rule `run_sql` applies refuses everything but SELECT / EXPLAIN SELECT.
- `ask_data` — **plan a question, don't execute it**: keyword-searches the tables (top 5), describes the best hit (up to 20 columns + catalog docs), optionally profiles up to 6 of its columns, then drafts ONE candidate SELECT (against the SQL-addressable qualified name) and a suggested follow-up. Markdown output ends in a "run this with run_sql" footer — execution is always a separate, explicit `run_sql` call (the plan→apply separation); `execute` is accepted for symmetry and has no effect.

### Resources & prompts (the other MCP primitives)
- `sqlhandler://catalog` — every table + its business description (data dictionary).
- `sqlhandler://table/<name>/schema` — one table's schema + column docs (also exposed as the resource template `sqlhandler://table/{table}/schema`).
- `sqlhandler://query-memory` — the last 50 query outcomes (`SQLHANDLER_QUERY_MEMORY_SIZE`), so later agent sessions reuse proven SQL patterns instead of rediscovering them.
- Prompts: `explore-data` (list → catalog → profile → SQL) and `analyze-table` (profile-first deep dive on one table).

### Semantic catalog
`SQLHANDLER_CATALOG=<file.json|file.yaml>` merges human-written table/column documentation into `list_tables` / `describe_table` / resources. Hot-reloaded on change (cached describes invalidated); a missing/broken file never breaks queries.

- **Virtual tables** — a catalog entry keyed by a clean bare identifier with a
  `definition` (a single read-only `SELECT`/`WITH`) becomes a queryable virtual
  table: listed with a `VIRTUAL` badge, schema derived from the definition,
  user filters still push down into the physical scans, definitions compose.
  Materialized results are clustered and reused across queries and replicas
  (see Performance). Spec: [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md).
- **Browser editing (1.4.0)** — a global **Edit YAML…** editor in the lower-left *Semantic catalog* panel (whole live catalog as YAML, JSON toggle) plus a per-dataset **Semantic** tab on every table (that table's breakout; skeletons prefilled from the real column list; per-entry upsert/remove). Both write through the same validated upload store — YAML default, JSON swap, `pygmentize -g`-style server-side highlighting (graceful plain-text fallback without pygments), `SQLHANDLER_CATALOG_UPLOAD=0` disables applying.
- **dbt import** — a dbt `target/manifest.json` compile artifact becomes catalog content: model/column descriptions map straight over, `meta.sqlhandler.*` opts into aliases / virtual tables / hiding, and virtual generation is double-gated (node meta **and** `allow_virtual: true`). Preview-first (`POST /api/semantic-catalog/import-dbt` returns the proposed catalog without writing) then Apply (merges through the validated store — only entries from a previous dbt import are refreshed; hand-written docs survive), plus an **Import dbt…** button in the same panel. No dbt dependency (stdlib JSON) and **no new env vars or chart values** — it rides the existing catalog surface. Spec: [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md#importing-from-dbt).

### Self-correction loops
- **Did-you-mean errors** — a bad table name in SQL returns the nearest real table names, so the agent self-corrects in one round-trip.
- **Usage-driven prewarm** — with `SQLHANDLER_PREWARM_TABLES` unset, the busiest tables of the previous run (usage counts persisted with the disk-warm cache) are prewarmed on restart.

---

## 2. Query engine capabilities

- **Parameterized queries** — `run_sql` accepts bind `params`: an object for named `$placeholders` or an array for positional `?`. Reusable templates stay injection-safe — and the saved-query tools (`query_save`/`query_saved`) store those templates with their default bind params so they can be rerun by name, values still bound (never interpolated).
- **Time travel** — `version_as_of` on `run_sql` / `scan_table`: a Delta snapshot version (nfs / onelake / Delta-on-S3) or an Iceberg snapshot id. Applies to every versionable table the query touches; plain-Parquet tables in the same query are a clear error. Historical datasets are cached per version (a snapshot never changes).
- **Query timeout** — `SQLHANDLER_QUERY_TIMEOUT` (seconds; **default 600** — decision D5: a runaway query used to hold a concurrency slot forever; `0` = off) interrupts a query inside DuckDB: no leaked threads, clean error to the caller. Applies to MCP tools and the web API alike.
- **Concurrency cap** — `SQLHANDLER_MAX_CONCURRENT_QUERIES` (default 8, 0 = unlimited) bounds simultaneous DuckDB queries per pod; excess queries queue up to `SQLHANDLER_QUEUE_TIMEOUT` (default 30 s) then fail with a clear error instead of piling up on the container. Chart values `query.maxConcurrentQueries` / `query.queueTimeoutSeconds` wire these env vars (default render = app defaults).
- **Output formats** — every result-returning tool/endpoint renders markdown (human/LLM-friendly), compact JSON (`{columns, rows}`), CSV, or Arrow IPC (`arrow`: the result's exact schema + rows base64-encoded under a one-line header — `pa.ipc.open_stream` reads it back with decimals/timestamps/nulls intact, which the text formats flatten).
- **Delta tables on S3** — `S3_FORMAT=auto|parquet|delta` on the s3 backend: `auto` (default) detects Delta tables by their `_delta_log` and reads the rest as plain Parquet, so one bucket can mix formats with time travel where a Delta log exists.

---

## 3. Web API & UI (read-only data explorer)

Same engine, caches, and read-only guard as the MCP tools — no extra deployment.

### Async query jobs (long-running queries)
| Endpoint | Purpose |
|---|---|
| `POST /api/query/async` | validate + start; returns `{"query_id", "state"}` |
| `GET /api/query/{id}` | status: `running`/`done`/`error`/`cancelled`, columns, n_rows |
| `GET /api/query/{id}/rows?offset=&limit=` | paginated rows (page cap 1000) |
| `DELETE /api/query/{id}` | cancel a running job (DuckDB interrupt) |
| `POST /api/jobs` | the MCP `query_submit` twin: submit-time read-only guard, `SQLHANDLER_MAX_JOBS` cap, watchdog-enforced timeout |
| `GET /api/jobs/{id}` | job status (+ `result_fetched`) |
| `GET /api/jobs/{id}/result` | the result, handed over **once** then freed (second fetch → 409) |
| `DELETE /api/jobs/{id}` | cancel a running job |

The MCP tools (`query_submit`/`query_status`/`query_result`/`query_cancel`) share the `/api/jobs` registry.

Finished jobs are kept for `SQLHANDLER_ASYNC_JOB_TTL` seconds (default 900), up to 100 tracked jobs; the registry refuses with HTTP 429 when full of running jobs.

Jobs, the synchronous query path, and the query timeout all run on the **same `QueryJob` primitive** (own thread, DuckDB `con.interrupt()` for cancellation) — one implementation serving the MCP tools, the JSON API, and the async registry, so cancel/timeout semantics cannot drift between surfaces.

### Profile & export endpoints
- `POST /api/profile` — column statistics (min/max, null %, distinct, quartiles).
- `POST /api/export` — download a query or table as **CSV or Parquet** (attachment), capped by `SQLHANDLER_EXPORT_MAX_ROWS` (default 100 000; 0 = hard 1M ceiling).

### UI features
- Searchable table list with format badges and `source/schema/name` labels in federated mode; per-table schema view.
- **Column stats panel** — lazy "Profile table" button per table.
- SQL editor (Ctrl+Enter) with row limit; results table with timing and copy-CSV.
- **Export buttons** — CSV and Parquet download of the current query.
- **Charts** — one-click inline SVG bar chart of the first numeric column (dependency-free).
- **Saved queries + history** — per-browser (localStorage): save/reload/delete queries; history keeps the last 30 successful runs.
- **Inspector tab** — a browser-side MCP inspector: the same 18 tools/schemas an MCP client's `tools/list` sees, an auto-generated arguments form (the selected table auto-fills the `table` argument), and MCP-shaped results (`{content, isError}`, Raw toggle) dispatched through the server's own tools/call path (`GET/POST /api/inspector/*` — the `/api` auth surface). See [MCP.md §6](MCP.md).
- Light/dark theme (OS-aware, persisted) with HPE branding.
- **Read-only guarantee** — every statement is parsed with DuckDB's own grammar; only plain `SELECT` (plus `EXPLAIN SELECT`) is accepted. Writes, `PRAGMA`/`SET`, and `COPY` are rejected. Results are clamped to `SQLHANDLER_MAX_ROWS` (default 1000).

---

## 3b. Per-caller identity + policy-as-code (governed masking)

The engine and every MCP/UI surface resolve **one Caller per request** (see the observability section for the ladder) and — when policy enforcement is on — enforce an operator-authored policy file against it:

```json
{
  "version": 1,
  "default_group": "restricted",
  "groups": {
    "restricted": {
      "tables": {
        "workorder/*": {"row_filter": "kind != 'secret'", "column_masks": {"ssn": "redact", "email": "hash"}}
      },
      "hidden_tables": ["scratch/*"]
    }
  },
  "subjects": {"alice": ["restricted"]},
  "key_fps": {"sha256:2689367b205c": ["restricted"]}
}
```

- **Masks** — `redact` (constant `***`), `hash`/`sha256[:n]`/`md5` (hex digest of the value — not reversible), or any other string = a SQL literal constant the operator wrote. Masked columns are OMITTED from list/describe/search output and refused honestly by `profile_table`/`column_stats`/`sample_rows`.
- **Row filters** — SQL boolean expressions AND-composed across a caller's groups, validated against each table's REAL columns with DuckDB's own binder at load (a filter naming a nonexistent column refuses the FILE — never a silent half-application).
- **Hidden tables** — invisible: not listed, not searchable, describe/profile/scan refuse as not-found; a query naming one directly returns an EMPTY relation (no existence oracle).
- **Enforcement point** — per-request DuckDB views (`_register_schema`): covered physical tables register a masking VIEW (base dataset under a private name) instead of the raw dataset; virtual-table definitions compose over those views, so masking is inherited TRANSITIVELY; materializations of masked virtuals land on separate `-p<hash8>-` parquet artifacts.
- **The non-negotiable** — every cache key (result L1/L2, virtual materialization, describe, profile, column stats) appends `policy=<sha256-of-the-caller's-effective-rules>` ONLY when non-empty: masked and unmasked callers NEVER share an entry, while two identically-restricted callers still share (the hash is over the resolved rules, not the identity). With `SQLHANDLER_POLICY_ENABLED=0` (default) the hash is empty everywhere and the entire feature is byte-identical to the pre-policy behavior.
- **Scope stores** — with enforcement ON, the `sqlhandler://query-memory` resource and the saved-query store are OWNER-scoped (a saved SQL template or recorded query can name tables a policy hides); enforcement off keeps them shared byte-identically.
- **Hot reload** — mtime-polled like the semantic catalog; a file edit changes every affected caller's hash, so old cache entries age out via TTL (no purge needed).

## 4. Observability & ops

- **Prometheus metrics** — `GET /metrics` renders the text exposition (0.0.4) with no extra dependency: `sqlhandler_queries_total{outcome}` (ok/error/timeout/cancelled), `sqlhandler_query_duration_seconds` histogram, `sqlhandler_query_rows_total`, `sqlhandler_cache_{hits,misses}_total{cache}` (describe/profile/dataset), and gauges for table count, process RSS, and the container memory limit.
- **Audit log** — `SQLHANDLER_AUDIT_LOG=/path/audit.jsonl` appends one JSON line per query outcome (`ts`, `event`, `sql`, `state`, `duration_ms`, `n_rows`, `error`) — compliance-grade, SIEM-friendly. Best-effort writes never break a query. Chart value `query.auditLog` wires the env and mounts a pod-local writable dir for the path; durable audit = point it at your own PVC-backed mount.
- **API token** — `SQLHANDLER_API_TOKEN` requires `Authorization: Bearer` or `X-API-Token` (constant-time compared) on every `/api/*` request, for deployments not already behind the oauth2-proxy gateway. `/mcp`, `/ui`, `/health` and `/ready` are unaffected (`/metrics` too, unless `SQLHANDLER_METRICS_AUTH=1` — and `/ready` stays open even then: kubelet readiness probes cannot carry a secret).
- **Optional `/mcp` API-key gate** — `SQLHANDLER_API_KEYS` (or the fleet-universal `MCP_API_KEYS`): when either is set, every `/mcp` request needs `X-API-Key` or `Authorization: Bearer` (constant-time compared); unset → `/mcp` runs open exactly as before. Env re-read per request, so rotation needs no restart.
- **Resilience (carried from 0.8.0)** — cgroup-proportional DuckDB memory budgets with spill-to-disk, disk-warm metadata cache, and health/readiness/startup probes.
- **Caller identity (additive)** — every HTTP request resolves one Caller through a trust ladder: (1) the gateway's relay attribution headers (`X-MCP-Caller-Subject` + `X-MCP-Caller-Class`) ONLY over a key-valid request; (2) oauth2-proxy browser headers ONLY when `SQLHANDLER_TRUST_BROWSER_HEADERS` is set (the operator's AuthorizationPolicy assertion); (3) the matched key's fingerprint (`sha256:<12hex>` — never the key); (4) anonymous. The audit log's query records gain an additive `caller: {class, subject, key_fp}` field (absent when no caller is bound), and `/metrics` adds `sqlhandler_caller_queries_total{caller_class}` (bounded labels: user|browser|key|anonymous — existing series render byte-identically). Stdio transport and engine-internal paths are anonymous (no identity context).

### Deployment note (chart coverage)

The semantic-catalog file is chart-configurable (`semanticCatalog.*` values — ConfigMap + read-only mount + `SQLHANDLER_CATALOG`, validated + hot-reloaded; see README "Semantic catalog") — and, new in 1.4.0, catalogs can also be **uploaded as JSON or YAML straight from the web UI / `POST /api/semantic-catalog`** (engine accepts both formats; `pyyaml` is now a base dependency), with an optional
shared PVC store (`semanticCatalog.store.enabled`) making uploads durable and cross-pod. The chart now covers the ops knobs too: the Prometheus scrape is a first-class value (`metrics.podMonitor.*` — enable, label for the operator's `podMonitorSelector`, and supply a `bearerTokenSecret` when `security.metricsAuth` is on), the concurrency gate is chart-wired (`query.maxConcurrentQueries` / `query.queueTimeoutSeconds` — default render equals the app defaults), and the audit log path is chart-wired too (`query.auditLog`; the chart mounts a pod-local writable dir for the path — point it at a PVC-backed mount when it must survive rescheduling). An image-only rollout is **default-safe**: timeout, audit, and token off;
`S3_FORMAT=auto`; export cap 100k; the one new *active* default is the concurrency cap of 8).

---

## 5. Performance (measured on G2, 2026-09-12)

Measured with the repo's own harness (`bench/ezpresto_vs_sqlhandler.py`,
stdlib-only, persistent-connection transport with per-thread pooling) against
the G2 deployment — v1.6.0, 4 replicas × 8 vCPU/16Gi, MinIO-backed workload of
10 queries from 50K to 3M rows. Full methodology, tables, and the in-cluster
variant: [`../bench/BENCHMARK.md`](../bench/BENCHMARK.md), with the scale-out
(HPA) campaign summarized in [BENCHMARKS.md](BENCHMARKS.md).

- **Query result cache** (new) — identical queries served from memory, keyed
  by sql/params/limits + base-snapshot versions (ETL commits invalidate
  instantly; TTL backstop). Measured speedup **1.8×–21× over cold execution**,
  scaling with query cost: counts/filters 1.8–2.5×, aggregations
  **6.9–19.2×**. The Snowflake result-cache analog, and the honest
  agent-facing experience — agents retry, loop, and re-ask.
- **Disk block cache for object stores** (opt-in, chart keys
  `cache.blockCache.*`) — parquet footers and column chunks are fetched once
  into pod-local disk and re-served on every later read. Covers pyarrow
  filesystems on the s3 + iceberg backends; the OneLake/Delta path routes
  its data files through it with snapshot-version-scoped keys (a 1.6.x
  addition — delta-rs's own client bypassed the cache before; Delta-log IO
  stays inside delta-rs). The nfs backend's delta tables keep delta-rs's
  internal reader. `cache.blockCache.includeLocal` opts NFS mounts in
  (pyarrow sees them as LocalFileSystem). Wins repeat/filtered/preview-style
  scans — the agent-shaped access pattern.
- **Shared L2 result cache** (opt-in, chart keys `cache.l2.*`) — the layer
  behind the memory result cache: entries publish as zstd parquet + JSON
  sidecars to a directory every replica reads (`cache.l2.dir` on an RWX PVC),
  so a warm query on one replica serves the others. Inert until `dir` is set
  (the engine refuses to default it to pod-local /tmp); the
  `minBytes`/`maxBytes` band keeps tiny (recompute is cheaper than a PVC
  round-trip) and huge (volume-fill) results out.
- **Metadata cache tuning** — `cache.ttl` (list/describe retention),
  `cache.listAsyncRefresh` (serve cached listings immediately and refresh in
  the background so callers never block on a slow object-store list), and
  `cache.spillDir` (DuckDB's spill directory when a query would otherwise
  push RSS past the pod limit and get OOMKilled — which wipes every
  in-process cache).
- **Virtual-table materialization cache** (new) — a virtual table's full
  result is written to parquet once and reused across queries and replicas
  (`cache.virtualCacheDir` on a shared PVC = one materialization per
  deployment per data change; `cache.virtualCacheTtl` bounds reuse — the key
  carries base-snapshot versions so ETL commits invalidate instantly;
  `cache.virtualCacheSort` clusters materialized results by their
  lowest-cardinality columns so row-group statistics prune filtered reads).
  Closes the multi-second cost of unfiltered
  previews on definitions with blocking aggregations (LIMIT cannot
  short-circuit a DISTINCT/GROUP BY pipeline).
- **count(\*) metadata fast-path** (new) — bare `SELECT COUNT(*) FROM t`
  reads parquet/Delta metadata instead of scanning: ~125 ms → ~0.1 ms on a
  20M-row table; visible in-cluster as 3M-row counts in ~23 ms.
- **Bare-LIMIT preview fast path** (new, chart key `query.previewFastpath`,
  default on) — a `SELECT cols FROM t LIMIT n` (no WHERE/GROUP/JOIN/ORDER
  BY/aggregate) reads the FIRST row group of the table's first data file
  and slices to the limit, instead of paying pyarrow's full fragment
  enumeration before the limit short-circuits. Same detector philosophy as
  the count fast-path (regex + resolve; anything else — virtual, raw,
  external-attach, params, time travel, explicit `limit` args — falls to
  the normal query path, which is also the fallback on ANY fast-path
  failure). LIMIT without ORDER BY guarantees no row subset, so the first
  physical rows are the same SQL contract; rendering, row caps
  (`SQLHANDLER_MAX_ROWS`) and audit/metrics are identical.
- **Data prewarm** (new, chart key `cache.prewarmRowgroups`, default 1, 0 =
  off) — the startup prewarm (explicit `cache.prewarmTables` or the
  usage-driven list) also reads the FIRST N row groups of each table
  through the disk block cache (`cache.blockCache.enabled`) via the same
  wrapped-filesystem read path later queries take, so the cold first query
  of the day hits warm pod-local blocks. Local/NFS backends are skipped
  (the OS page cache does that job — the block cache's own includeLocal
  semantics); per-table outcomes (`data-ok` / `data-error` /
  data-skipped-no-cache) never fail startup.
- **Cold engine numbers** (result cache busted — pure DuckDB): 3M-row scans
  1.0–2.0 s p50 (4.3–5.1M rows/s counts/projections), 200–350K queries
  0.19–2.0 s. Warm floor ~104–120 ms through the gateway for every query size
  (cache hit + round-trip), ~18–25 ms in-cluster.
- **Concurrency**: warm queries scale to ~75–81 qps at L8 with flat p50;
  all-cold bursts saturate per-pod CPU (flat ~11–12 qps, p95 to 4.2 s) — the
  capacity-planning number for cold agent storms.
- **Clustered virtual materializations** (new) — materialized results are
  auto-sorted by their lowest-cardinality columns so parquet row-group
  statistics prune filtered reads (chart key `cache.virtualCacheSort`).

### Environment findings during the benchmark (upstream/ops, not sqlhandler)

Recorded with repro evidence in `bench/BENCHMARK.md` §"Environment findings":
the EzPresto locator routes `nextUri` GETs to its web app (404 "Query not
found", 10/10 repro), the EzPresto MCP pod flaps between MCP and health-echo
states (4 restarts, dev image), EzPresto's OPA policy denies table access for
non-platform principals (`AccessDeniedException`), and the ezaf-gateway tier
alternates MCP routes on a minutes cadence for external clients. In-cluster
traffic is unaffected on all counts.

---

## Feature → code → tests map

| Feature | Module(s) | Tests |
|---|---|---|
| `profile_table` / `search_tables` tools, params, time travel, timeout, concurrency gate, query memory | `engine.py`, `server.py` | `test_engine.py` |
| `column_stats` tool, fuzzy `search_tables` ranking | `engine.py`, `server.py` | `test_column_stats.py`, `test_search_fuzzy.py` |
| `sample_rows` tool (head + fill rates) | `engine.py`, `server.py` | `test_sample_rows.py` |
| `explain_query` tool (metadata cost estimate, warm/cold band, JSON plan summary, no-execution) | `engine.py`, `server.py` | `test_explain_query.py` |
| `ask_data` tool (search→describe→profile→draft, never executes) | `server.py`, `engine.py` | `test_ask_data_mcp.py` |
| Structured error codes + fix hints | `errors.py`, `engine.py`, `server.py` | `test_structured_errors.py`, `test_agent_pack_mcp.py` |
| Async MCP query jobs (`query_submit`/`status`/`result`/`cancel`, `/api/jobs/*`) | `jobs.py`, `engine.py`, `server.py`, `webui.py` | `test_query_jobs.py` |
| Saved parameterized queries (store, bind params, auth gating) | `saved.py`, `server.py`, `webui.py` | `test_saved_queries.py` |
| MCP resources + prompts | `mcp_resources.py`, `server.py` | `test_mcp_resources.py` |
| Semantic catalog merge/hot-reload | `engine.py` | `test_engine.py` |
| Output formats (markdown/json/csv/arrow) | `engine.py`, `webui.py` | `test_tools_output.py` |
| Async query jobs (submit/poll/rows/cancel) | `webui.py`, `engine.py` | `test_async_query.py`, `test_webui.py` |
| `/api/profile`, `/api/export` | `webui.py` | `test_webui.py` |
| Delta on S3 (`S3_FORMAT`) | `s3.py`, `config.py` | `test_s3_delta.py` |
| Raw-format landing zone (csv/tsv/json discovery, size cap, RAW badge, count(*) fast-path gate) | `rawfiles.py`, `s3.py`, `file.py`, `config.py`, `engine.py`, `server.py` | `test_raw_formats.py` |
| Metrics, audit log, API token | `observability.py`, `server.py` | `test_ops.py` |
| Write tier (scratch CTAS/INSERT/COPY, global flag default off, subject-scoped, single-writer lease, delta/pyiceberg backends, never-cached) | `writes.py`, `engine.py`, `server.py` | `test_writes.py` |
| UI: stats panel, charts, export, saved queries | `ui/index.html` | `test_webui.py` |
| Inspector tab (MCP tool bridge: `/api/inspector/tools` + `/api/inspector/call`, shared dispatcher) | `webui.py`, `server.py` | `test_inspector_ui.py` |

All tests in the files above pass on the current tree (run with `pytest tests/ -p no:cacheprovider`; the optional s3/iceberg integration files need their documented local setup — see their module docstrings).

## Key configuration knobs

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_CATALOG` | — | Semantic catalog JSON file (hot-reloaded) |
| `SQLHANDLER_QUERY_MEMORY_SIZE` | 50 | Query-memory ring size behind `sqlhandler://query-memory` |
| `SQLHANDLER_PROFILE_MAX_ROWS` | 1000000 | Sample cap for profiling (0 = full table) |
| `SQLHANDLER_MCP_READONLY` | `1` | MCP `run_sql` is SELECT-only (decision D2); `0` restores multi-statement/DDL for trusted callers. Attached external catalogs stay read-only in both modes |
| `SQLHANDLER_WRITES_ENABLED` / `SQLHANDLER_WRITE_SCRATCH_ROOTS` / `SQLHANDLER_WRITE_LEASE_TTL` | off / — / 300 | **Write tier** (review §4 — GLOBAL FLAG, DEFAULT FALSE): when enabled, `run_sql` admits ONE classified scratch-write statement (`CREATE TABLE AS` / `INSERT INTO ... SELECT` / `COPY ... TO`) whose target resolves under `<scratch-root>/<subject-slug>/...` (subjects from the identity ladder; anonymous and key-fingerprint callers get NO write capability). Writes execute OUTSIDE DuckDB — delta-rs `write_deltalake` (default) or pyiceberg (an `iceberg://`-named root) — so the fs lockdown and the READ_ONLY attach posture are untouched (source-never-sink holds for reads inside the write too). Single-writer discipline: in-process lock + advisory lease file (O_EXCL + TTL) on the scratch volume; contention is a retryable `E_WRITE_CONFLICT`. Writes are never cached and never served from cache (classification precedes the cache check; a write also evicts cached reads of its target). A write SUMMARY (target, backend, rows) returns in place of rows. Policy interaction: masking applies to the READ inside the write — scratch receives exactly what the caller could read, never more; write targets are always outside covered source tables. Audit gains `event:"write"` lines + `sqlhandler_writes_total{backend,outcome}` (both additive) |
| `SQLHANDLER_ALLOWED_ORIGINS` | none (same-origin) | Extra browser origins allowed on `/api/*`, `/ui`, `/mcp` (CORS + Origin validation, decision D3) |
| `mcp.compression.enabled` / `mcp.compression.minSize` (chart → `SQLHANDLER_COMPRESSION` / `SQLHANDLER_COMPRESSION_MIN_SIZE`) | gzip / 1024 | **HTTP response compression** (GZipMiddleware, innermost in the middleware stack — auth/guard/CORS stay outside it). Agent-facing tool results are markdown/JSON-heavy text, so gzip compresses them 5-10x. `off` removes the middleware entirely. Bodies under `minSize` bytes (e.g. `/health`) pass through uncompressed; SSE (`text/event-stream`) is excluded by the middleware and always streams uncompressed; plain chunked streams compress chunk-wise (Z_SYNC_FLUSH per chunk, verified incremental in `test_compression.py`); `/metrics` compresses fine. Unknown values fall back to `gzip`, never silently off |
| `SQLHANDLER_METRICS_AUTH` | off | `1` gates `/metrics` behind the API token / MCP API keys (default off = today's behavior); `/ready` is NEVER gated — kubelet readiness probes cannot authenticate (gating it strands every pod NotReady, live-seen) |
| `SQLHANDLER_QUERY_TIMEOUT` | 600 (decision D5) | DuckDB interrupt after N seconds (0 = off) |
| `SQLHANDLER_MAX_CONCURRENT_QUERIES` (chart: `query.maxConcurrentQueries`) | 8 | Per-pod concurrency cap (0 = unlimited) |
| `SQLHANDLER_QUEUE_TIMEOUT` (chart: `query.queueTimeoutSeconds`) | 30 | Seconds a query may wait for a slot |
| `SQLHANDLER_ASYNC_JOB_TTL` | 900 | Seconds finished async jobs are kept (shared by the web registry and the MCP/jobs registry) |
| `SQLHANDLER_MAX_JOBS` | 8 | Cap on tracked async query jobs (`query_submit` + `/api/jobs`); beyond it submits are refused; garbage/non-positive values fall back to the default |
| `SQLHANDLER_SAVED_QUERIES_PATH` | next to the cache dir | JSON file for the saved-parameterized-queries store (`query_save`/`query_list`/`query_delete`/`query_saved`; writes are auth-gated when a credential env is configured) |
| `SQLHANDLER_EXPORT_MAX_ROWS` | 100000 | Row cap for CSV/Parquet exports (0 = 1M ceiling) |
| `SQLHANDLER_AUDIT_LOG` (chart: `query.auditLog`) | — | JSONL audit file path |
| `SQLHANDLER_API_TOKEN` | — | Bearer/X-API-Token gate for `/api/*` |
| `S3_FORMAT` | auto | `auto` \| `parquet` \| `delta` for the s3 backend |
| `iceberg.catalogType` / `iceberg.catalogUri` / `iceberg.nessieRef` (chart → `ICEBERG_CATALOG_TYPE` / `ICEBERG_CATALOG_URI` / `ICEBERG_NESSIE_REF`) | rest / — / — | Iceberg backend catalog selection (values-first): `rest` (default; Databricks Unity Catalog preset: `catalogUri: https://<workspace-host>/api/2.1/unity-catalog/iceberg` + `warehouse: <uc-catalog>` + token via `iceberg.credentialsSecret` → `ICEBERG_CATALOG_TOKEN`), `sql`, `glue` (no URI — AWS_REGION/AWS_* env or IRSA, never rendered values), `hive` (`catalogUri: thrift://hms:9083`), `nessie` (REST URI; `nessieRef` pins the branch/tag, empty = catalog default). Unknown `ICEBERG_CATALOG_TYPE` values fall back to `rest` |
| `sharing.enabled` + `sharing.existingConfigMap` / `sharing.existingSecret` (chart → `SQLHANDLER_SHARING_PROFILE` / `DELTA_SHARING_*`) | — | Delta Sharing backend (`backend: sharing`): profile file via an operator-owned ConfigMap (mounted, standard open-protocol YAML/JSON: `endpoint` + `bearerToken`) or a Secret carrying endpoint + bearer-token keys wired via `secretKeyRef` — the token never renders into the ConfigMap or values, and never appears in tool output or errors (scrubbed at raise). Env names are what the server reads; operators set the values |
| `adls.enabled` + `adls.account` / `adls.filesystem` / `adls.prefix` / `adls.auth` / `adls.existingSecret` (chart → `SQLHANDLER_BACKEND=adls` / `ADLS_ACCOUNT` / `ADLS_CONTAINER` / `ADLS_PREFIX` / `ADLS_AUTH` / `ADLS_CLIENT_SECRET_ENV`) | false / — | ADLS Gen2 backend (`backend: adls`; render-guarded — enabling `adls.enabled` without `backend: adls` fails the render): Parquet + Delta discovery with the same folder conventions as s3, raw landing zone included. Auth `anon` (default; public container) or `client-secret` — the client secret itself NEVER renders: the ConfigMap carries only `ADLS_CLIENT_SECRET_ENV` (the env-var NAME) and deployment.yaml wires `ADLS_CLIENT_SECRET` from the operator's Secret (`adls.existingSecret` / `adls.existingSecretClientSecretKey`) via `secretKeyRef`. `adls.endpointSuffix` covers sovereign clouds (`core.usgovcloudapi.net`, `core.chinacloudapi.cn`) |
| `gcs.enabled` + `gcs.bucket` / `gcs.prefix` / `gcs.credentialsFile` / `gcs.anonymous` (chart → `SQLHANDLER_BACKEND=gcs` / `GCS_BUCKET` / `GCS_PREFIX` / `GCS_CREDENTIALS_FILE` / `GCS_ANONYMOUS`) | false / — | GCS backend (`backend: gcs`; render-guarded like adls): Parquet + Delta discovery with the same folder conventions as s3, raw landing zone included. `gcs.credentialsFile` is the MOUNTED path of a service-account JSON key file (the operator supplies the volume; key contents never appear in values/ConfigMap) — empty means ambient application-default credentials; `gcs.anonymous: true` reads a public bucket with no credential |
| `rawFiles.enabled` / `rawFiles.maxFileMB` (chart → `SQLHANDLER_RAW_FORMATS` / `SQLHANDLER_RAW_MAX_FILE_MB`) | true / 64 | Raw-text landing-zone discovery on the s3 + nfs/file backends: `.csv`/`.tsv`/`.json`/`.ndjson`/`.jsonl` (+ `.gz`) files become queryable tables under the same folder conventions as Parquet (see Backends & data sources above). `false` hides every raw table; the MB cap (0 = unlimited) skips whole tables containing any larger file (compressed size for `.gz` — approximate) with one log line each. Landing-zone feature by design: no row groups/statistics, whole-file scans, count(*) takes the query path — promote large raw data to Parquet |
| `SQLHANDLER_ATTACH` / `SQLHANDLER_ATTACH_FILE` | — | Read-only external-database attach config (JSON; `password_env` names only) — `postgres` \| `mariadb` \| `mysql` \| `sqlite` \| `sqlserver` \| `ducklake` (catalog connect string + optional `data_path`) \| `mongodb` (host + required `database` scope; extension opt-in at build time) \| `bigquery` (project/dataset scope + optional `billing_project`, ADC auth or a temporary access token via `password_env`; extension opt-in at build time); per-entry `params` for driver options (TLS, non-ducklake/non-bigquery types). `clickhouse` / standalone `motherduck` are not types (no 1.5.5 clickhouse ATTACH extension; MotherDuck rides a ducklake `md:` catalog) |
| `SQLHANDLER_BLOCK_CACHE` / `_DIR` / `_BLOCK_SIZE` / `_MAX_BYTES` / `_INCLUDE_LOCAL` | off | Disk block cache for object-store parquet reads (s3 / iceberg / onelake backends; OneLake Delta data files are read through it snapshot-version-scoped, so a new ETL commit or a time-travel read never serves another snapshot's cached bytes) |
| `SQLHANDLER_PREVIEW_FASTPATH` (chart `query.previewFastpath`) / `SQLHANDLER_PREWARM_ROWGROUPS` (chart `cache.prewarmRowgroups`) | on / 1 | **Bare-LIMIT preview fast path** — `SELECT cols FROM t LIMIT n` (no WHERE/GROUP/ORDER/aggregate/params/time travel) reads the first data file's first row group and slices to the limit instead of paying full fragment enumeration; identical rendering/row-caps/audit, normal path on any failure. **Data prewarm depth** — the startup prewarm also reads the first N row groups of each prewarm table through the disk block cache (needs `SQLHANDLER_BLOCK_CACHE=1`; local/NFS skipped); 0 = describe-only prewarm |
| `SQLHANDLER_RESULT_CACHE_TTL` / `_MAX_BYTES` | 3600 / 256MiB | In-memory result cache for identical queries (snapshot-version-keyed) |
| `SQLHANDLER_VIRTUAL_CACHE_TTL` / `_DIR` / `_MAX_BYTES` / `_SORT` | 3600 / cacheDir / 2GiB / on | Virtual-table materialization cache (point `_DIR` at an RWX PVC to share across replicas) |
| `SQLHANDLER_L2_DIR` / `_ENABLED` / `_TTL` / `_MIN_BYTES` / `_MAX_BYTES` | — / on / 3600 / 256KiB / 2GiB | **Shared L2 result cache** behind the memory LRU: results published as zstd parquet + JSON sidecar to a directory every replica reads (RWX PVC) — one replica's warm query serves all. Inert until `_DIR` is set; band caps keep small (recompute is cheaper than the PVC round-trip) and huge (volume-fill) results out; keys carry base-snapshot versions + a conditional `policy=` part (empty = byte-identical to the pre-L2 key format) |
| `SQLHANDLER_TRUST_BROWSER_HEADERS` | off | **Identity spine, browser rung**: when truthy, oauth2-proxy identity headers (`X-Auth-Request-User`, `X-Forwarded-Groups`) resolve the Caller on keyless requests (the UI/API path). TRUST-GATED: set it ONLY when the workload AuthorizationPolicy pins ingress to the gateway (chart: `security.identity.trustBrowserHeaders`) — headers are forgeable on any pod reachable without that pin |
| `SQLHANDLER_POLICY_FILE` / `SQLHANDLER_POLICY_ENABLED` | — / off | **Policy-as-code** (per-caller masking): an operator-authored JSON/YAML file of `groups → {tables glob → {row_filter, column_masks, hidden_tables}}` + `subjects`/`key_fps` bindings + `default_group`. Hot-reloaded on mtime (a broken edit keeps the previous file enforcing — fail-closed). Enforcement ON: masking views per request in DuckDB (row filter + `redact`/`hash`/const column masks), hidden tables invisible on list/search/describe/profile/scan, covered-table `scan_table` delegates to the SQL path (pyarrow filters refused), describe/profile/column_stats compute over the masked view with the caller's policy hash folded into their cache keys, virtual tables mask TRANSITIVELY (definitions compose over masked base views) and materialize to separate `-p<hash8>-` artifacts, query memory + saved queries become owner-scoped. Every cache key (L1/L2/materialization/describe/profile) carries `policy=<hash>` ONLY when non-empty — with enforcement off everything is byte-identical to the pre-policy behavior |

### Scale-out deployment (chart keys)

`autoscaling` (HPA, autoscaling/v2, needs metrics-server), `podDisruptionBudget`,
`topologySpread`, `terminationGracePeriodSeconds`, and
`semanticCatalog.store` (shared RWX PVC for uploads + virtual-table
materializations) — the G2 benchmark showed a single replica collapses under
concurrency while 4 replicas hold flat throughput; the HPA's CPU target is
burst/runaway protection, not load-following. Numbers:
[BENCHMARKS.md](BENCHMARKS.md).
