# mcp-fleet-common

The MCP fleet's shared server package — **Wave-6 G1, decision D16** (ratified):
`pcai_utils/mcp_fleet_common/` is a real package; consumers vendor a **copy** of
this directory (not a hardlink — new common code is **not** meshed; the existing
hardlink mesh for current shared files is untouched).

Modules:

| Module | Extracts | Consumers today |
|---|---|---|
| `metrics.py` | the four byte-identical `mcp_metrics.py` copies, parameterized by app prefix — **plus the one intended delta**: unknown tool names normalize to the `"unknown"` label (bounded cardinality; the B3-queued fix) | workbench (pilot) · logsearch · prometheus · searxng |
| `health.py` | the byte-identical `Route("/health") + Route("/healthz")` probe pair (payload is the one parameter; dict or per-request callable) | workbench (pilot) · logsearch · prometheus · applygate |
| `namespace_policy.py` | the blocked-wins namespace policy of the three implementations (logsearch D8-deny + escape hatch, applygate default-deny reasons, K8S-MCP open-when-unset + pattern validation) — env re-read per call everywhere | logsearch · applygate · K8S-MCP |
| `audit.py` | applygate's hash-chained JSONL writer (genesis → `prev_sha256` chain, lock-serialized, best-effort) + `verify_audit_chain`; the `caller` field is optional via an injectable provider | applygate (writer); workbench/K8S-MCP writers are plain JSONL (see queue) |

Import-time dependencies: **stdlib only**. `starlette` / `prometheus_client` are
imported lazily inside handlers (the fleet import-guard pattern — a metrics or
route failure can never break a tool call, and the package never adds an install
requirement).

## Distribution — `pcai_utils/fleet_common_sync.sh`

```bash
# vendor/update (copies the package + writes MANIFEST.sha256 inside each copy):
pcai_utils/fleet_common_sync.sh mcp_servers/workbench_mcp [more app roots...]

# drift detection (the check_links philosophy for copied packages):
pcai_utils/fleet_common_sync.sh --check mcp_servers/workbench_mcp [...]
```

* Copies `pcai_utils/mcp_fleet_common/` → `<app>/mcp_fleet_common/` (README
  included — it is the adoption protocol), excluding `__pycache__`.
* `MANIFEST.sha256` sits inside each copy (`sha256sum -c` compatible);
  `--check` re-computes every checksum, flags modified/missing files AND
  unmanaged extra files, and exits 1 on any drift.
* Never deletes a dest file the source dropped — a manifest mismatch on
  `--check` is the signal to re-sync (deletion is an adoption-protocol
  decision, not a sync side effect).
* Portability note: no `comm` (uutils coreutils' comm warns/exits 1 on
  identical sorted input); the script uses `sha256sum`/`awk`/`diff`-free
  membership tests only.

## Mesh safety (why `check_links.sh` stays ALL SYNC)

`link_utils.sh` and `check_links.sh` derive their managed set from
`pcai_utils/*.py` **top-level only** (+ `preprocessors/`, + the fixed
ROOT_FILES list). A new *subpackage directory* is structurally excluded from
per-file meshing — no code change was needed. For belt-and-braces,
`hardlink_ignores.base.json` gained two rel-path-anchored patterns
(`pcai_utils/mcp_fleet_common`, `pcai_utils/tests`) that fire only on the
hypothetical flow of hardlinking FROM `pcai_utils` (or the workspace root)
itself. Deliberately NOT a bare `mcp_fleet_common` name pattern: `matches_any`
matches directory names at any depth, which would exclude every app's vendored
copy from its delivery mirror. Residual (documented, accepted): a manual
`hardlinker.py pcai_utils <dest>` run would still copy the package — that flow
does not exist today (no hardlink config uses pcai_utils as source).

The vendored copies are ordinary app files: they mirror to `pcai-solutions/`
normally with the app's next mirror sync (orchestrator-run, dry-run reviewed).

---

## Divergence catalog (what was extracted vs skipped, and why)

Extracted (behavior-identical today across their consumers):

1. **`/health`+`/healthz` route pair** — workbench, logsearch, prometheus,
   applygate build the identical two-route pair with a JSONResponse closure;
   payloads: workbench/logsearch `{"status": "ok", "server": name}`,
   prometheus `{"status": "ok", "prometheus": base_url}`, applygate a richer
   body re-read per request. The factory parameterizes exactly that.
2. **`mcp_metrics.py` ×4** — verified byte-identical except the app-name
   prefix (docstring env name, logger name, `METRIC_PREFIX`, HELP line).
   Parameterized as `Metrics(metric_prefix=…, display_name=…)`.
3. **Namespace policy ×3** — one blocked-wins core; the empty-allowlist
   posture and reason strings are knobs (see the module doc).
4. **Hash-chained audit writer** — applygate's, generalized (optional
   `caller` provider, path resolution: call → ctor → env → default).

Skipped, with reasons:

