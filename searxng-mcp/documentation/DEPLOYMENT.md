# DEPLOYMENT — searxng-mcp on PCAI

> **What changed (2026-10-07 doc wave):** the **Internal G2 vs Hosted trial**
> profile section is NEW — this guide previously had no per-target comparison;
> the `apiKey.*` (optional key gate), `metrics.*` and `networkPolicy` chart
> keys are now documented (they existed without walkthrough rows); stale image
> tags were corrected (chart/image `v1.4.0` → `v1.7.0`, browser sidecar
> `v1.6.1`); `ezua.domainName` is demoted from "required" to informational
> (no template reads it); and a new "Rebuilding the images" note documents the
> W7 build-time permission neutralizer (`chmod -R a+rX` after COPY — agents'
> files land `0600` on the ops box and COPY preserves modes, so non-root
> containers used to crash on the first unreadable module).

Deployment on PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) is values-driven: import the packaged `searxng-mcp` chart into the PCAI catalog once, create the deployment from it, and set every knob in the chart's **Helm Values** editor (or via the PCAI API). PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering. There is deliberately no `helm install`/`kubectl apply` runbook here — that is not how PCAI deployments happen.

## Values walkthrough (from `helm/values.yaml`)

### Required

| Key | Why it is required |
|---|---|
| `searxng.secretKey` (or `searxng.existingSecret`) | SearXNG session/crypto secret (`SEARXNG_SECRET`) — the deployment template errors when both are empty. Only the literal key is used when `searxng.existingSecret` is empty; the chart default is a placeholder. **Preferred:** pre-create a Secret and set `searxng.existingSecret` (rotation = update the Secret + restart); never put a real secret in a values file (see `helm/local/values.example.yaml`). |
| `ezua.virtualService.endpoint` | Full public hostname, e.g. `searxng-mcp.${DOMAIN_NAME}`; must be unique per release on the shared gateway. The VirtualService fails to render without it (gated on `ezua.enabled` + `ezua.virtualService.enabled`). Doubles as `MCP_HOSTNAME` — the DNS-rebinding Host pin (read once at server startup). |

`ezua.domainName` is informational only — **no template reads it** (the
gateway host comes solely from `endpoint`); set it for documentation
readability in site files, but nothing gates on it.

Minimal required-values document:

