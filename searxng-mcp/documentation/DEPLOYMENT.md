# Deployment — searxng-mcp on PCAI

## 1. The deployment contract

Deployment on PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) is
values-driven. The chart is **packaged and imported into the PCAI catalog
once**; the deployment is created from it, and every knob below is set in
the chart's **Helm Values** editor (or via the PCAI API) and re-applied.
PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering.
There is deliberately no `helm install`/`kubectl apply` runbook here — that
is not how PCAI deployments happen (the plain-Helm path is the developer
path; see the values-examples README).

**What PCAI does for you:** resolves `${DOMAIN_NAME}`, creates the target
namespace, installs via its controller, and routes the gateway host
(`searxng-mcp.${DOMAIN_NAME}`) to the deployment's VirtualService: `/mcp` →
the MCP server (9090), `/` → the normal SearXNG web page (8080).

**What you must do out-of-band**, because it cannot be a value:

- Create the SearXNG session Secret (§2.1) — or accept a literal
  `searxng.secretKey` in values (lab only).
- Provide OUTBOUND EGRESS — direct or through the corporate proxy (§2.3).
  This server is the fleet's window to the public internet; without egress
  it has no reason to exist.
- When behind a TLS-intercepting proxy: provide the CA ConfigMap(s) the
  chart mounts (§2.2) — the proxy's own root must be in them.

**Upgrade** = import the new chart version, keep the values, re-apply (§5).

## 2. Prerequisites

### 2.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| SearXNG session Secret (site-chosen name, wired via `searxng.existingSecret`, key `secret` — chart default key name) | `secret` | `templates/deployment.yaml` → `SEARXNG_SECRET` via `secretKeyRef` | Seeds SearXNG's session/crypto secret; the preferred wiring keeps the real secret off the values path and out of git | **Neither this Secret nor a literal `searxng.secretKey`: the deployment template errors at render** — `searxng.secretKey is required (SEARXNG_SECRET) — or set searxng.existingSecret`. A referenced-but-missing Secret: pod `CreateContainerConfigError`. A placeholder literal: pod runs, but on a known secret | `kubectl -n <ns> create secret generic searxng-secret --from-literal=secret=$(openssl rand -hex 32)` |
| API-key Secret (OPTIONAL — e.g. `searxng-mcp-apikey` or the fleet `mcp-fleet-apikeys`, wired via `apiKey.existingSecret`, key `api-keys`) | `api-keys` (comma-separated list) | → `SEARXNG_API_KEYS` (or `MCP_API_KEYS`), rendered ONLY when referenced | The optional key gate on `/mcp` — **recommended beyond a lab**: `fetch_content` can reach in-cluster URLs (the SSRF guard bounds it, but unauthenticated in-cluster callers are still a real surface) | Not referenced: server runs open with a loud startup warning (by design). Referenced but missing: pod `CreateContainerConfigError` | `kubectl -n <ns> create secret generic searxng-mcp-apikey --from-literal="api-keys=$(openssl rand -hex 32)"` |

Rotation: session Secret = update the Secret + restart the pod; API keys =
append to the comma list (env re-read per request, zero downtime).
`/health`, the SearXNG UI route, and the opt-in `/metrics` stay key-free.

