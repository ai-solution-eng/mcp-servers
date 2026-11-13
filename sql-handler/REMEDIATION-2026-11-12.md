# SQLhandler — remediation wave, 2026-11-12

Same-day follow-through on [AUDIT-2026-11-12.md](AUDIT-2026-11-12.md): **2 P0, 13 P1, ~40 P2 and ~40 P3 findings fixed**, then cross-validated by six independent reviewers who were pointed at each other's work.

| | before | after |
|---|---|---|
| Tests | 1542 passed / 25 skipped | **1796 passed / 25 skipped / 0 failed** (+254) |
| `ruff check src` (the documented gate) | **11 errors (red)** | **clean** |
| `helm lint` + 4 render modes | clean | clean |
| P0 repros | both exploitable | both refused (see below) |

Everything below was verified by execution, not by reading the diff.

---

## 1. The two P0s

**P0-1 — policy masking was bypassable with a guessable name.** The raw dataset is no longer registered under a deterministic `__sqlhandler_base_<n>`; the name is randomized per query, any caller SQL containing the reserved prefix is refused (case-insensitive, quoted forms included) at four entry points, and `sqlguard.assert_readonly` refuses it at the MCP/web boundary too. Proof — the lead's repro, before and after:

```
before: SELECT b.ssn FROM work_order o JOIN __sqlhandler_base_1 b ON true
        -> [{'ssn': 'ssn-000'}, {'ssn': 'ssn-001'}, {'ssn': 'ssn-002'}]   # RAW, row filter gone
after : REFUSED: LakehouseError Query refused: SQL may not reference SQLhandler's internal
        relations (the '__sqlhandler_' prefix is reserved)...
        masked path unchanged: [{'id': 0, 'ssn': '***'}, {'id': 2, 'ssn': '***'}]
```

**P0-2 — federation resolved every source to the global backend.** A passed env mapping now WINS over `os.environ` (a ContextVar + decorator on all 10 `load_*_config` functions), the per-source mapping is sparse so omitted fields still fall through to the environment, and the chart no longer renders `SQLHANDLER_BACKEND` in federation mode. Proof:

```
before: per-source backend=s3 -> onelake | bucket None | endpoint None | keys None
after : per-source backend=s3 -> s3 | bucket per-source | endpoint minio.per-source:9000 | use_ssl True
```

---

## 2. The 13 P1s

| # | Fix | Proof |
|---|-----|-------|
| P1-1 | Cache-key collision on DuckDB `E'...'` strings — normalization now bails on a backslash inside a quoted region | the two statements that returned different values under one key now get different keys |
| P1-2 | Filesystem lockdown no longer defeats spill: `temp_directory` + max size → `allowed_directories` → `enable_external_access=false` | 50M-row sort under a 256 MB budget spills and succeeds; `read_csv('/etc/hostname')` and the k8s SA token still refused |
| P1-3 | A mask spec that cannot compile refuses the POLICY FILE at load; the runtime fallback emits `'***'`, never the raw column | `sha256:12x` / `sha256:0` now refuse the file naming table+column |
| P1-4 | `/mcp` gate arms on any CONFIGURED store (revoking the last key no longer opens it); an unreadable store serves env keys and 401s unverifiable minted keys (503 only when nothing can authenticate) | 8 gate-matrix tests |
| P1-5 | Relay attribution is no longer authorization by default: an unsigned `X-MCP-Caller-Subject` is refused unless `SQLHANDLER_TRUST_RELAY_HEADERS=1`; signed path unchanged | 12 relay tests incl. the admin-impersonation shape |
| P1-6 | `/api/*` is identity-gated by default (chart `requireIdentity: true`), **and** an explicit API key now authenticates on `/api` — the documented "paste a key" recovery | 6 new tests: whoami and the data plane agree on the same key |
| P1-7 | `isError=true` on all 38 tool-failure paths (a `_ToolError` marker; wire text byte-identical) | failing run_sql/scan_table/describe_table flagged; success stays false |
| P1-8 | `readOnlyHint` is honest for all three env-dependent tools (`run_sql`, `query_saved`, `query_submit`) under both opt-outs | 4 env combinations |
| P1-9 | Semantic-catalog reads are caller-scoped (engine filters with the same visibility predicate; routes forward `caller=`) | a hidden table's entry is absent / not-found |
| P1-10 | An L2 hit decodes under the query gate and a decode budget (declared size checked before `pq.read_table`); a sidecar with no usable size is a miss | budget + corrupt-sidecar tests |
| P1-11 | dbt apply reads the catalog FILE (not `_catalog()`, which degrades to `{}`) so an unreadable store aborts instead of being replaced | chmod-000 store with 2 hand-written entries survives byte-identically |
| P1-12 | Iceberg catalog paths are contained: `file://`/absolute/schemeless refused unless `SQLHANDLER_ICEBERG_ALLOW_LOCAL_FILES=1`, then realpath-contained (symlink-safe) | 12-test attack battery (symlink, `../`, prefix boundary, remote warehouse) |
| P1-13 | Sources-path `S3_USE_SSL` defaults to true; only an explicit falsy opts out | both paths tested |