```yaml
searxng:
  # Preferred — the secret never passes through values files or git:
  existingSecret: "searxng-secret"       # kubectl create secret generic searxng-secret --from-literal=secret=$(openssl rand -hex 32)
  existingSecretKey: secret
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

### Optional (defaults are sensible)

| Key | Default | Meaning |
|---|---|---|
| `ezua.enabled` | `true` | `false` skips the VirtualService (in-cluster-only exposure); the hardened-admin `ezua.virtualService.enabled: false` unrenders just the VS while `ezua.enabled` keeps driving the other gates. |
| `deployment.*` | `searxng-mcp`, 1 replica | Workload naming and scale. The MCP server is stateless — scale replicas freely. |
| `image.repository` / `tag` | `ghcr.io/ai-solution-eng/searxng-mcp` / `v1.7.0` | Keep `tag` in lockstep with the chart's `appVersion` (`v1.7.0`) — a stale default is how an "old MCP + new chart" pod happens. |
| `imagePullSecrets` | `[]` | Only if the GHCR package stays private (public packages pull anonymously). |
| `searxng.image.*` | `docker.io/searxng/searxng`, dated pin (`2026.9.8-3fdc6d753`) | SearXNG sidecar image; pinned to the dated tag `latest` resolved to when the chart was last touched (2026-09-08) — never the mutable `latest` itself. Refresh the pin by editing the tag and re-applying. |
| `searxng.baseUrl` | `""` | Public base URL rendered into `settings.yml` (`server.base_url`). The JSON API works without it; set `https://<endpoint>/` for correct UI links. |
| `searxng.language` | `en-US` | Default language/region for the MCP server (`SEARXNG_LANGUAGE`). |
| `searxng.disabledEngines` | `[]` (chart default: the corporate-proxy dead-engine list) | Engines to disable via `settings.yml`. The chart's default list drops engines with a track record of dying behind corporate egress proxies (ddg/brave/startpage variants + `wikidata` — CAPTCHA walls / SPARQL timeouts on a shared egress IP); on a shared egress IP dead engines still burn the per-IP rate budget on every search, so keeping the list is a quota save, not a latency nicety. Set `[]` only on open-internet (non-proxied) installs. Names must match engine names exactly (`GET /config`); a wrong name is silently ignored. |
| `searxng.resources` | 100m/256Mi → 1 CPU/1Gi | SearXNG sidecar sizing. |
| `service.port` / `searxngPort` | 9090 / 8080 | MCP (`/mcp`) and SearXNG web UI ports. |
| `resources` | 200m/200Mi → 1 CPU/512Mi | MCP container sizing. |
| `browser.enabled` | `false` | Adds the headless-browser sidecar (Playwright Chromium) so `fetch_content` can render JS-only pages and take screenshots. Costs ~512Mi and a second image; plain HTTP + curl_cffi already covers most sites. |
| `browser.image.*` | `ghcr.io/ai-solution-eng/searxng-mcp-browser` / `v1.6.1` | Sidecar image; build/push first if you maintain your own (see `browser/Dockerfile`). |
| `browser.caCert.enabled` / `.configMap` | `false` / `ezaf-root-ca` | Installs a corporate MITM CA into the Chromium trust store at startup (Chromium ignores `SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`). The ConfigMap must exist in the release namespace — copy the cluster-wide one if needed. **Note (2026-10-04, SE-G2): the cluster-wide `ezaf-root-ca` holds only public root CAs** — the egress proxy's own root (Zscaler) must be in the ConfigMap too, or the browser still fails with `ERR_CERT_AUTHORITY_INVALID`. **And Chromium ≥ 105 (playwright headless_shell) does not read the Debian system bundle** — the sidecar imports the mounted PEMs into the NSS DB `~/.pki/nssdb` (needs `libnss3-tools` in the image; entrypoint 2026-10-04). |
| `browser.ignoreCertErrors` | `false` | Escape hatch: skip TLS verification in rendered contexts (`BROWSER_IGNORE_CERT_ERRORS`). Use only when the proxy's CA cannot be distributed; the `caCert` bundle is the proper fix. |
| `caCert.enabled` / `.configMap` | `false` / `zscaler-root-ca` | The MCP container's answer to TLS-intercepting egress (Zscaler-style proxies re-sign every certificate — without the proxy root, fetch_content dies with `curl: (60) unable to get local issuer certificate`). Mounts the ConfigMap's PEMs and combines them with the image's public bundle into `/ca-bundle/ca-bundle.crt` (init container `init-ca-bundle`), wired to `FETCH_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`. Verification stays **ON** — the bundle must carry public roots + the proxy's CA (OpenSSL replaces the default store with the bundle). **Legacy roots:** Python 3.13+'s default X.509-strict rejects the 2014 Zscaler Root CA (its `basicConstraints` is not marked critical — "Basic Constraints of CA cert not marked critical"); when a bundle is configured the server clears ONLY that strict-encoding flag — signatures, validity windows and hostname checks still fully apply. |
| `fetch.tlsInsecureFallback` | `""` (off) | Last-resort resilience: when a fetch rung fails on a **certificate-verification** error specifically (not HTTP/DNS/policy), it retries ONCE unverified and the tool output is marked. Consider `true` where the CA bundle may lag the proxy's root rotation. |
| `browser.extraArgs` | `""` | Extra Chromium command-line flags. |
| `env` | `SEARXNG_URL=http://localhost:8080` | MCP container env. `SEARXNG_URL` points at the sidecar — leave it. Other server knobs (`SEARXNG_TIMEOUT`, `FETCH_REQUESTS_PER_MINUTE`, …) can be added here. |
| `fetch.*` | `""` (built-in defaults) | `fetch_content` SSRF-guard escapes/caps (fleet decision D6 — the guard is default-ON in the server): `allowHosts` (internal hosts/CIDRs to permit), `denyExtra`, `cacheTtl`, `maxBodyBytes`, `maxScreenshotKb`, `maxRedirects`. Wired to the `SEARXNG_FETCH_*` envs; loopback/pod-local/metadata targets are never fetchable regardless. See README "fetch_content SSRF guard". |
| `proxy.{http,https,noProxy}` | `{}` (all off) | Per-key egress proxy wiring — each key is injected only when non-empty: `http`/`https` → `HTTP(S)_PROXY` env on BOTH containers, `noProxy` → `NO_PROXY`; a non-empty `https` additionally wires SearXNG's `outgoing.proxies` (`settings.yml`). **On corporate-proxy clusters set `http`/`https` (and usually `noProxy`)**; on direct-egress systems leave `proxy: {}` — a wrong proxy breaks DNS/egress. |
| `extraAllowedHosts` | `[]` | EXTRA in-cluster Host allowlist additions for the SDK's DNS-rebinding protection, joined into `MCP_EXTRA_ALLOWED_HOSTS` (entries verbatim, or `host:*` for any port). `ezua.virtualService.endpoint` is wired as `MCP_HOSTNAME` (the public pin). With neither set, the SDK's implicit loopback-only protection applies — behaviour unchanged. When transport security is active, the chart AUTO-prepends the release's own service DNS (`<deployment.name>-service.<ns>.svc.cluster.local:*` — the gateway relay's Host header) as the first entry, so this key lists only hosts BEYOND that. Both envs are read once at server startup — changing them requires a restart. |
| `apiKey.existingSecret` / `.existingSecretKey` | `""` / `api-keys` | OPTIONAL API-key gate on `/mcp` (fleet decision 2026-09): rendered (`SEARXNG_API_KEYS`, or the fleet-universal `MCP_API_KEYS` — either authenticates) ONLY when the values point at a pre-deployed Secret — `/mcp` then requires a key. Empty (default) = the server runs open with a loud startup warning. **Recommended beyond a lab** — the fetch tools can reach in-cluster URLs, so unauthenticated in-cluster callers are a real surface. The chart NEVER creates the Secret and never inlines a key; comma-separated keys (`api-keys=new,old`) are the zero-downtime rotation (env re-read per request). `/health`, the SearXNG UI route and the opt-in `/metrics` stay key-free. |
| `metrics.enabled` (+ `.serviceMonitor` / `.interval`) | `false` / `true` / `30s` | `GET /metrics` self-metrics on the MCP container — one counter family, `searxng_mcp_tool_requests_total{tool,outcome}` (no queries, URLs, or error text exported). Default OFF renders no env and no ServiceMonitor and the server serves no `/metrics` route — the default render is byte-identical to the baseline. When on, `/metrics` rides the MCP port key-free (the key gate scopes to `/mcp` only) and the ServiceMonitor scrapes it (requires prometheus-operator CRDs). |
| `networkPolicy.*` | `enabled: false` | The MCP network zone (ADDITIVE, default OFF — the default render is byte-identical to the baseline): ingress default-deny admitting only `authorizedClients.namespaces` (+ same-namespace pods, `probeCidrs` kubelet probes, and the edge-gateway pods while `allowEzafGatewayIngress: true` — fleet doctrine keeps the browser path open). Keep `monitoring` listed once metrics are on, or scraping dies silently. Full recipe: README "Enabling the network zone"; hardened profile: `helm/values-examples/values-hardened-g2.yaml`. |