* **Egress/url_policy** — RAG's (`utils/url_policy.py`: two-tier denylist with
  an unconditional core, DNS-rebinding pin, media-fetch specifics) and
  searxng's (CDP route filter, per-hop redirect validation, proxy-tunnel
  residual documented in Wave 1) are INTENTIONALLY diverged: different threat
  surfaces (media fetch vs search fetch vs browser path), different escape
  hatches. Merging them would change at least one server's refusal behavior —
  the opposite of this package's parity-first contract. Revisit only if a new
  fetch surface appears; then extract the SHARED CORE (resolve→classify→pin)
  without the per-app policy tiers.
* **Webui scaffolding** — genuinely different per app (endpoint sets, response
  shapes, docstrings; 241–917 lines each; searxng has none). Nothing is
  byte-identical in ≥3 servers. The only shared pattern is "read-only JSON API
  over the same core functions", which is a convention, not code.
* **K8S-MCP's inline metrics** — different counter family (`kind`/`outcome`
  server events, raw-ASGI rendering, no starlette Route) and its own
  registry conventions; migrating it is a per-server decision, queued.
* **applygate's inline metrics handler** — different fallback shape (honest
  explanation text vs the MiniCounter exposition) and counts at the audit
  choke point; queued with its audit migration.
* **Auth middleware** — already shared: `pcai_utils/mcp_auth.py` IS one
  hardlinked inode fleet-wide (P0-1/2/4 closed pre-Wave-1). It stays meshed
  (existing mesh untouched per D16); a move into this package can ride a
  later wave with the copy mechanism.

The one UX delta in everything above (workbench pilot):

> **Unknown tool names normalize to the `"unknown"` metrics label.** Before:
> a probe of `no_such_tool` rendered a `tool="no_such_tool"` series (an
> anonymous caller could mint unbounded series — cardinality blow-up + a small
> oracle about which tool names exist). After: one bounded `tool="unknown"`
> series; registered tools keep their real names; outcomes unchanged. That is
> the B3-queued fix landing in the shared implementation, and the ONLY
> behavior change this package introduces.

---

## Adoption protocol — per-server migration recipe

Workbench is the pilot (committed proof: suite 109P/0F, render byte-identical,
health bytes identical, `--check` green). For each remaining server:

### Step 0 — pre-flight (read-only)
1. Record the suite bar: `<app>`'s current pytest counts (see
   `FLEET-PLAN-ARTIFACTS/wave0/logs/` for the command per app).
2. Record the render baseline: `helm template release-name <app>/helm/ | md5sum`.
3. `git -C mcp_servers status --porcelain` — must be clean before you start.

### Step 1 — vendor
```bash
pcai_utils/fleet_common_sync.sh mcp_servers/<app>
pcai_utils/fleet_common_sync.sh --check mcp_servers/<app>   # must be green
```

### Step 2 — wire (per concern, ONE concern per commit-sized step)
* **metrics**: in `server.py`, replace `import mcp_metrics` with the binding
  (see workbench `server.py` — the comment block shows the exact shape), then
  `import mcp_metrics` in the app's `tests/test_metrics.py` becomes
  `from server import mcp_metrics`. Do NOT touch assertions except where a
  test pins the pre-fix unknown-tool label (that is the delta — update it to
  `tool="unknown"` and add the dedicated normalization test; see the pilot).