---

## 3. P2/P3 — what changed, by area

**Engine** — volatile queries (`now()`, `random()`, **and the paren-less** `current_timestamp`/`current_date`/`localtime` spellings) are never cached; result-cache byte accounting fixed on overwrite; L1 entries warmed from L2 carry `sql=` so write-tier eviction sees them; block-cache keys include block size and validate lengths; a cache-layout version gates L2 sidecars and the block-cache dir; cancel works before the connection opens and the terminal state is consistent; paren-less `PRAGMA` refused; negative env ints fall back with a warning; `FILE_ACCESS=1` warns loudly; the memory-budget failure logs at WARNING; `SQLHANDLER_DUCKDB_MAX_TEMP_SIZE` (20 GB default) bounds the spill dir; gate release targets the acquired semaphore; `cache_stats()` is O(1) (sweeper-published); L2 dir chmod + orphan reclaim; the policy-columns provider is finally wired; the write-tier source snapshot is keyed per policy hash.

**Server** — /api token middleware accepts `X-API-Key`, a comma-separated list and collects every presented claim; a COPY refused by the write tier surfaces its rewrite hint; markdown cells escape `|`/newlines (both the query and profile writers); CSV text cells (and headers) are formula-neutralized; `/metrics` label values are escaped everywhere; `token_matches` fails closed on an empty expectation; `_suggest_follow_up` never renders "None"; whoami's docstring matches reality.

**Web** — saved-run and `/api/jobs` apply the always-on read-only guard + clamp; the **inspector bridge** applies the same guard when the MCP plane is relaxed (it could execute DDL on the /api credential plane); the 409 fetch path no longer destroys a finished result on a transient read error; claim steal is token+rename+flock (exactly one winner — was 2-3 in 15/50 stress trials); security headers + strict CSP; generic 500s keep client errors actionable (400 + message); OAuth state planted from the generator; CSRF refuses opaque `Origin: null` and honors `X-Forwarded-Host`; QueryJobManager RLock; highlight caps; dead duplicate handler / phantom injection / F821 removed.

**Authz** — keyed hash masks (`SQLHANDLER_MASK_HASH_SECRET`, folded into the policy hash so rotation rekeys the cache space); sharing action-line byte cap; presigned-URL SSRF guard (scheme + private-range refusal, body never echoed); `COPY` reclassified as unsupported with a CTAS hint; OIDC `aud` required with `azp` additional; SSO discovery fetches outside the lock with a stampede set; staging dir 0o700 + per-file cap; mask grammar refuses unknown algorithms; `_glob_match` documented + pinned.

**Connectors** — GCS credential pinning warns on cross-source bleed (and on a source silently inheriting a pin); sqlserver ODBC passwords brace-quoted; unsupported Iceberg schemes refused; ADLS missing secret-env named; unknown catalog type refused; `.gz` caps count DECOMPRESSED bytes across **multi-member** files; mysql attach requires an explicit user; the dbt gate is a real parser check (EXPLAIN refused); `check_connection` scrubs credentials; a listing cap exists on **all four** backends.

**Chart / build / CI** — `requireIdentity` defaults ON (breaking, documented, opt-out); audit trail chart-wired to a durable claim (render refuses an ephemeral one); optional egress default-deny (DNS + trusted CIDRs, no arbitrary port pinning); metrics-auth pairing guard; a real ServiceAccount template; the lock is hash-bearing and installed with `--require-hashes`; `ARG BASE_IMAGE` for digest pinning; `bump_version.sh` no longer strips hashes; **CI exists** (`ruff` + `pytest` + `helm lint` + 4 renders + guard-rail assertions + hashed-lock check); the OIDC operator script verifies TLS (`CA_BUNDLE`) and keeps the admin password out of argv.