### 2.2 Patches / cluster objects (the things that are not values)

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| Namespace with `hpe-ezua` labels | PCAI convention for platform-managed vendor services. | Platform tooling cannot recognize the workload. | `kubectl get ns <ns> --show-labels` |
| Kyverno ClusterPolicy (chart-rendered, UNGATED — pre-install hook, idempotent) | Stamps `hpe-ezua/type: vendor-service` + `hpe-ezua/app: searxng-mcp` labels — the marker PCAI uses to recognize vendor services. | Cosmetic-to-platform: missing vendor labels. | `kubectl get clusterpolicy | grep searxng-mcp` |
| Istio `VirtualService` on the EZAF gateway (chart-rendered when `ezua.enabled` — chart default true) | Two routes: `/mcp` → MCP server (9090), everything else (`/`) → the SearXNG web UI (8080), both `timeout: 660s`. The endpoint **doubles as `MCP_HOSTNAME`** — the DNS-rebinding Host pin (read once at server startup). | Empty endpoint aborts the render (gated on `ezua.enabled` + `ezua.virtualService.enabled`); an unresolved `${DOMAIN_NAME}` registers a host that matches nothing. | `curl -I https://searxng-mcp.<domain>/healthz`; `kubectl -n istio-system get virtualservice | grep searxng` |
| CA ConfigMap(s) for TLS-intercepting egress (only on MITM-proxy clusters): `caCert.configMap` (default `zscaler-root-ca`) for the MCP container's fetch bundle; `browser.caCert.configMap` (default `ezaf-root-ca`) for the browser sidecar | The chart mounts the ConfigMap's PEMs and (for the MCP container) combines them with the public bundle into `/ca-bundle/ca-bundle.crt` (init container `init-ca-bundle`), wired to `FETCH_CA_BUNDLE`/`SSL_CERT_FILE`/`CURL_CA_BUNDLE`. The browser sidecar imports its PEMs into the Chromium NSS DB (`~/.pki/nssdb`). **The ConfigMap must exist in the release namespace — copy the cluster-wide one if needed — and must carry the proxy's own root, not only public roots** (2026-10-04 SE-G2: the cluster-wide `ezaf-root-ca` held only public CAs; the egress proxy's root had to be added too) | MCP container: every fetch dies with `curl: (60) unable to get local issuer certificate`. Browser sidecar: every render dies with `ERR_CERT_AUTHORITY_INVALID` | `kubectl -n <ns> get cm <ca-configmap>`; one fetch + one browser render through VERIFICATION.md |
| `NetworkPolicy` — the MCP network zone (optional, `networkPolicy.enabled: false` default) | Default-deny ingress admitting only `authorizedClients.namespaces` (+ same-namespace pods when `networkPolicy.sameNamespace`, the edge-gateway pods via `ingressGatewayNamespace`/`ingressGatewayLabels` while `allowEzafGatewayIngress: true` — the browser path stays open by default so the VirtualService keeps working). Kubelet probes: `probeCidrs` + `probeExceptCidrs`. Optional egress governance: `networkPolicy.egress.enabled` + `egress.extraEgress` — **egress stays unrestricted by default because this server's whole job is egress.** | Off = baseline. On without callers listed: they time out; `monitoring` unlisted (metrics on) kills ServiceMonitor scraping silently. | From an allowed ns: `curl -m 3 http://<service>:9090/healthz`; from a non-listed ns: expect timeout |
| `PodDisruptionBudget` (optional, `podDisruptionBudget.enabled: false` default) | Voluntary-eviction guard (`minAvailable: 1` pins the single-replica default pod through a drain/apply window). | Off = baseline. | `kubectl -n <ns> get pdb` |

There is no PVC and no RBAC: the server is stateless with no persistent
volumes, and it holds no Kubernetes permissions at all.

### 2.3 External dependencies — the egress prerequisite