* **health**: replace the inline `async def health` + two `Route(...)` lines
  with `routes = list(fleet_health.health_routes(<payload-or-callable>))` —
  preserve the payload exactly (dict for workbench/logsearch shapes; a
  callable for per-request bodies like applygate's).
* **namespace policy** (logsearch/K8S-MCP queue items): bind `NamespacePolicy`
  with the app's env names + posture; keep each app's refusal TEXT in its own
  server (the shared reason strings are the applygate-style default) unless
  byte-parity of errors is re-proven.
* **audit** (applygate queue item): swap `_audit` internals for
  `HashChainedAuditLog(path=None, env_var="APPLYGATE_AUDIT_FILE",
  default_path="/data/audit.jsonl", caller_provider=_audit_caller)` — keep the
  7-key entry dict + the metrics-counter side effect at the call site; the
  JSONL format is unchanged (additive fields only), so `verify_audit_chain`
  keeps verifying old trails.

### Step 3 — parity gates (ALL must be green)
1. **Suite**: the app's pytest bar — 0 failures; identical counts except
   import-updated tests and the one new normalization test.
2. **Render**: `helm template release-name <app>/helm/ | md5sum` equals the
   Step-0 baseline (the package adds NO chart change — if the md5 moves, you
   edited the chart by accident).
3. **Health bytes**: TestClient-GET `/health`+`/healthz` (never `/mcp` — fleet
   convention) → status/body/content-type identical to pre-migration.
4. **Metrics bytes**: with `<APP>_METRICS_ENABLED=1`, the exposition's counter
   family name and HELP line identical; unknown-tool probes land on
   `tool="unknown"`.
5. **Mesh**: `bash pcai_utils/check_links.sh` → ALL SYNC.
6. **Vendor drift**: `fleet_common_sync.sh --check <app>` → green.
7. `git -C mcp_servers status --porcelain` shows only the expected files.

### Rollback
`git -C mcp_servers checkout -- <app>` + delete `<app>/mcp_fleet_common/` —
no chart/mirror/chart-values footprint to undo.

---

## Remaining-7 adoption queue (per-server notes)

Ordered smallest-risk-first. Each row: the module(s) it adopts + its specific
notes. NONE of these have been done by Wave-6-G1 (workbench only).

| # | Server | Adopts | Notes / cautions |
|---|--------|--------|------------------|
| 1 | **logsearch** | metrics, health, namespace policy | metrics+health are drop-ins (same shapes as workbench). Namespace policy: bind with `LOGSEARCH_*` envs + the `LOGSEARCH_EMPTY_ALLOWS_ALL` escape; the server's `_ns_denied_error` TEXT stays in the server (the shared predicate replaces only the bool core) — port the `_namespace_allowed` tests as-is. |
| 2 | **prometheus** | metrics, health | metrics drop-in (`prometheus_mcp` prefix). Health payload `{"status": "ok", "prometheus": config.base_url}` is a static dict. Its inline prometheus self-metrics (E3/D14 additions) stay as-is. |
| 3 | **searxng** | metrics | drop-in (`searxng_mcp` prefix); dual-transport route assembly — the `/metrics` Route insert stays where it is (before the merged SSE/HTTP routes). Egress policy NOT extracted (intentional divergence — see catalog). |
| 4 | **applygate** | health, audit | Health: the payload is a per-request CALLABLE (re-reads `_env_list("APPLYGATE_ALLOWED_NAMESPACES")` + `FIELD_MANAGER`). Audit: swap `_audit` internals for the shared writer; keep `_caller_context`/`_CallerAuditMiddleware` and pass `_audit_caller` as the provider; keep the counter side effect. `verify_audit_chain` → `mcp_fleet_common.audit.verify_audit_chain` (same report shape). Metrics handler deliberately NOT migrated (different fallback shape — record why in its README if revisited). |
| 5 | **K8S-MCP** | namespace policy (optional) | The trickiest: raw-ASGI (no starlette Route table), pattern-validated parse, open-when-unset posture, its own refusal strings, lowercase normalization. Bind with `default_empty_allows=True` + `parse_ns_patterns`; migrate `namespace_violation` onto `policy.violation()` only with its 237-test suite as the gate — its strings/normalization must stay byte-identical. Its inline `kind/outcome` metrics stay. |
| 6 | **SQLhandler** | (its own repo — separate wave) | Inline `_McpApiKeyMiddleware` + gated web metrics; the audit JSONL is its own writer. Adoption crosses the repo boundary (`SQLhandler/src/sqlhandler/…`) — needs its own render-diff + its 475-test suite; schedule after the mcp_servers seven. |
| 7 | **ddgs_mcp / ddgs_lite** | — | Do NOT migrate: retiring per D17 (deprecation warnings shipped in W3; archive after the window). Spend zero consolidation effort here. |

### Known follow-ups discovered during the pilot (recorded, NOT done here)

* **Pre-existing image packaging gap (fleet-wide, unchanged by G1):** the
  per-app Dockerfiles COPY `mcp_auth.py`/`mcp_metrics.py` into `/app` but
  `pyproject.toml`'s `py-modules` never lists them — the installed console
  script would fail on `import mcp_auth`/`import mcp_metrics` (the F4 lesson
  in prometheus fixed exactly this there). Any server adopting the package
  should fix packaging in the same commit: add
  `COPY mcp_fleet_common/ ./mcp_fleet_common/` and include the package in the
  built wheel (e.g. `py-modules` entries or a `[tool.setuptools]` package
  include), then `docker build`-smoke the import.
* **workbench's `.audit.jsonl`** is plain JSONL (no chain). Switching it to
  `HashChainedAuditLog` is ADDITIVE (readers tolerate new fields) but changes
  the file's schema — queued as its own decision (operator-visible tool).
* **Caller identity in the shared middleware** (C4's queued recommendation):
  `mcp_auth.py` has no `_Caller`; applygate mirrors K8S-MCP's contextvar
  pattern locally. When the auth middleware grows caller resolution, the
  `audit.CALLER_CONTEXT` slot here is the natural sink — one more reason the
  audit module keeps the provider pluggable.

## Package tests

```bash
cd pcai_utils && /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python \
  python -m pytest tests/test_mcp_fleet_common.py -v   # fallback backend
/home/andrew/.conda/envs/ML14/bin/python -m pytest tests/test_mcp_fleet_common.py -v  # prometheus_client backend
```

58 tests: health shapes/bytes · metrics prefix + outcomes + normalization +
forced-fallback backend · the namespace matrix ported from all three
implementations · the full audit-chain matrix (incl. legacy-root trails).
Both interpreters green (the suite runs under both backends by construction).