### Migrating from hpe_proxies

The `hpe_proxies` flag was removed (chart ≥ 1.5.0) — the per-key `proxy:` dict replaced it. Set `proxy.http` / `proxy.https` / `proxy.noProxy` directly; each key is active only when non-empty, and `proxy: {}` is fully off. Former `hpe_proxies: true` sites must now write the explicit proxy block (the chart no longer carries built-in HPE proxy defaults in shipped values).

## ezua / Istio wiring

When `ezua.enabled: true` the chart renders:

- **VirtualService** `<name>-vs` on gateway `ezua.virtualService.istioGateway` (default `istio-system/ezaf-gateway`) with two routes: `/mcp` → the MCP server (9090), everything else (`/`) → the SearXNG web UI (8080), both with `timeout: 660s`.
- **Kyverno pre-install ClusterPolicy** stamping `hpe-ezua/type: vendor-service` and `hpe-ezua/app: searxng-mcp` labels — the marker PCAI uses to recognize vendor services.

The values carry an `ezua.authorizationPolicy` block (`enabled`, `namespace: istio-system`, `providerName: oauth2-proxy`) for the PCAI gateway-gate convention. Note this chart ships **no** AuthorizationPolicy template itself — gateway-side authentication for the exposed host is managed at the PCAI/gateway level; the server performs no client auth of its own, and in-cluster callers reach `/mcp` directly without the gateway.

