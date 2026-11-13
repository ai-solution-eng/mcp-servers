# SQLhandler

An **MCP server** that gives LLM agents fast, direct, read-only SQL over columnar
lake data — Microsoft Fabric OneLake, S3-compatible object storage, Apache
Iceberg or a mounted directory — packaged as the `sqlhandler` Helm chart
**4.0.0** for HPE Private Cloud AI (PCAI, also branded Ezmeral Unified
Analytics).

## What it is

An **MCP server** is a Model Context Protocol endpoint: it publishes tools
(`list_tables`, `describe_table`, `run_sql`, …) that an LLM agent calls like
functions, over HTTP at `/mcp`. SQLhandler's tools answer from the customer's
existing lake storage **in place** — no copy, no load step, no second database —
by planning each query with **DuckDB** (an in-process analytical SQL engine)
over **pyarrow datasets** (Arrow's columnar scan layer), so predicates and
column projections are pushed into the scan and only the bytes a query needs
leave the store. It is a drop-in replacement for the EzPresto/PrestoDB MCP path,
and the same process also serves a read-only web explorer, a JSON API, health
probes and Prometheus metrics.

## The problem it solves

- **You have a lakehouse and agents that need it.** SQLhandler reads the storage
  you already have, through one SQL surface, so the lake does not have to be
  copied into a query engine before an agent can use it.
- **You have EzPresto/PrestoDB in front of it.** Rows are pulled over JDBC,
  re-serialised and scanned in full to answer a filtered question. This scans
  columnar files directly with pushdown, so the same question costs a fraction
  of the IO — and it is measurably faster on the same data (see
  [Performance](#9-performance-and-caches)).
- **Your agent guesses at column meanings.** `woh_ord_typ` with values `a`/`b`
  is not self-describing, so the model burns round-trips on trial SQL. A
  semantic catalog merges human-written table and column docs into every
  discovery response, and virtual tables turn a saved definition into a
  first-class queryable table.
- **A long query outlives the tool call.** Submit it as an async job, get a
  `job_id` immediately, poll, fetch the result once, cancel if it is wrong —
  instead of blocking until the client times out.
- **One agent can starve the pod.** A concurrency gate, a per-query timeout and
  per-caller buckets bound the blast radius of a single runaway loop.
- **"What ran against the lake?" has no answer.** Prometheus metrics, a
  structured audit log and a JSONL record of every query outcome make the
  question answerable.
- **An agent's result can blow its own context.** A byte budget cuts a result at
  a row boundary and states what was withheld and how to page on — never a
  silent truncation.

## How it fits in PCAI

```
   agent / MCP client            human browser
            │                          │
            ▼                          ▼
   ┌──────────────────────────────────────────┐
   │ PCAI gateway (Istio + oauth2-proxy)      │  ← PCAI renders the
   │ VirtualService, AuthorizationPolicy      │    VirtualService for you
   └──────────────────┬───────────────────────┘
                      ▼
   ┌──────────────────────────────────────────┐
   │ sqlhandler pod  :9097                    │
   │ /mcp  /ui  /api/*  /health  /ready  /metrics │
   │ DuckDB + pyarrow, caches, policy, audit  │
   └──────────────────┬───────────────────────┘
                      ▼
   OneLake (Delta) │ S3/MinIO (Parquet/Delta) │ Iceberg catalog │ NFS/PVC
                      ▲
                      └── optionally Postgres/MySQL, attached read-only
```

| | |
|---|---|
| **Depends on** | A data source (one of the four backends) and its credential; PCAI's gateway if the endpoint is exposed; a Secret for `/mcp` API keys (mandatory) |
| **Depended on by** | Any MCP client — DSH, OWUI, agent frameworks, the official Python/TS SDKs; in-cluster callers reach the Service directly |
| **Does not need** | A database, a metastore, a scheduler, a copy of the data, or any Kubernetes API access |

## Capabilities, by theme

Each entry is one line of *what*, one line of *why it is built that way*. Depth:
[documentation/FEATURES.md](documentation/FEATURES.md).

### Choose your data path in 30 seconds

| Backend (`backend:`) | Reads | Pick it when | Do not pick it when |
|---|---|---|---|
| `onelake` | Fabric OneLake — Delta Lake over ABFS | The data already lives in Microsoft Fabric; you want Delta transactions and time travel with zero copying | The data is not in Fabric (the service principal grants nothing outside its workspace) |
| `s3` / `minio` | Parquet objects, plus Delta tables detected by `_delta_log` | The data is Parquet in object storage — MinIO on-prem, AWS, or any tool can drop files there | You need a catalog's snapshot isolation (use Iceberg) |
| `iceberg` | Iceberg tables through a REST or SQL catalog | A lakehouse catalog already owns the metadata — real schemas, snapshots, concurrent writers, cross-engine reads | You only have loose Parquet files |
| `nfs` | A mounted directory — Delta **and** Parquet | The data is on a filer or a PVC, or you are proving the engine locally | The data is in object storage — a mount adds a hop for nothing |

Not sure? Start with `s3`: it is the chart's default, needs only a bucket and a
key, and its discovery rules are the simplest. You can federate several sources
of any mix later without changing tools — see
[FEATURES.md §2](documentation/FEATURES.md#2-backends-read-the-lake-where-it-lives).

### Read the lake where it lives

- **Four backends, one engine.** Storage layout is the customer's decision, not
  a migration project: a backend is a `DataProvider` subclass, so the SQL
  surface, caches and probes are identical whichever you choose.
- **Federate without a second deployment.** `sources:` puts several buckets or
  backend mixes behind one endpoint with cross-source `JOIN`s; ambiguous bare
  table names are deliberately *not* registered, so a query fails loudly instead
  of silently reading the wrong source.
- **Join the system of record.** Postgres/MySQL attach read-only
  (`ATTACH … READ_ONLY` — enforced by DuckDB, not convention) and join with
  lake tables in the same query.

### Ask in SQL, safely

- **Read-only by construction.** MCP `run_sql` is SELECT-only by default: a
  DuckDB-grammar parser guard rejects DDL/DML, `ATTACH`, `COPY` and
  `PRAGMA`/`SET` (decision D2). Attached external catalogs stay read-only in
  both modes, so the guard cannot be the only thing between a prompt injection
  and a write.
- **The filesystem is locked down.** `read_csv('/etc/passwd')` and `COPY … TO`
  fail closed on every connection; all legitimate IO goes through pyarrow
  outside DuckDB, so nothing is lost. If the lockdown cannot be applied the
  query is refused rather than silently degraded.
- **Bounded by default.** A 600 s query timeout (interrupted inside DuckDB, no
  leaked threads), 8 concurrent queries per pod, a queue timeout, a row cap and
  a byte cap on every result — each with a clean error instead of a buried
  container.
- **Fair between callers.** Per-caller buckets key on the resolved identity, and
  the process-global cap always stays the outer bound. With no identity (stdio,
  dev) behaviour is exactly the historical one.

### Long queries do not block the agent

- **Async jobs, same engine.** `query_submit` → `query_status` →
  `query_result` → `query_cancel`, plus REST twins under `/api/jobs/*`. A job
  *is* the engine's `QueryJob`, so it honours the same timeout, caps, gate and
  audit — one implementation, so cancel/timeout semantics cannot drift.
- **Fetch-once results.** The result is handed over once and freed; a second
  fetch is refused, so memory is bounded by design rather than by luck.
- **The watchdog fires even if nobody polls**, so a runaway job cannot hold a
  concurrency slot forever.

### Agent ergonomics

- **Discovery before SQL.** `search_tables` (fuzzy, ranked, with `matched_on`
  reasons), `profile_table`/`column_stats` (bounded samples), `sample_rows` (a
  look at real rows with fill rates), `explain_query` (cost estimate and a
  warm/cold band without executing), `ask_data` (drafts SQL and never runs it).
- **Errors that teach.** Every error keeps its human message and gains one
  machine-readable tail — a stable code (`E_TABLE_NOT_FOUND`, `E_TIMEOUT`,
  `E_CONCURRENCY_GATE`, …) plus `fix_hints` naming the actual knob to change.
  An unrecognised error gets no tail rather than a guessed code.
- **Memory that persists.** The last 50 query outcomes are an MCP resource, and
  the busiest tables of the previous run are prewarmed on restart — the server
  teaches itself what to warm.
- **MCP done properly.** Resources (`sqlhandler://catalog`, per-table schema,
  query memory), prompts (`explore-data`, `analyze-table`) and explicit tool
  annotations, so a cautious client never presents a reader as destructive.

### The semantic layer

- **Meaning, not just dtypes.** A human-authored catalog merges table/column
  descriptions into `list_tables`, `describe_table` and the MCP resources; it
  hot-reloads on change and a broken file degrades to empty rather than breaking
  queries.
- **Virtual tables.** A catalog entry with a `definition` becomes a queryable
  table with no DDL and no writes to the lake; filters still push down, and
  definitions compose.
- **Three attach routes, one format** — values (most auditable), an existing
  ConfigMap, or an upload from the UI/API.

### Human access

- **A read-only explorer for the same data.** One self-contained `index.html`
  (no build step, no CDN): searchable table list, schema, column stats, SQL
  editor, charts, CSV/Parquet export, saved queries, and an Inspector tab that
  lists the same 23 tools an MCP client sees.
- **Read-only is enforced the same way** — DuckDB's own grammar, plain `SELECT`
  only — and results clamp to the row cap so the payload stays bounded.

### Identity, policy and the write tier

- **Layered, resolving to one `Caller`.** `/mcp` API keys (mandatory in the
  chart), the PCAI gateway's oauth2-proxy, an OIDC resource-server rung for
  bearer tokens, and policy-as-code for row filters, column masks, hidden tables
  and dataset ACLs. No path creates a second identity or a privilege shortcut.
- **Caches respect policy, and share within a rule set.** Every cache key
  carries a hash of the caller's RESOLVED effective rules (group names and
  identity are not part of it), so callers with identical rules — the same
  policy group, or the same dataset grants — share entries, while masked and
  unmasked callers never do. Every tool, resource and web route forwards the
  resolved caller into the engine; the request-scoped contextvar is the
  fail-closed backstop.
- **Writes are confined by construction.** The one write tier (CTAS/INSERT/COPY
  INTO) works only under an allowlisted scratch root in a subject-scoped
  namespace, and fails closed with no roots configured.

### Ops

- **Metrics without a dependency.** `/metrics` renders Prometheus text
  exposition directly: query outcomes, duration, rows, cache hit/miss, table
  count, RSS and container limit — plus per-caller duration and queue-wait
  families labelled with a bounded, non-secret scope.
- **Audit without a dependency.** One JSON line per query outcome, SIEM-friendly.
- **Probes that mean something.** `/health` is the process; `/ready` performs a
  real backend check and returns 503 when the data source is unreachable, so a
  broken credential drains the pod instead of serving errors.

### Performance and caches

- **A cache hierarchy, each layer keyed to a cost.** Metadata, dataset handles,
  result L1/L2, virtual-table materializations, an optional disk block cache —
  every one invalidated by data-snapshot versions, so freshness is never traded
  silently.
- **Measured, not claimed.** 1.8×–21× for repeated queries over cold execution;
  ~104–120 ms warm floor through the gateway, ~18–25 ms in-cluster; ~75–81 qps
  warm at 8-way parallelism. Evidence:
  [bench/BENCHMARK.md](bench/BENCHMARK.md).

## Deploy on PCAI (one screen)

PCAI is a Kubernetes wrapper: **you never run `helm` or `kubectl` for the
release.** Import the chart once, then the values editor is the only surface.

1. **Prerequisites** — create the Secrets (at minimum the mandatory
   `sqlhandler-apikey`; plus your backend's credential Secret) and confirm the
   cluster prerequisites: an RWX StorageClass, and the gateway's oauth2-proxy
   provider. See [Prerequisites at a glance](#prerequisites-at-a-glance).
   The admin panel additionally needs a policy whose `admins` names that key's
   **fingerprint** — one command sets both up correctly:
   [`scripts/bootstrap_admin.py`](scripts/bootstrap_admin.py)
   ([DEPLOYMENT.md §4.1](documentation/DEPLOYMENT.md#41-first-admin--the-two-objects-that-must-agree)).
2. **Import the chart** — upload `sqlhandler-4.0.0.tar.gz` into PCAI (AI
   Essentials / EzAppConfig import). PCAI stores it in ChartMuseum.
3. **Set values** — open the deployment's *Helm Values* editor and paste a full
   values document, starting from
   [`helm/values-examples/`](helm/values-examples/). Set `backend:` explicitly
   and `ezua.virtualService.endpoint`.
4. **Apply** — PCAI substitutes `${DOMAIN_NAME}`, renders the VirtualService and
   installs. The endpoint is `https://sqlhandler.<domain>` (`/mcp` for agents,
   `/ui` for humans).
5. **Verify** — pod Running, `/ready` 200, the log line says API-key auth is
   `ENABLED`, and `tools/list` returns 23 tools.

Full procedure, every prerequisite with its failure mode, and troubleshooting:
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

## Prerequisites at a glance

| Prerequisite | Required when | Why | Fails how |
|---|---|---|---|
| Secret `sqlhandler-apikey` (key `api-keys`) | **Always** | `/mcp` API-key gate; the chart wires it unconditionally | Pod never starts — `CreateContainerConfigError` |
| Secret `s3-credentials` | `backend: s3` and not anonymous | S3 access/secret key | `CreateContainerConfigError`; wrong keys → `/ready` 503 |
| Secret `fabric-credentials` | `backend: onelake` | Entra service principal | `CreateContainerConfigError`; expired → `/ready` 503 |
| Secret `iceberg-credentials` | `backend: iceberg` | REST catalog token + data-file keys | `CreateContainerConfigError` |
| Secret `sqlhandler-db-credentials` | `databases:` with a password | Read-only attach passwords | That database reports unavailable |
| StorageClass with `ReadWriteMany` | `semanticCatalog.store`, `security.policy.claim`, `writes.scratch.pvc` | Replicas must read the *same* file | Extra pods `Pending`; replicas disagree on the ACL |
| ConfigMap `sqlhandler-policy` | `security.policy.enabled` + `seedFromConfigMap` | Seeds the writable policy file | Pods `ContainerCreating` (`FailedMount`) |
| oauth2-proxy provider in the mesh | `ezua.authorizationPolicy.enabled` (default) | Gateway authentication | Every request denied (403) |
| S3 bucket / NFS PVC / REST catalog | The matching backend | The data source | `/ready` 503; `NoSuchBucket`; `FailedMount` |
| metrics-server | `autoscaling.enabled` | HPA resource metrics | HPA targets `unknown`, never scales |

Detail and verification commands:
[DEPLOYMENT.md §2](documentation/DEPLOYMENT.md#2-prerequisites).

## Day-2

| Task | How |
|---|---|
| **Upgrade** | Import the new chart version and re-apply the same values document. Values changes alone are a re-apply. |
| **Health** | `/health` (process), `/ready` (data source), `/metrics` (Prometheus), pod events |
| **Logs** | `kubectl -n <ns> logs deploy/sqlhandler` — startup prints the auth posture and any permissive-policy warning |
| **Rotate a credential** | Update the Secret out-of-band, then re-apply: the `checksum/*` annotations roll the pods (a `secretKeyRef` env var is frozen at container start) |
| **Uninstall** | Delete the deployment in PCAI. Out-of-band Secrets/ConfigMaps/PVCs are namespace-scoped and stay until you delete them |
| **Troubleshooting** | [DEPLOYMENT.md §6](documentation/DEPLOYMENT.md#6-troubleshooting) — symptom → cause → fix, and [VERIFICATION.md](documentation/VERIFICATION.md) for check commands |

## Documentation map

| Document | The question it answers |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy this on PCAI, and what must exist first? |
| [documentation/CONFIGURATION.md](documentation/CONFIGURATION.md) | What does this knob do, what is its default, and can I set it on PCAI? |
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is it built this way? |
| [documentation/MCP.md](documentation/MCP.md) | What are the tools, resources and prompts, and how do I connect a client? |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | Is my deployment actually working? |
| [documentation/ADMIN-WRITES.md](documentation/ADMIN-WRITES.md) | How do I enable admin writes and a writable policy file? |
| [documentation/SCALING.md](documentation/SCALING.md) | One process or several workers per pod? |
| [documentation/BENCHMARKS.md](documentation/BENCHMARKS.md) | Why does it scale out, and with what numbers? |
| [bench/BENCHMARK.md](bench/BENCHMARK.md) | Full methodology and raw measurements |
| [docs/semantic-catalog.md](docs/semantic-catalog.md) | The catalog/virtual-table format, for data owners |
| [documentation/DECISIONS.md](documentation/DECISIONS.md) | Which decisions were ratified, and why? |
| [documentation/AHA_IDEA.md](documentation/AHA_IDEA.md) | The case for adopting it as PCAI's agent-facing SQL MCP |
| [demo_runbook.md](demo_runbook.md) | A scripted customer demo |

## Developing locally

**Not the deployment path** — this is for working on the code. The chart, the
PCAI values editor and the prerequisite Secrets are the deployment story
([DEPLOYMENT.md](documentation/DEPLOYMENT.md)).

```bash
uv sync                                  # or: pip install -e '.[dev]'
cp config/.env.example config/.env       # fill in backend credentials (never commit)
sqlhandler --transport stdio             # MCP over stdio
sqlhandler --transport streamable-http --port 9097
ruff check src && pytest
```

CI runs exactly that pair (plus `helm lint` / overlay renders and a hashed-lock
check) on every push and PR: `.github/workflows/ci.yml`. Run it locally before a
release — the 2026-11-12 audit found this documented gate red on a clean tree.

Local MinIO/Iceberg fixtures and the gated integration tests are described in
the module docstrings and [documentation/VERIFICATION.md](documentation/VERIFICATION.md).