---

## 4. Cross-validation (six reviewers, direction-matched)

Each area was reviewed by an agent that did **not** write it, with instructions to break it and to state what held.

| Area reviewed | Reviewer | Claims | Verdicts | What it caught |
|---|---|---|---|---|
| engine | GLM | 19 | 18 correct, 1 incomplete | paren-less volatile keywords leaked a frozen timestamp |
| authz | GLM | 13 | 13 correct | 3 P3s (line-cap boundary, OIDC aud undocumented, mask CAST drift) |
| server | DeepSeek | 7 | 3 correct, 4 partial | profile-markdown unescaped; loaded-then-corrupt store 503'd verifiable keys |
| web | DeepSeek | 16 | 15 correct, 1 partial | **inspector could execute DDL on /api**; generic-500 over-reach; 409 destroyed a result |
| connectors | DeepSeek | 12 | 8 correct, 4 incomplete | **P1-11 was still broken at the wrong layer** (proved data loss); vacuous test; multi-member gzip; missing caps |
| lead (chart/build/CI) | GLM | 11 | 9 correct, 1 with P1, 1 partial | **requireIdentity default broke the documented /api key recovery**; egress ports; CI tripwires |

**78 claims: 66 correct, 12 incomplete/partial, 0 wrong.** Every P1/P2 raised was fixed and re-verified; the P3s were fixed or recorded below. The two headline catches — the `/api` key recovery and the dbt data-loss layer — were both introduced *by this wave*, which is exactly why the second pass existed.

---

## 5. Accepted residuals (with reasons)

1. **`mcp_fleet_common/audit.py` is fleet-shared and manifest-verified.** The process-safe `flock` + broader exception handling were implemented, then **reverted**: the vendored copy must match `MANIFEST.sha256` and the canonical file lives in the fleet repo. Apply there, then re-sync. (SQLhandler does not use this writer; it has its own JSONL trail.)
2. **`fastrender.py` stays unwired.** 641 tested lines with a parity contract; wiring it needs a byte-identical-output proof on real result shapes. The false claim in SCALING.md was corrected and the module now says so in its docstring.
3. **`requireIdentity: true` is a deliberate breaking change** for gateway-only deployments whose humans have no SSO cookie and no key. Opt out with `security.identity.requireIdentity: false` (the g2 overlay does, with a warning comment).
4. **OIDC `aud` is now required.** A Keycloak deployment whose tokens carry `aud: ["account"]` needs an audience mapper or `SQLHANDLER_OIDC_AUDIENCE=account`. Documented; declines stay fail-closed.
5. **DuckDB extensions are still downloaded at build** (unsigned, community repo included). Mitigations documented in the Dockerfile: build on a trusted runner, or vendor the extension tree and COPY it. Digest-pinning the base image is now one `--build-arg`.
6. **`bootstrapAdmin: true`** remains the default: a fresh install otherwise has no reachable admin. Pre-policy, every key holder is an admin — documented in the values file.
7. **A self-minted, subject-bound key used on `/api`** resolves without its store subject (the `/mcp` middleware owns that enrichment). Fail-restrictive: the caller gets key-level access, never a subject's grants.
8. **Sharing SSRF guard has a DNS-rebinding window** (per-host cache, documented in-code) and **`/mcp` header coverage is inferred** from middleware placement rather than live-tested.
9. **Chart dependency path points outside the repo** (`file://../../pcai-helm-lib`) — the fleet-drift channel. `helm template`/`package` work from the vendored `charts/` dir and the packaged tgz is self-contained; `helm dependency build` in a fresh clone would not.

---

## 6. How to verify

```bash
cd /home/andrew/Code/HPE/SQLhandler
.venv/bin/ruff check --no-cache src          # clean
.venv/bin/python -m pytest tests -q          # 1796 passed, 25 skipped
helm lint helm && for f in helm/local/values.*.yaml; do helm template t helm -f "$f" >/dev/null; done
.venv/bin/python .scratch/audit-2026-11-12/p0repro.py      # step 2 must REFUSE
.venv/bin/python .scratch/audit-2026-11-12/z1verify.py     # no cache-key collision
```

Evidence trail: `.scratch/audit-2026-11-12/` — seven zone audits, six cross-validation reports, and the repro scripts.
Pre-remediation safety snapshot of the (still uncommitted) working tree: `.scratch/backup-2026-11-12/`.