| Dependency | Purpose | How to prove reachability from the cluster |
|---|---|---|
| **Outbound egress** (direct, or via the corporate proxy wired through `proxy.{http,https,noProxy}`) | The server's entire purpose: SearXNG's engine requests AND `fetch_content` both leave the cluster | From a pod in the cluster: `kubectl -n <ns> exec deploy/searxng-mcp -c searxng-mcp -- curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.com` — a proxy cluster also proves the proxy hop (expect 200, not a proxy error). **Prove it from the cluster, not your laptop** — a laptop-reachable route says nothing about the pod's egress path. |
| Upstream search engines (SearXNG's configured engines) | What `search` aggregates | One `search` tool call; read the `unresponsive_engines` note — engines that consistently fail from your egress belong in `searxng.disabledEngines` (see FEATURES.md fallback guidance) |
| The SearXNG sidecar itself (localhost:8080, bundled) | What `search` talks to | `kubectl -n <ns> exec deploy/searxng-mcp -- curl -s http://localhost:8080/config | head` — JSON format must be enabled (the chart's ConfigMap does this) |

## 3. Values

### 3.1 Required

| Key | Why it is required |
|---|---|
| `searxng.secretKey` (or `searxng.existingSecret`) | SearXNG session/crypto secret (`SEARXNG_SECRET`) — the deployment template errors when both are empty. Only the literal key is used when `searxng.existingSecret` is empty; the chart default is a placeholder. **Preferred:** pre-create a Secret and set `searxng.existingSecret` (rotation = update the Secret + restart); never put a real secret in a values file (see `helm/local/values.example.yaml`). |
| `ezua.virtualService.endpoint` | Full public hostname, e.g. `searxng-mcp.${DOMAIN_NAME}`; must be unique per release on the shared gateway. The VirtualService fails to render without it. Doubles as `MCP_HOSTNAME` — the DNS-rebinding Host pin (read once at server startup). |

`ezua.domainName` is informational only — **no template reads it** (the
gateway host comes solely from `endpoint`); set it for documentation
readability in site files, but nothing gates on it.

```yaml
searxng:
  # Preferred — the secret never passes through values files or git:
  existingSecret: "searxng-secret"
  # … or a literal (lab only): secretKey: "<SECRET_KEY>"   # openssl rand -hex 32
ezua:
  enabled: true
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "searxng-mcp.${DOMAIN_NAME}"
    istioGateway: "istio-system/ezaf-gateway"
    timeout: 660s
  authorizationPolicy:
    enabled: true
    namespace: "istio-system"
    providerName: "oauth2-proxy"
```

### 3.2 Optional (defaults are sensible)

**SearXNG engine behavior:**

| Key | Default | Meaning |
|---|---|---|
| `searxng.image.*` | `docker.io/searxng/searxng`, dated pin (`2026.9.8-3fdc6d753`) | Sidecar image; pinned to the dated tag `latest` resolved to when the chart was last touched (2026-09-08) — **never the mutable `latest` itself** (pods rescheduled later would silently get different engines/settings). Refresh the pin by editing the tag and re-applying; digest form = fully immutable. |
| `searxng.baseUrl` | `""` | Public base URL rendered into `settings.yml` (`server.base_url`). The JSON API works without it; set `https://<endpoint>/` for correct UI links. |
| `searxng.language` | `en-US` | Default language/region for the MCP server (`SEARXNG_LANGUAGE`). |
| `searxng.disabledEngines` | `[]` (chart default: the corporate-proxy dead-engine list) | Engines to disable via `settings.yml` — see FEATURES.md fallback guidance for when to keep/widen/empty this list. Names must match engine names exactly (`GET /config`); a wrong name is silently ignored. |
| `searxng.resources` | 100m/256Mi → 1 CPU/1Gi | SearXNG sidecar sizing. |

**Fetch/browser behavior:**

| Key | Default | Meaning |
|---|---|---|
| `browser.enabled` | `false` | Adds the headless-browser sidecar (Playwright Chromium) so `fetch_content` can render JS-only pages and take screenshots. Costs ~512Mi and a second image; plain HTTP + curl_cffi already covers most sites. |
| `browser.image.*` | `ghcr.io/ai-solution-eng/searxng-mcp-browser` / `v1.7.1` | Sidecar image; build/push first if you maintain your own (see `browser/Dockerfile`). |
| `browser.resources` | 250m/512Mi → — /1536Mi | Sidecar sizing; the 1536Mi limit absorbs rendering bursts that OOM-killed the sidecar at 1Gi (2026-10-27). |
| `browser.caCert.enabled` / `.configMap` | `false` / `ezaf-root-ca` | Installs a corporate MITM CA into the Chromium trust store at startup — see §2.2 for the ConfigMap prerequisites and the NSS-DB detail. |
| `browser.ignoreCertErrors` | `false` | Escape hatch: skip TLS verification in rendered contexts (`BROWSER_IGNORE_CERT_ERRORS`). Use only when the proxy's CA cannot be distributed; the `caCert` bundle is the proper fix. |
| `browser.extraArgs` | `""` | Extra Chromium command-line flags. |
| `caCert.enabled` / `.configMap` | `false` / `zscaler-root-ca` | The MCP container's answer to TLS-intercepting egress — see §2.2. The `init-ca-bundle` init container concatenates EVERY `.crt` in the ConfigMap with the image's own public bundle (certifi) into one file — no key selection — and the server clears ONLY Python 3.13+'s strict-encoding flag for legacy roots (the 2014 Zscaler Root CA's `basicConstraints` is not marked critical); signatures, validity windows and hostname checks still fully apply. |
| `fetch.tlsInsecureFallback` | `""` (off) | Last-resort resilience: when a fetch rung fails on a **certificate-verification** error specifically (not HTTP/DNS/policy), it retries ONCE unverified and the tool output is marked. Consider `true` where the CA bundle may lag the proxy's root rotation. |
| `fetch.*` | `""` (built-in defaults) | `fetch_content` SSRF-guard escapes/caps (fleet decision D6 — the guard is default-ON in the server): `allowHosts`, `denyExtra`, `cacheTtl`, `cacheMaxBytes`, `cacheMaxEntries`, `cacheMaxEntryBytes`, `maxBodyBytes`, `maxScreenshotKb`, `maxRedirects`. Wired to the `SEARXNG_FETCH_*` envs; loopback/pod-local/metadata targets are never fetchable regardless. Entries larger than the per-entry cache cap are returned but never cached. |
| `search.*` | `""` (built-in defaults) | Search result cache knobs wired to the `SEARXNG_SEARCH_*` envs: `cacheTtl` (s, `0` disables), `cacheMaxBytes`, `cacheMaxEntries`, `cacheMaxEntryBytes`. |

**Workload, exposure, auth:**

| Key | Default | Meaning |
|---|---|---|
| `deployment.*` | `searxng-mcp`, 1 replica | Workload naming and scale. The MCP server is stateless — scale replicas freely. |
| `image.repository` / `tag` | `ghcr.io/ai-solution-eng/searxng-mcp` / `v1.7.1` | Keep `tag` in lockstep with the chart's `appVersion` — a stale default is how an "old MCP + new chart" pod happens. |
| `imagePullSecrets` | `[]` | Only if the GHCR package stays private (public packages pull anonymously). |
| `resources` | 200m/200Mi → 1 CPU/1Gi | MCP container sizing; the 1Gi limit is burst headroom for large-page extraction — the cache byte bounds (`fetch.*` / `search.*` above) are the real memory fix (see FEATURES.md caching). |
| `service.port` / `searxngPort` | 9090 / 8080 | MCP (`/mcp`) and SearXNG web UI ports. |
| `proxy.{http,https,noProxy}` | `{}` (all off) | Per-key egress proxy wiring — each key is injected only when non-empty: `http`/`https` → `HTTP(S)_PROXY` env on BOTH containers, `noProxy` → `NO_PROXY`; a non-empty `https` additionally wires SearXNG's `outgoing.proxies` (`settings.yml`). **On corporate-proxy clusters set `http`/`https` (and usually `noProxy`)**; on direct-egress systems leave `proxy: {}` — a wrong proxy breaks DNS/egress. (The `hpe_proxies` flag was removed, chart ≥ 1.5.0.) |
| `extraAllowedHosts` | `[]` | EXTRA in-cluster Host allowlist additions for the SDK's DNS-rebinding protection, joined into `MCP_EXTRA_ALLOWED_HOSTS` (verbatim, or `host:*` for any port). `ezua.virtualService.endpoint` is wired as `MCP_HOSTNAME` (the public pin). With neither set, the SDK's implicit loopback-only protection applies. When transport security is active, the chart AUTO-prepends the release's own service DNS (`searxng-mcp-service.<ns>.svc.cluster.local:*` — the gateway relay's Host header; 421 Misdirected Request without it) as the first entry, so this key lists only hosts BEYOND that. Both envs are read once at server startup — changing them requires a restart. |
| `apiKey.existingSecret` / `.existingSecretKey` | `""` / `api-keys` | OPTIONAL API-key gate on `/mcp` — see §2.1. `/health`, the SearXNG UI route and the opt-in `/metrics` stay key-free. |
| `metrics.enabled` (+ `.serviceMonitor` / `.interval`) | `false` / `true` / `30s` | `GET /metrics` self-metrics on the MCP container — one counter family, `searxng_mcp_tool_requests_total{tool,outcome}`. Default OFF renders no env and no ServiceMonitor — the default render is byte-identical to the baseline. When on, `/metrics` rides the MCP port key-free and the ServiceMonitor scrapes it (requires prometheus-operator CRDs). |
| `networkPolicy.*` | `enabled: false` | The MCP network zone — see §2.2 for the sub-keys (`sameNamespace`, `ingressGatewayNamespace`/`ingressGatewayLabels`, `egress.*`). Hardened profile: `helm/values-examples/values-hardened-g2.yaml`. |
| `podDisruptionBudget.enabled` / `.minAvailable` | `false` / `1` | See §2.2. |
| `env` | `SEARXNG_URL=http://localhost:8080` | MCP container env. `SEARXNG_URL` points at the sidecar — leave it. Other server knobs (`SEARXNG_TIMEOUT`, `FETCH_REQUESTS_PER_MINUTE`, …) can be added here — note the config freeze (FEATURES.md): most are read once at startup. |