## Rebuilding the images (W7 fix — build-time permission neutralizer, 2026-10)

Site operators rarely rebuild, but the failure mode is worth knowing: agents'
files land `0600` on the ops box and `COPY` preserves modes, so a rebuilt
image could carry modules the non-root container cannot read — the pod
crashes on the first unreadable import (the same 0600-file crash class the
RAG server hit live). Both images now neutralize this at build time: the MCP
image runs `chmod -R a+rX /app` after its code `COPY` (and the browser
sidecar `chmod -R a+rX /ms-playwright`), and a build-time sanity gate imports
every declared module from the installed wheel — packaging drift
(`mcp_auth`/`mcp_metrics`/`url_policy` are loose py-modules; the non-editable
install silently omits one whose file is missing) fails in the build log
instead of as a first-query `ModuleNotFoundError` in a pod. If a rebuilt pod
crash-loops, check these two layers before touching values.

## Deployment profiles: Internal G2 vs Hosted trial

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized, secret-free; real per-site values live
in `helm/local/`). Workflow: pick the matching example file, paste it whole
into the PCAI **Helm Values** editor (these are full documents — the editor
replaces the chart's bundled values), adjust the `# SITE:` lines, apply.

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
  the shipped G2 example resets to `[]` — on a shared egress IP dead engines
  burn the per-IP rate budget on every search, so keep the chart default
  unless the site's `GET /config` evidence says otherwise.
- Sanitized example:
  [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

### Hosted trial (customer-hosted PCAI)

- `${DOMAIN_NAME}` placeholders stay as-is (PCAI resolves them before
  rendering; substitute the literal domain only when render-checking outside
  PCAI — an unresolved placeholder registers a gateway host that matches
  nothing).
- Keep the secret off the values path: `searxng.existingSecret` at a
  pre-created Secret (the chart errors loudly if neither it nor a literal
  `secretKey` is set).
- Browser sidecar off by default; enable per site with the sidecar image
  built/pushed first (`browser/Dockerfile`).
- Sanitized example:
  [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).

## Upgrading

Edit values, re-apply:

1. Change the values in the PCAI **Helm Values** editor (or re-submit via the PCAI API) — e.g. a new `image.tag` (keep it in lockstep with the chart's `appVersion`) or a refreshed `searxng.image.tag` pin.
2. Apply. PCAI re-renders the ConfigMap and rolls the Deployment.
3. Confirm the new pod is ready and re-run the [VERIFICATION](VERIFICATION.md) checks — after a sidecar-image bump, also validate `GET /config` (JSON format enabled, engine list as expected) and one search call.

No migration steps exist — the server is stateless with no persistent volumes; the settings ConfigMap is re-rendered on every apply.

## See also

- [`../helm/values-examples/`](../helm/values-examples/README.md) — paste-ready full-values examples (G2 lab cluster — sanitized from the real site file — and hosted trial).
- [`../helm/values.yaml`](../helm/values.yaml) — every key with its default and inline comments.
