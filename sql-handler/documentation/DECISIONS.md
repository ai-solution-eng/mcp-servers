# Decision log — SQLhandler-local

> The CANONICAL decision log lives OUTSIDE this repo at
> `/home/andrew/Code/HPE/scratch_workspace/feature-ideas-2026-09/DECISIONS.md`
> (the file docstrings cite as "DECISIONS.md" — e.g. writes.py "Design
> contract", identity.py "OAuth REVISION", §4 of the OAuth revision). That
> file is not edited from here; entries that must be citable from inside the
> repo are mirrored below. Keep this file terse — same style as the
> docstring citations.

## Dataset ACLs + identity-required gate (2026-09-30)

- **APPROVED — `datasets` ACL document in the policy file** (mutually exclusive
  with hand-written `groups` in the same file — both present is a PolicyError,
  fail-closed): `{"global": [globs], "assignments": {subject | key:sha256:<12hex>:
  [globs]}, "blocked": [globs]}`. Default-closed grant model: global is visible
  to EVERY authenticated identity, assignments ADD for that identity, blocked
  hides for everyone and WINS over any grant. Globs match `schema/name` paths
  and bare names. Identity with no assignment sees global only. Escape hatch:
  anything beyond visible/hidden (row_filter, column_masks) drops `datasets`
  and writes `groups` directly (default-open posture — hidden_tables denies).
- **APPROVED — `SQLHANDLER_REQUIRE_IDENTITY`** (default off = byte-identical):
  when set, `/mcp` + `/api/*` refuse anonymous callers with 401. Probes
  (`/health` `/ready` `/metrics`) NEVER gate — kubelet cannot carry a secret —
  and the webui shell stays open (data gated at `/api`).
- **APPROVED — resource-server JWT rung** (`SQLHANDLER_OIDC_ISSUER` /
  `_AUDIENCE` / `_JWKS_URL`): users present their SSO bearer token; verified
  via JWKS. NO client secret, NO redirect URIs — deliberately NOT the RAG
  browser-SSO pattern (D22): OAuth terminates at the gateway for browsers
  (OAuth REVISION §3); servers exposed directly verify tokens as plain
  resource servers. Optional per deployment, keys remain the baseline (§6).
- **Identity channels (unchanged ladder, now with SSO):** minted static keys
  (fingerprint-bound, `sha256:<12hex>` via audit.key_fingerprint — the raw key
  never enters a policy file or audit trail), gateway relay attribution
  (`X-MCP-Caller-Subject` over a key-valid request — attribution-never-
  authorization), SSO bearer JWT (JWKS rung above), oauth2-proxy browser
  headers (existing trustBrowserHeaders pin — only with the workload
  AuthorizationPolicy enabled).
- **Admin keys keep working unchanged** under a `datasets` document: full
  access = one assignment `{"key:sha256:<admin-fp>": ["*"]}`. Key minting is
  the offline `scripts/mint_key.py` flow (mint → append to the api-keys
  Secret → fingerprint assignment into the policy ConfigMap; both hot-reload,
  no restart).

### Frontend administration (2026-09-30, sub-entry)

- **APPROVED — admin keys store + `/api/admin/*` + the Access-control panel**
  (three-owner split: identity-keys owns admin_keys.py/policy.py/identity.py,
  admin-api owns the server routes + MCP twins, ui-chart-docs owns the panel
  + chart + docs). `KeyEntry` = `{"fp": "sha256:<12hex>", "label",
  "created_at", "created_by", "source": "file"|"secret"}` — the RAW key is
  returned once at mint and never stored or recoverable; only the fingerprint
  persists (audit.key_fingerprint).
- **The `admins` designation is orthogonal to grants:** a top-level
  `"admins": [subject | "key:sha256:<12hex>"]` list in the policy document
  (hot-reloaded with it) decides who may CALL the admin routes; the
  `datasets` lists decide what anyone may SEE. Fail-closed: no policy, no
  admins list → nobody is admin.
- **Union matching, env-first:** the middleware matches api-keys
  (SQLHANDLER_API_KEYS/MCP_API_KEYS, Secret-sourced, `source: "secret"`) AND
  the admin-keys file store; Secret keys are read-only in the store (their
  lifecycle is the Secret's — kubectl), file keys mint/revoke via the panel.
- **The store is FILE-based on purpose** (chart mounts the Secret's key;
  SQLHANDLER_ADMIN_KEYS_FILE = the mounted path, never env-rendered):
  mtime hot-reload works on a file the way it never can on a secretKeyRef
  env var (frozen at container start — DEPLOYMENT.md §4.7.1 correction).
- **The panel shows the raw minted key once** (copyable, "store it now"
  warning); the admin credential itself lives in the page's memory only
  (X-API-Key header, never localStorage/sessionStorage).