### 3.3 Chart contents (what the render produces)

- `deployment.yaml` — one pod: an init container that copies the settings
  ConfigMap into an `emptyDir` (SearXNG's entrypoint writes to
  `/etc/searxng`), the SearXNG sidecar (8080), the MCP server (9090), and —
  when `browser.enabled` — the headless-browser sidecar (loopback CDP).
- `configmap.yaml` — renders `settings.yml` from values: **JSON format
  enabled** (the MCP server uses `format=json`; SearXNG returns 403
  otherwise — the client surfaces a targeted hint when that happens),
  limiter off (no valkey needed), optional disabled-engine list, optional
  `outgoing.proxies` for the corporate proxy.
- `service.yaml` — ports `mcp` (9090) and `searxng` (8080).
- `virtualservice.yaml` — `/mcp` → 9090 (MCP), `/` → 8080 (the normal
  SearXNG web page), behind the EZAF gateway.
- `kyverno.yaml` — vendor label policy (pre-install hook, ungated).

## 4. Import and install (PCAI steps)

1. **Prove egress from the cluster** (§2.3) — this is the prerequisite that
   makes or breaks the server.
2. **Pre-create the Secrets** (§2.1): the SearXNG session Secret (preferred
   wiring) and, beyond a lab, the API-key Secret.
