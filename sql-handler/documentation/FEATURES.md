# SQLhandler — features and the thinking behind them

SQLhandler is an **MCP server**: a Model Context Protocol endpoint that exposes
tools an LLM agent can call. Its tools are SQL and schema discovery, and they
answer from columnar lake data **in place** — no copy, no load step, no second
database. This document is the feature reference for a reader who has never
seen the package: what each capability does, and the design decision that made
it that way.

Deployed as the `sqlhandler` Helm chart **4.0.0** (image `v4.0.0`) on HPE
Private Cloud AI (PCAI / Ezmeral Unified Analytics). Endpoint, tool schemas and
client setup: [MCP.md](MCP.md). Every knob: [CONFIGURATION.md](CONFIGURATION.md).
Deployment: [DEPLOYMENT.md](DEPLOYMENT.md).

---

## 1. What it is, in one paragraph

An agent asks a question in SQL; SQLhandler plans it with **DuckDB** (an
in-process analytical SQL engine) over **pyarrow datasets** (Arrow's columnar
scan layer) pointed at the customer's existing storage — Microsoft Fabric
OneLake, an S3-compatible store, an Iceberg catalog, or a mounted directory.
Predicate and column-projection pushdown means only the bytes the query needs
leave the store. The same process serves the MCP endpoint, a read-only web
explorer, a JSON API, health probes and Prometheus metrics. It replaces
EzPresto/PrestoDB for the agent path: no JDBC round-trip, no row
re-serialisation, no full scan to filter.

## 2. Backends: read the lake where it lives

One engine, four backends, selected by the chart's `backend:` value. They share
the SQL engine, the caches, the tool surface and the backend-aware readiness
probe; only the `DataProvider` differs. **The philosophy is that the storage
layout is the customer's decision, not a migration project** — pointing a
provider at existing data is the whole product, and a new source is a subclass
plus one branch, not a new service.

| Backend | Reads | Credential | Pick it when |
|---|---|---|---|
| `onelake` | Microsoft Fabric OneLake — **Delta Lake** (Parquet data files plus a `_delta_log` transaction log) over ABFS | Entra service principal | The data already lives in Fabric; Delta gives you transactions and time travel for free |
| `s3` / `minio` | Parquet objects (plain or Hive-partitioned), plus Delta tables detected by `_delta_log` | S3 access/secret key, or anonymous | The data is Parquet in object storage, on-prem MinIO or AWS, or any tool can drop files there |
| `iceberg` | **Apache Iceberg** tables through a REST or SQL catalog, Parquet data files | REST token and/or S3 keys | A real lakehouse catalog already owns the metadata — schemas, snapshots, concurrent writers, cross-engine reads |
| `nfs` / `file` / `local` | A mounted directory — Delta **and** Parquet | none | The data is on a filer or a PVC, or you are proving the engine locally |