---

## 7. Turn-3 follow-up (the "remaining issues")

### Does requireIdentity just mean an API key is always required?

**No — it requires a *resolved identity*, not specifically a key.** The gate
refuses ANONYMOUS callers on `/mcp` and `/api/*`; any rung of the identity
ladder satisfies it:

| Credential | Works on /api | Notes |
|---|---|---|
| `X-API-Key` / `X-API-Token` / static `Bearer` | yes | env key list **and** the minted keys store; a self-minted key carries its store subject |
| `SQLHANDLER_API_TOKEN` (shared token) | **yes (fixed now)** | resolves `via="api-token"`, fingerprint only |
| OIDC bearer JWT | yes | RS256, iss/aud/exp verified |
| D22 SSO session cookie | yes | browser sign-in |
| oauth2-proxy browser headers | yes, only with `trustBrowserHeaders` | trust-gated |
| anonymous | **401** | the only thing the default refuses |

Still open by design: `/health`, `/ready`, `/metrics`, `/ui`, `/api/whoami`,
`/oauth/*` (probes and the sign-in path), and `/api/admin/*` (self-gated,
stricter).

**The gap the question exposed:** the shared `SQLHANDLER_API_TOKEN` did *not*
satisfy the gate — the token middleware sits INSIDE the identity gate, so a
token-only deployment was locked out of `/api` (token accepted, identity 401).
Fixed: the ladder resolves the token (`identity.resolve_api_token_caller`), so
the two layers agree. Pinned by
`test_require_identity.py::test_api_token_request_passes_the_identity_gate`.

Honest one-liner: **with the chart default an ANONYMOUS caller is refused; a
caller with any configured credential — key, shared token, OIDC JWT, or SSO
session — is not.** A deployment with no OIDC and no gateway headers has one
usable credential class: keys/tokens.

### Residuals closed in this pass

| Residual | What was done |
|---|---|
| Shared API token didn't satisfy requireIdentity | new ladder rung + 3 tests (token, rotation list, wrong-token 401) |
| Self-minted key lost its subject on `/api` | key matcher now covers env **and** store; the subject rides (admin resolver too) |
| OIDC `aud` strictness was a deployment trap | `SQLHANDLER_OIDC_AUDIENCE` is now a comma-separated list (accept `ua,account`), and an audience mismatch logs ONE actionable WARNING instead of a debug line |
| `fastrender` dead code / false doc | **wired** into the markdown path behind its all-or-nothing parity contract; found and pinned a real parity bug (naive timestamps: fast said `1970-01-01`, pandas `1970-01-01T00:00:00.000000`) — timestamp tables bypass it until the formatter is resolution-aware |
| Fleet-shared audit writer not process-safe | `flock` + broader exception handling applied to **both** copies, `MANIFEST.sha256` regenerated, sync test green. WARNING: the canonical fleet repo still needs the same patch — a future `fleet_common_sync.sh` run would overwrite it |
| Sharing SSRF DNS-rebinding window | the unbounded `lru_cache` is now a 30 s TTL cache (bounded staleness); IP-pinned connect remains the complete fix and is documented as such |
| Mask typing drift (keyed CASTs, unkeyed doesn't) | documented in `_hash_expr` rather than changed: the keyspaces never mix, and changing the unkeyed digest would silently alter every existing numeric mask |
| `bootstrapAdmin` default | verified there IS a loud startup notice plus an admin-gate message naming the bootstrap state; left as the documented trade-off |
| DuckDB extension download unverified | unchanged; the Dockerfile documents both mitigations (trusted runner, or vendor + COPY) |

### Smaller cross-validation P3s also fixed

`limit: true` no longer becomes `limit: 1` (bool is an int subclass); markdown
escaping moved to the ARROW level so both renderers see identical values and
covers the profile writer too; the non-positive `max_rows` rule (0 = unlimited)
is enforced on the fast path as well (a regression the suite caught); the F12
generic-500 rule now states its one deliberate exception (a store-write OSError
keeps its path-bearing message); isError coverage extended to four more tools;
metric label escaping has its own test; the write-gate helper's docstring
matches the new key resolution.

**Final state: 1802 passed / 25 skipped / 0 failed, ruff clean, helm lint + 4
renders clean, both P0 repros refusing.**