3. **On MITM-proxy clusters: stage the CA ConfigMap(s)** in the release
   namespace, including the proxy's own root (§2.2).
4. **Import the packaged chart once** into PCAI (AI Essentials /
   EzAppConfig import).
5. **Create the deployment from the catalog entry**; PCAI creates the
   namespace and opens the chart's Helm Values editor.
6. **Paste a full values document** — `helm/values-examples/values.g2.yaml`
   or `values.hosted-trial.yaml` (full documents; the editor replaces the
   chart's bundled values), adjust the `# SITE:` lines, keep
   `${DOMAIN_NAME}` as-is.
7. **Apply.** PCAI re-renders the ConfigMap and rolls the Deployment.
8. **Verify** per [VERIFICATION.md](VERIFICATION.md): two-path liveness,
   MCP handshake, one `search` call, one `fetch_content` call — and after
   any sidecar-image bump, validate `GET /config` (JSON format enabled,
   engine list as expected) plus one search call.

## 5. Upgrade / rollback / uninstall

Edit values, re-apply:

1. Change the values in the PCAI **Helm Values** editor (or re-submit via
   the PCAI API) — e.g. a new `image.tag` (keep it in lockstep with the
   chart's `appVersion`) or a refreshed `searxng.image.tag` pin.
2. Apply. PCAI re-renders the ConfigMap and rolls the Deployment.
3. Confirm the new pod is ready and re-run the [VERIFICATION](VERIFICATION.md)
   checks — after a sidecar-image bump, also validate `GET /config` (JSON
   format enabled, engine list as expected) and one search call.

**Rollback / uninstall (PCAI-way):** roll back by re-importing the previous
chart version and re-applying the same values. Uninstall = delete the
deployment from the PCAI UI; the operator-created Secrets stay until
deleted. **No migration steps exist** — the server is stateless with no
persistent volumes; the settings ConfigMap is re-rendered on every apply.

Plain-Helm equivalent (developer/secondary path):
`helm template searxng-mcp helm/ -f helm/local/values.<site>.yaml` to
eyeball, then `helm upgrade --install searxng-mcp helm/ -n searxng-mcp
--create-namespace -f ...`. NOT the PCAI path.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| SearXNG answers 403 to every search | `search.formats` missing `json` — the MCP server uses `format=json` | The chart's ConfigMap enables it; if you bring your own `settings.yml`, add `json` to `search.formats` (the client surfaces a targeted hint) |
| An engine you "disabled" still appears and fails | Overrides went under `search.engines:` — SearXNG's settings loader reads `engines:` only at the TOP LEVEL; a `search.engines` key is silently ignored (the pre-existing searxng namespace deployment made exactly this mistake and burned 12 days) | Move `engines:` to the top level of settings.yml; confirm via `GET /config` |
| Browser sidecar fails every render with `ERR_CERT_AUTHORITY_INVALID` | The CA ConfigMap lacks the egress proxy's own root (the cluster-wide `ezaf-root-ca` holds only public roots on G2), or the PEMs were never imported into the NSS DB | Put the proxy's root into the ConfigMap; keep `browser.caCert.enabled: true` so the entrypoint imports PEMs into `~/.pki/nssdb` (Chromium ≥ 105 ignores the Debian bundle); `browser.ignoreCertErrors` is the last resort, not the fix |
| `fetch_content` dies with `curl: (60) unable to get local issuer certificate` | TLS-intercepting egress and the MCP container's bundle lacks the proxy root | Enable `caCert.enabled` with a ConfigMap that carries public roots + the proxy CA; verification stays ON; `fetch.tlsInsecureFallback: true` only as a rotation-gap last resort (output is marked) |
| Searches degrade; `Note: some engines did not respond: …` on most queries | Upstream engines rate-limit/CAPTCHA-wall the shared egress IP; dead engines still burn the per-IP budget on every search | Disable the casualties via `searxng.disabledEngines` (chart default targets the corporate-egress dead); narrow agent `backend` calls; keep the search cache on (FEATURES.md fallback guidance) |
| `/mcp` answers 421 Misdirected Request on the gateway-relay hop | Transport security active and the relay's Host header (`searxng-mcp-service.<ns>.svc.cluster.local:*`) missing | The chart auto-prepends it when transport security is on; on a manual/plain-Helm render add it via `extraAllowedHosts` |
| Browser sidecar OOM-killed during renders | Rendering bursts exceeded the old 1Gi limit (2026-10-27) | Chart default is now 1536Mi; keep `browser.resources` limits at 1536Mi |
| Changed `SEARXNG_URL` (or a cache knob) in the running pod's env — nothing changed | Config freeze: the connection block and cache/cap knobs are read once at import/construct; only the API-key envs re-read live | Re-apply the values and let the pod restart; do not expect in-place env edits to take effect |
| `fetch_content` of an internal URL returns a policy error naming `SEARXNG_FETCH_ALLOW_HOSTS` | The SSRF guard (D6, default-ON) refused a resolved-internal target — working as designed | If the target is legitimate, add it to `fetch.allowHosts`; loopback/pod-local/metadata are never fetchable regardless |
| MCP tools 200 but every fetch fails with proxy errors | `proxy.*` wired to a dead/wrong proxy on a cluster that actually has direct egress (or vice versa) | `proxy: {}` on direct-egress builds; on proxy clusters set real addresses — a wrong proxy breaks DNS/egress |

## 7. Rebuilding the images (W7 fixes)

Site operators rarely rebuild, but the failure mode is worth knowing:
agents' files land `0600` on the ops box and `COPY` preserves modes, so a
rebuilt image could carry modules the non-root container cannot read — the
pod crashes on the first unreadable import (the same 0600-file crash class
the RAG server hit live). Both images now neutralize this at build time:
the MCP image runs `chmod -R a+rX /app` after its code `COPY` (and the
browser sidecar `chmod -R a+rX /ms-playwright`), and a build-time sanity
gate imports every declared module from the installed wheel — packaging
drift (`mcp_auth`/`mcp_metrics`/`url_policy` are loose py-modules; the
non-editable install silently omits one whose file is missing) fails in the
build log instead of as a first-query `ModuleNotFoundError` in a pod. If a
rebuilt pod crash-loops, check these two layers before touching values.

## 8. Deployment profiles: Internal G2 vs Hosted trial

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized, secret-free; real per-site values
live in `helm/local/`). Workflow: pick the matching example file, paste it
whole into the PCAI **Helm Values** editor (these are full documents — the
editor replaces the chart's bundled values), adjust the `# SITE:` lines,
apply.

| Posture key | Proxied corporate site (SE-G2) | Hosted trial (customer-hosted PCAI) |
|---|---|---|
| `${DOMAIN_NAME}` | Literal domain in the values (paste-ready `helm -f` readability) | Placeholders kept — PCAI resolves them before rendering |
| Egress `proxy.{http,https,noProxy}` | Explicit block wired (engines + `fetch_content` both ride it) | Wired too (trial clusters have no direct egress) — `proxy: {}` only on a direct-egress build |
| MITM CA | `browser.caCert.enabled: true` (`ezaf-root-ca` ConfigMap, must also carry the proxy's root) | `browser.caCert.enabled: false` unless that cluster sits behind a TLS-intercepting proxy (then also consider the top-level `caCert` bundle for the MCP container) |
| Browser sidecar | `browser.enabled: true` (JS rendering + screenshots) | `browser.enabled: false` (costs a second image + ~512Mi; plain HTTP + curl_cffi covers most sites) |
| API key | `apiKey.existingSecret: mcp-fleet-apikeys` (fleet Secret, key `api-keys` — create it before the deploy) | Same fleet-Secret convention, or the customer's own Secret name |
| Gateway auth | `ezua.authorizationPolicy.enabled: true` (oauth2-proxy at the ezaf-gateway) | Same block — decide per trial posture; the server itself does no client auth |
| Metrics | `metrics.enabled: true` + ServiceMonitor | `metrics.enabled: true` in the shipped example — set `false` if the trial's Prometheus stack lacks the ServiceMonitor CRD |
| Engine disable-list | `[]` (the example relies on auto-suspension; widen the chart's corporate dead-engine default only if the site's evidence demands it) | `[]` (same) |

### Proxied corporate site (SITE: your-cluster.example)

- Literal domain, corporate proxy wired into BOTH the SearXNG sidecar
  (`settings.yml` `outgoing.proxies`) and the MCP container's
  `fetch_content` env; browser sidecar + its MITM CA on.
- The dead-engine question is site-specific: the chart default disables the
  corporate-proxy casualties (ddg/brave/startpage variants + `wikidata`);
  the shipped G2 example resets to `[]` — on a shared egress IP dead
  engines burn the per-IP rate budget on every search, so keep the chart
  default unless the site's `GET /config` evidence says otherwise.
- Sanitized example: [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

### Hosted trial (customer-hosted PCAI)

- `${DOMAIN_NAME}` placeholders stay as-is (PCAI resolves them before
  rendering; substitute the literal domain only when render-checking
  outside PCAI — an unresolved placeholder registers a gateway host that
  matches nothing).
- Keep the secret off the values path: `searxng.existingSecret` at a
  pre-created Secret (the chart errors loudly if neither it nor a literal
  `secretKey` is set).
- Browser sidecar off by default; enable per site with the sidecar image
  built/pushed first (`browser/Dockerfile`).
- Sanitized example: [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).

## See also

- [`../helm/values-examples/`](../helm/values-examples/README.md) — paste-ready full-values examples (G2 lab cluster — sanitized from the real site file — and hosted trial).
- [`../helm/values.yaml`](../helm/values.yaml) — every key with its default and inline comments.