The image also accepts `adls`, `gcs` and `sharing` (Delta Sharing) as backend
values, but the chart exposes only the four above — see
[CONFIGURATION.md §9](CONFIGURATION.md#9-what-the-chart-does-not-wire).

Two composition mechanisms sit on top:

- **Federated multi-source** (`sources:` / `SQLHANDLER_SOURCES`) — several
  buckets or sources of any backend mix behind **one endpoint**, with
  cross-source `JOIN`s in a single `run_sql`. Every table is source-qualified
  (`sales_orders`, `inventory_raw_customers`); a bare name resolves only when it
  is unique across all sources, and an ambiguous bare name is deliberately **not**
  registered, so it fails loudly instead of silently reading the wrong source.
- **External databases, read-only** (`databases:` / `SQLHANDLER_ATTACH`) —
  Postgres/MySQL systems of record become `<alias>.<schema>.<table>` and join
  with lake tables in the same query. Read-only is enforced by DuckDB itself
  (`ATTACH … READ_ONLY`), not by convention, and the config carries env-var
  *names* for passwords, never values. A database that is down does not fail the
  lake: `list_tables` reports it unavailable and lake-only queries never attach.

### How tables are discovered

- **S3 / Iceberg / NFS Parquet:** every Parquet file or folder of Parquet files
  becomes a table. `<prefix>/orders.parquet` → `orders`;
  `<prefix>/sales/customers/*.parquet` → schema `sales`, table `customers`,
  addressable as `customers` or `sales_customers`. Hive partition folders
  (`key=value`) fold into their table; hidden (`.`) paths are skipped. On S3,
  `S3_FORMAT=auto` (the default) treats any folder carrying a `_delta_log` as a
  Delta table and everything else as plain Parquet, so one bucket can mix both.
- **OneLake:** discovery uses the DFS REST API (`?resource=filesystem`) because
  OneLake does not implement the ADLS Gen2 *blob* list API that pyarrow's Azure
  filesystem uses — that listing fails with `501 Not Implemented`. Tables live
  under `Tables/<schema>/<table>`.
- **NFS:** both formats auto-detected; Parquet files inside a Delta folder are
  folded into that Delta table, never exposed twice.

## 3. The query engine, and its safety posture

The engine is one `SqlEngine` per process, shared by the MCP tools and the web
API, so a cache or a guard cannot drift between surfaces. Its capabilities:

- **Parameterized queries.** `run_sql` takes bind `params` — an object for
  named `$placeholders`, an array for positional `?`. Values never enter the
  SQL text, so a reusable template stays injection-safe.
- **Time travel.** `version_as_of` reads a historical snapshot: a Delta
  snapshot version (nfs / onelake / Delta-on-S3) or an Iceberg snapshot id. It
  applies to every versionable table in the query; a plain-Parquet table in the
  same query is a clear error. Historical datasets are cached per version — a
  snapshot never changes.
- **Output formats.** markdown (default, agent-readable), JSON, CSV, and Arrow
  IPC (base64) for exact dtypes.
- **Count fast path.** A bare `SELECT COUNT(*) FROM t` reads Parquet/Delta
  metadata instead of scanning: ~125 ms → ~0.1 ms on a 20M-row table.

Four guards bound what a query can do. Each exists because the failure it
prevents was observed, and each fails **closed**:

| Guard | What it stops | Why it is designed this way |
|---|---|---|
| **Read-only parser guard** (decision D2) | DDL/DML, `ATTACH`, `COPY`, `PRAGMA`/`SET`, multi-statement scripts on `run_sql` | The MCP surface is one prompt-injection away from a destructive statement. Statements are parsed with DuckDB's own grammar, not a regex. Attached external catalogs stay SELECT-only in **both** modes, enforced on the connection itself, so `INSERT INTO sink SELECT * FROM attached` cannot exfiltrate even with the guard off |
| **DuckDB filesystem lockdown** | `read_csv('/etc/passwd')`, `COPY … TO`, URL fetches | All legitimate object-store IO is done by pyarrow *outside* DuckDB, so nothing is lost. If the engine cannot disable local filesystem access on a connection it **refuses the query** rather than silently degrading (`SQLHANDLER_ALLOW_LOCAL_FS` is the explicit escape hatch) |
| **Timeout** (decision D5) | A runaway query holding a concurrency slot forever | 600 s default, enforced by interrupting inside DuckDB — no leaked threads — and by a watchdog for async jobs, which fires even when nobody is polling |
| **Concurrency gate + queue timeout** | The container being buried by parallel queries | 8 concurrent queries per pod, excess queues up to 30 s and then fails with a clean error instead of piling up |

**Per-caller fairness** (`limits.py`) is the second, identity-keyed layer on top
of the process-global caps. The global cap is the OUTER bound, not the whole
story: before this layer, one agent loop could take every concurrency slot and
every job slot on the pod. The design rules are deliberate:

- The caller identity is resolved **once**, by the identity layer, and travels
  explicitly into the engine and the job registry — nothing re-parses a request
  or trusts a header.
- **No identity = the global bucket**, so stdio, dev and unauthenticated
  deployments behave byte-for-byte as before.
- The per-caller knobs **default to the global caps**: an existing deployment
  keeps its behaviour until an operator opts into a fairness bound.
- Bucket labels are the bounded, non-secret scope vocabulary (subject, key
  fingerprint, `anonymous`, `other`) and are hard-capped, so a creative
  identity source cannot mint unbounded metric series or semaphores.

**Result budget** (`limits.py`) bounds one tool result in *bytes*, not just
rows. Over the ceiling (100 000 B ≈ 25k tokens by default) the result is cut at
a complete-row or complete-line boundary and ends with one deterministic
marker: total, returned, `offset`, `next_offset` and a resume recipe. **Rows are
never dropped silently** — the agent is always told what it did not get and how
to ask for it. `run_sql`, `scan_table` and `query_saved` accept `offset` to
page; `query_result` is fetch-once and reports the cap with no continuation.

## 4. Async query jobs — long queries without blocking the agent

An MCP tool call has a client-side timeout; a lake query can outlive it. The
`QueryJob` primitive (own thread, DuckDB `interrupt()` for cancellation) has
powered the web async API since 0.9; `jobs.py` exposes the *same* primitive to
MCP callers as `query_submit` / `query_status` / `query_result` /
`query_cancel`, plus REST twins under `/api/jobs/*`.

The philosophy is **one implementation, so semantics cannot drift**:

- **Same engine path.** A job *is* a `QueryJob`, so it honours the timeout, the
  row caps, the concurrency gate, the memory budget and the audit log.
- **The read-only guard runs at SUBMIT**, before any job exists — DDL is refused
  synchronously with the same error text `run_sql` produces, and nothing is
  started.
- **The timeout watchdog is armed at submit**, so a job that outlives the
  timeout is interrupted even if no one ever polls. A runaway job can never hold
  a gate slot forever.
- **Results are fetch-once.** `query_result` hands the spooled Arrow table over
  once and frees it; a second fetch is refused (HTTP 409) and `query_status`
  reports `"result_fetched": true`. Memory is bounded by design, not by luck.
- **The registry is bounded and in-memory.** At most `SQLHANDLER_MAX_JOBS`
  (8) tracked; beyond that a submit is refused with a clear message. A restart
  clears it — jobs are **not durable**, resubmit after a restart.
- **Owner-scoped under policy enforcement.** When policy is on and a submitter
  was resolved, the job records its owner and other callers get the same
  unknown-id 404 a bogus id gets — a wrong guess never confirms that a job
  exists.
- **Replica scope is the non-obvious one.** Each replica keeps its **own**
  registry. Submitting on one pod and polling on another is the classic
  "Unknown job id" outage on a scaled-out deployment (live-seen: a 4-replica
  release where three of four status calls 404'd). The fix is opt-in and
  additive: point `SQLHANDLER_JOBS_DIR` at a directory every replica can
  read-write (an RWX PVC) and finished jobs publish state and result there, so
  any replica can serve any job and the fetch-once claim holds cluster-wide via
  an atomic claim file. Unset, behaviour is exactly the historical one.

## 5. Agent ergonomics

Agents fail differently from humans: they do not scroll, they do not guess
twice, and they pay a full round-trip for every mistake. These features exist to
make the first attempt correct.

- **Discovery before SQL** — `list_tables`, `search_tables` (keyword search over
  table names, column names *and* catalog docs, ranked exact/substring then
  fuzzy, each hit carrying `matched_on`), `describe_table`, `sample_rows` (a
  bounded look at actual rows with per-column fill rates), `profile_table` and
  `column_stats` (min/max, approximate distinct, null %, quartiles, top values —
  over a bounded sample, never a full-table scan beyond the cap).
- **Plan without executing** — `explain_query` returns referenced tables with
  metadata row counts and bytes-to-scan, each labeled `exact` / `approx` /
  `none`, keyed to the snapshot version it describes, plus a warm/cold band
  (is this exact query already in the result cache?). `ask_data` turns a plain
  question into a drafted SELECT and a suggested follow-up, and **never
  executes** — execution is always a separate, explicit call.
- **Structured, self-correcting errors** (`errors.py`). An agent self-corrects
  far faster when an error carries a machine-parseable tail as well as the human
  message. With structured errors on (the default), every error message keeps
  its existing text **byte-identically** and gains one final line:
  `{"error": {"code": "E_TABLE_NOT_FOUND", "fix_hints": ["call list_tables", …]}}`.
  Codes are stable identifiers (`E_TABLE_NOT_FOUND`, `E_COLUMN_NOT_FOUND`,
  `E_READONLY`, `E_TIMEOUT`, `E_CONCURRENCY_GATE`, `E_ROWS_CAPPED`,
  `E_PARAM_INVALID`), so logs are greppable and policy surfaces can reference
  them. Three rules keep it boring and safe: the human message stays primary and
  unchanged, an unrecognised error gets **no** tail rather than a guessed code,
  and a caller can pin a code explicitly. Fix hints name the *actual* knob —
  a per-caller refusal points at the per-caller limit, not the global one.
- **Did-you-mean** — a typo'd table name comes back with the nearest real names,
  so the agent fixes it in one round-trip instead of re-discovering.
- **Query memory** — the last 50 outcomes are exposed as the
  `sqlhandler://query-memory` MCP resource, so a later session reuses proven SQL
  instead of rediscovering it.
- **Usage-driven prewarm** — with no explicit prewarm list, the busiest tables
  of the previous run (usage counts persisted with the disk-warm cache) are
  warmed on restart. The server teaches itself what to warm.
- **MCP primitives beyond tools** — resources `sqlhandler://catalog`,
  `sqlhandler://table/<name>/schema` and `sqlhandler://query-memory`; prompts
  `explore-data` and `analyze-table`; and explicit tool annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so a
  client that defaults pessimistically never presents a reader as destructive.
  Details: [MCP.md](MCP.md).

## 6. The semantic layer — telling the model what the data means

A schema alone is not meaning. A column called `woh_ord_typ` with values `a`/`b`
is a guess, and guesses cost round-trips. The semantic catalog is a
human-authored document merged into `list_tables`, `describe_table` and the MCP
resources:

- **Three attach routes, one document format.** In values (most auditable:
  native YAML under `semanticCatalog.tables`, rendered to a ConfigMap mounted
  read-only); an existing ConfigMap you own; or an **upload** from the web UI /
  `POST /api/semantic-catalog` (JSON or YAML), effective immediately.
- **Hot-reloaded on mtime**, so a catalog edit plus a re-apply updates the docs
  without a pod restart and without disturbing in-flight tool calls. A missing
  or broken catalog **never** breaks queries — it degrades to empty.
- **Virtual tables.** A catalog entry keyed by a clean bare identifier with a
  `definition` (a single read-only `SELECT`/`WITH`) becomes a queryable table:
  listed with a `VIRTUAL` badge, schema derived from the definition, filters
  still pushed down into the physical scans, and definitions compose. Nothing is
  stored in the lake and no DDL is issued — the semantic layer adds meaning and
  reuse without a write path.
- **Browser editing.** A global *Edit YAML…* editor (whole live catalog, JSON
  toggle) plus a per-table **Semantic** tab whose skeleton is prefilled from the
  real column list. Both write through the same validated upload store, so an
  edit never forks a duplicate entry.
- **dbt import.** `POST /api/semantic-catalog/import-dbt` seeds the catalog from
  a dbt manifest, so the docs a data team already wrote become agent context.

Spec, key forms and troubleshooting: [../docs/semantic-catalog.md](../docs/semantic-catalog.md).

## 7. Human access — the read-only data explorer

The same process serves a dependency-free web UI (one self-contained
`index.html`, no build step, no CDN) and a JSON API. It exists because
"the agent can see it" is not the same as "a human can check it", and debugging
an agent's SQL needs the same data through the same engine and caches.

- Searchable table list with format badges and `source/schema/name` labels;
  per-table schema view; lazy column-stats panel.
- SQL editor (Ctrl+Enter) with a row limit; results table with timing and
  copy-CSV; one-click inline SVG bar chart of the first numeric column.
- CSV/Parquet export of a query or a preview.
- Saved queries and history **per browser** (localStorage).
- **Inspector tab** — a browser-side MCP inspector that lists the same 23 tools
  and input schemas as `tools/list` (one source of truth, served from
  `/api/inspector/tools`) and dispatches calls through the server's own
  `tools/call` path, so identity, policy, caching and audit behave exactly as
  over `/mcp`.
- **Read-only guarantee** — every statement is parsed with DuckDB's grammar;
  only plain `SELECT` (plus `EXPLAIN SELECT`) is accepted, and results clamp to
  the row cap even when the server cap is unlimited, to keep the JSON payload
  bounded.

Endpoints: [MCP.md](MCP.md) and the API table in [../README.md](../README.md).

## 8. Identity, policy and the write tier

The package reads a whole data lake, so its access model is layered rather than
a single switch. All four layers resolve to **one** `Caller` object; no path
creates a second identity or a privilege shortcut.

| Layer | What it answers | Where |
|---|---|---|
| **API keys** (`MCP_API_KEYS`) | May this caller reach `/mcp` at all? | Mandatory in the chart: the key Secret must exist before the pod starts |
| **Gateway** (oauth2-proxy AuthorizationPolicy) | Is this a PCAI-authenticated caller? | Rendered by the chart at the ezaf gateway |
| **OIDC resource server** (`security.oidc`) | Who is this bearer token's subject? | Verifies a JWT against the issuer's JWKS — no client secret, no redirect URI |
| **Policy-as-code** (`security.policy`) | What may this caller *see*? | Per-caller row filters, column masks, hidden tables, dataset ACLs; hot-reloaded, and a present-but-invalid file fails **closed** |

Two consequences worth stating because they are easy to get wrong: every cache
key carries a hash of the caller's effective policy, so masked and unmasked
callers never share a cached result; and the policy file's `admins` list decides
who may call the admin routes, which is orthogonal to what anyone may see.

**The write tier** (`writes.enabled`) is the one place SQLhandler can write, and
it is confined by construction: CTAS / INSERT / COPY INTO are allowed only under
an allowlisted scratch root, in a subject-scoped namespace
(`<scratch-root>/<subject-slug>/…`), with an advisory single-writer lease. It
fails closed — an empty root list classifies nothing. Operationally this is
covered in [ADMIN-WRITES.md](ADMIN-WRITES.md); the identity ladder and its
rationale in [DECISIONS.md](DECISIONS.md).

## 9. Observability

Two outputs, both dependency-free (hand-rolled Prometheus text exposition and a
JSONL file), because a SQL endpoint that needs an agent to be observable is not
observable. **Everything here is best-effort: a metrics or audit failure must
never affect a query.**

`GET /metrics` (exposition 0.0.4):

| Family | Type | Meaning |
|---|---|---|
| `sqlhandler_queries_total{outcome}` | counter | ok / error / timeout / cancelled |
| `sqlhandler_query_duration_seconds` | histogram | per-query wall time |
| `sqlhandler_query_rows_total` | counter | rows returned by successful queries |
| `sqlhandler_cache_{hits,misses}_total{cache}` | counter | describe / profile / dataset / l2 |
| `sqlhandler_caller_queries_total{caller_class}` | counter | queries by caller class |
| `sqlhandler_caller_query_duration_seconds` | histogram | wall time **by caller scope** |
| `sqlhandler_caller_queue_wait_seconds` | histogram | gate queue wait **by caller scope** |
| `sqlhandler_tables`, `sqlhandler_process_rss_bytes`, `sqlhandler_container_memory_limit_bytes` | gauge | table count, RSS, container limit (0 = unlimited) |

The caller-scope families answer "how slow or expensive is *this* caller". Their
labels are the bounded scope vocabulary (subject / key fingerprint / anonymous /
other — never a raw key, hard-capped), and each family appears only after its
first observation, so an idle deployment does not carry empty series.

`SQLHANDLER_AUDIT_LOG` appends one JSON line per query outcome — `ts`, `event`,
`sql`, `state`, `duration_ms`, `n_rows`, `error` — a reviewable record of every
statement executed against the lake, SIEM-friendly by construction. The chart
does not wire it yet ([CONFIGURATION.md §9](CONFIGURATION.md#9-what-the-chart-does-not-wire)).

**Probes** are three, and they answer different questions: `/health` is the
process, `/ready` performs a cheap backend connectivity check (OneLake token and
listing, S3 list, Iceberg catalog, NFS root) and returns 503 when the data
source is unreachable, so broken-credential pods are drained from the Service
instead of serving errors; the startup probe gates both while prewarm runs.
A failing check reports **DEGRADED** for a grace period before **NOT READY**, so
one flaky storage call does not drain every replica at once. `/metrics` and
`/ready` can be gated behind a credential (`security.metricsAuth`) — with the
gate on and no credential configured they 401 for everyone, fail-closed.

## 10. Performance and the cache hierarchy

Every layer exists to remove one specific cost, and each is keyed so that
freshness is never traded away silently.

| Cache | Removes | Invalidated by |
|---|---|---|
| **Metadata** (list/describe, `cache.ttl`) | The DFS/S3/catalog listing and Parquet-footer read on every call | TTL; `list_tables` refreshes in the background so callers never block |
| **Dataset handle** (`SQLHANDLER_DATASET_CACHE_TTL`) | Re-opening the Delta log per query (the serialised part of every call) | TTL, plus snapshot-version re-check every 10 s |
| **Result L1** (`cache.resultCacheTtl`) | Re-executing an identical query | Snapshot-version key + TTL; `policy=<sha256>` of the resolved rules separates different rule sets while callers with identical rules share |
| **Result L2** (`cache.l2`) | The same re-execution **on another replica** | Shared RWX directory; off by default |
| **Single-flight** | N identical **cold** executions (a cache stampede) | Result-cache key; waiters fall through on leader failure |
| **Virtual materialization** (`cache.virtualCacheTtl`) | Recomputing an unfiltered virtual table | Definition + base snapshot versions; clustered so row-group stats prune filtered reads |
| **Block cache** (`cache.blockCache`) | Re-fetching parquet footers and column chunks | Opt-in, pod-local disk; Delta data files are cached snapshot-version-scoped |

Measured on PCAI G2 (2026-09-12, v1.6.0, 4 replicas × 8 vCPU/16Gi,
MinIO-backed, 10 queries from 50K to 3M rows — full methodology in
[../bench/BENCHMARK.md](../bench/BENCHMARK.md), scale-out campaign in
[BENCHMARKS.md](BENCHMARKS.md)):

- **Result cache: 1.8×–21× over cold execution**, scaling with query cost —
  counts/filters 1.8–2.5×, aggregations 6.9–19.2×.
- **Cold engine** (cache busted, pure DuckDB): 3M-row scans 1.0–2.0 s p50
  (4.3–5.1M rows/s for counts/projections), 200–350K-row queries 0.19–2.0 s.
- **Warm floor**: ~104–120 ms through the gateway for every query size, ~18–25 ms
  in-cluster.
- **Concurrency**: warm queries scale to ~75–81 qps at 8-way parallelism with
  flat p50; all-cold bursts saturate per-pod CPU (flat ~11–12 qps fleet-wide,
  p95 4.2 s) — the capacity-planning number for cold agent storms.

### Environment findings during the benchmark (upstream/ops, not SQLhandler)

Recorded with repro evidence in [../bench/BENCHMARK.md](../bench/BENCHMARK.md)
§"Environment findings": the EzPresto locator routes `nextUri` GETs to its web
app (404 "Query not found", 10/10 repro), the EzPresto MCP pod flaps between MCP
and health-echo states (4 restarts, dev image), EzPresto's OPA policy denies
table access for non-platform principals (`AccessDeniedException`), and the
ezaf-gateway tier alternates MCP routes on a minutes cadence for external
clients. In-cluster traffic is unaffected on all counts.

## 11. Scale-out

The HTTP layer is stateless per request (no session state), so any replica can
serve any call. What does *not* scale for free is the in-process state, and the
chart keys exist for exactly those:

| Concern | Chart key | Note |
|---|---|---|
| Replica count | `replicaCount`, `autoscaling` | One replica collapses under concurrency; 4 held flat throughput in the benchmark. The HPA's CPU target is burst/runaway protection, not load-following |
| Disruption | `podDisruptionBudget`, `topologySpread` | Keep serving through node drains; spread across nodes |
| Drain | `terminationGracePeriodSeconds` (90) | In-flight MCP tool calls finish |
| Catalog uploads + saved queries | `semanticCatalog.store` | **RWX required** — the render fails on an RWO claim for a scale-out deployment, because a per-pod volume would look shared and share nothing |
| Policy file | `security.policy.claim` | Same RWX rule, same reason: replicas must not disagree about the ACL |
| Shared result cache | `cache.l2` | Needs a genuinely shared directory |
| Async jobs | — | **Per-replica registry**; a shared jobs dir is not chart-wired yet, so keep jobs and scale-out apart ([CONFIGURATION.md §9](CONFIGURATION.md#9-what-the-chart-does-not-wire)) |

Process-per-pod vs multiple uvicorn workers inside one pod is analysed with
measurements in [SCALING.md](SCALING.md): the answer is one process, because the
heavy path already parallelises and the L1 cache would be diluted per worker.

## 12. Feature, code and test map

| Feature | Module(s) | Tests |
|---|---|---|
| Backends (onelake / s3 / iceberg / nfs), federated, attach | `onelake.py`, `s3.py`, `iceberg.py`, `file.py`, `provider.py`, `external.py` | `test_engine.py`, `test_s3_delta.py`, `test_iceberg_integration.py` |
| `profile_table` / `search_tables` / params / time travel / timeout / concurrency gate / query memory | `engine.py`, `server.py` | `test_engine.py` |
| `column_stats`, fuzzy ranking, `sample_rows`, `explain_query`, `ask_data` | `engine.py`, `server.py` | `test_column_stats.py`, `test_search_fuzzy.py` |
| Async MCP jobs, fetch-once, shared jobs dir | `jobs.py`, `engine.py`, `server.py`, `webui.py` | `test_query_jobs.py`, `test_shared_jobs.py` |
| Per-caller fairness + result budget + truncation marker | `limits.py` | `test_per_caller_limits.py` |
| Structured self-correcting errors | `errors.py` | `test_structured_errors.py` |
| Saved parameterized queries (store, bind params, auth gating) | `saved.py`, `server.py`, `webui.py` | `test_saved_queries.py` |
| MCP resources + prompts | `mcp_resources.py`, `server.py` | `test_mcp_resources.py` |
| Semantic catalog merge / hot-reload / virtual tables | `engine.py` | `test_engine.py` |
| Metrics, audit log, readiness drift | `observability.py`, `server.py` | `test_ops.py`, `test_metrics_render.py` |
| Identity ladder, policy, admin keys, OIDC rung | `identity.py`, `policy.py`, `admin_keys.py`, `oidc_identity.py`, `oidc_sso.py` | `test_identity.py`, `test_require_identity.py`, `test_oidc_rung.py`, `test_acl_policy.py`, `test_policy_leaks.py`, `test_policy_seed.py`, `test_policy_write_posture.py`, `test_admin_keys.py`, `test_admin_api.py` |
| dbt manifest import (propose-not-overwrite) | `dbt_import.py`, `webui.py` | `test_dbt_import.py` |
| Write tier (scratch CTAS/INSERT/COPY) | `writes.py` | `test_writes.py` |
| Caches (L2, block) | `l2cache.py`, `blockcache.py` | `test_l2cache.py`, `test_l2_crossreplica.py`, `test_perf.py` |
| UI: stats panel, charts, export, saved queries, inspector | `ui/index.html`, `webui.py` | `test_webui.py` |

All tests in those files pass on the current tree (`pytest tests/ -p
no:cacheprovider`); the optional S3/Iceberg integration files need their
documented local setup (see their module docstrings).
