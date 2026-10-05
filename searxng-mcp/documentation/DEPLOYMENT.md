# DEPLOYMENT — searxng-mcp on PCAI

Deployment on PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) is values-driven: import the packaged `searxng-mcp` chart into the PCAI catalog once, create the deployment from it, and set every knob in the chart's **Helm Values** editor (or via the PCAI API). PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering. There is deliberately no `helm install`/`kubectl apply` runbook here — that is not how PCAI deployments happen.

## Values walkthrough (from `helm/values.yaml`)

### Required

| Key | Why it is required |
|---|---|
| `ezua.domainName` | Cluster domain; use `${DOMAIN_NAME}` — PCAI substitutes it. |
| `ezua.virtualService.endpoint` | Full public hostname, e.g. `searxng-mcp.${DOMAIN_NAME}`; must be unique per release on the shared gateway. The VirtualService fails to render without it. Doubles as `MCP_HOSTNAME` — the DNS-rebinding Host pin (read once at server startup). |
| `searxng.secretKey` | SearXNG session/crypto secret (`SEARXNG_SECRET`) — only used when `searxng.existingSecret` is empty. The chart default is a placeholder. **Preferred:** pre-create a Secret and set `searxng.existingSecret` (rotation = update the Secret + restart); never put a real secret in a values file (see `helm/local/values.example.yaml`). |

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
| `deployment.*` | `searxng-mcp`, 1 replica | Workload naming and scale. The MCP server is stateless — scale replicas freely. |
| `image.repository` / `tag` | `ghcr.io/ai-solution-eng/searxng-mcp` / `v1.4.0` | Keep `tag` in lockstep with the chart's `appVersion` — a stale default is how an "old MCP + new chart" pod happens. |
| `imagePullSecrets` | `[]` | Only if the GHCR package stays private (public packages pull anonymously). |
| `searxng.image.*` | `docker.io/searxng/searxng`, dated pin | SearXNG sidecar image; pinned to the dated tag `latest` resolved to at chart update time. Refresh the pin by editing the tag and re-applying. |
| `searxng.baseUrl` | `""` | Public base URL rendered into `settings.yml` (`server.base_url`). The JSON API works without it; set `https://<endpoint>/` for correct UI links. |
| `searxng.language` | `en-US` | Default language/region for the MCP server (`SEARXNG_LANGUAGE`). |
| `searxng.disabledEngines` | `[]` (chart default: the corporate-proxy dead-engine list) | Engines to disable via `settings.yml`. The chart's default list drops engines with a track record of dying behind corporate egress proxies (ddg/brave/startpage variants + `wikidata` — CAPTCHA walls / SPARQL timeouts on a shared egress IP); on a shared egress IP dead engines still burn the per-IP rate budget on every search, so keeping the list is a quota save, not a latency nicety. Set `[]` only on open-internet (non-proxied) installs. Names must match engine names exactly (`GET /config`); a wrong name is silently ignored. |
| `searxng.resources` | 100m/256Mi → 1 CPU/1Gi | SearXNG sidecar sizing. |
| `service.port` / `searxngPort` | 9090 / 8080 | MCP (`/mcp`) and SearXNG web UI ports. |
| `resources` | 200m/200Mi → 1 CPU/512Mi | MCP container sizing. |
| `browser.enabled` | `false` | Adds the headless-browser sidecar (Playwright Chromium) so `fetch_content` can render JS-only pages and take screenshots. Costs ~512Mi and a second image; plain HTTP + curl_cffi already covers most sites. |
| `browser.image.*` | `ghcr.io/ai-solution-eng/searxng-mcp-browser` / `v1.4.0` | Sidecar image; build/push first if you maintain your own (see `browser/Dockerfile`). |
| `browser.caCert.enabled` / `.configMap` | `false` / `ezaf-root-ca` | Installs a corporate MITM CA into the Chromium trust store at startup (Chromium ignores `SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`). The ConfigMap must exist in the release namespace — copy the cluster-wide one if needed. **Note (2026-10-04, SE-G2): the cluster-wide `ezaf-root-ca` holds only public root CAs** — the egress proxy's own root (Zscaler) must be in the ConfigMap too, or the browser still fails with `ERR_CERT_AUTHORITY_INVALID`. **And Chromium ≥ 105 (playwright headless_shell) does not read the Debian system bundle** — the sidecar imports the mounted PEMs into the NSS DB `~/.pki/nssdb` (needs `libnss3-tools` in the image; entrypoint 2026-10-04). |
| `browser.ignoreCertErrors` | `false` | Escape hatch: skip TLS verification in rendered contexts (`BROWSER_IGNORE_CERT_ERRORS`). Use only when the proxy's CA cannot be distributed; the `caCert` bundle is the proper fix. |
| `caCert.enabled` / `.configMap` | `false` / `zscaler-root-ca` | The MCP container's answer to TLS-intercepting egress (Zscaler-style proxies re-sign every certificate — without the proxy root, fetch_content dies with `curl: (60) unable to get local issuer certificate`). Mounts the ConfigMap's PEMs and combines them with the image's public bundle into `/ca-bundle/ca-bundle.crt` (init container `init-ca-bundle`), wired to `FETCH_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`. Verification stays **ON** — the bundle must carry public roots + the proxy's CA (OpenSSL replaces the default store with the bundle). **Legacy roots:** Python 3.13+'s default X.509-strict rejects the 2014 Zscaler Root CA (its `basicConstraints` is not marked critical — "Basic Constraints of CA cert not marked critical"); when a bundle is configured the server clears ONLY that strict-encoding flag — signatures, validity windows and hostname checks still fully apply. |
| `fetch.tlsInsecureFallback` | `""` (off) | Last-resort resilience: when a fetch rung fails on a **certificate-verification** error specifically (not HTTP/DNS/policy), it retries ONCE unverified and the tool output is marked. Consider `true` where the CA bundle may lag the proxy's root rotation. |
| `browser.extraArgs` | `""` | Extra Chromium command-line flags. |
| `env` | `SEARXNG_URL=http://localhost:8080` | MCP container env. `SEARXNG_URL` points at the sidecar — leave it. Other server knobs (`SEARXNG_TIMEOUT`, `FETCH_REQUESTS_PER_MINUTE`, …) can be added here. |
| `fetch.*` | `""` (built-in defaults) | `fetch_content` SSRF-guard escapes/caps (fleet decision D6 — the guard is default-ON in the server): `allowHosts` (internal hosts/CIDRs to permit), `denyExtra`, `cacheTtl`, `maxBodyBytes`, `maxScreenshotKb`, `maxRedirects`. Wired to the `SEARXNG_FETCH_*` envs; loopback/pod-local/metadata targets are never fetchable regardless. See README "fetch_content SSRF guard". |
| `proxy.{http,https,noProxy}` | `{}` (all off) | Per-key egress proxy wiring — each key is injected only when non-empty: `http`/`https` → `HTTP(S)_PROXY` env on BOTH containers, `noProxy` → `NO_PROXY`; a non-empty `https` additionally wires SearXNG's `outgoing.proxies` (`settings.yml`). **On corporate-proxy clusters set `http`/`https` (and usually `noProxy`)**; on direct-egress systems leave `proxy: {}` — a wrong proxy breaks DNS/egress. |
| `extraAllowedHosts` | `[]` | EXTRA in-cluster Host allowlist additions for the SDK's DNS-rebinding protection, joined into `MCP_EXTRA_ALLOWED_HOSTS` (entries verbatim, or `host:*` for any port). `ezua.virtualService.endpoint` is wired as `MCP_HOSTNAME` (the public pin). With neither set, the SDK's implicit loopback-only protection applies — behaviour unchanged. When transport security is active, the chart AUTO-prepends the release's own service DNS (`<deployment.name>-service.<ns>.svc.cluster.local:*` — the gateway relay's Host header) as the first entry, so this key lists only hosts BEYOND that. Both envs are read once at server startup — changing them requires a restart. |

### Migrating from hpe_proxies

The `hpe_proxies` flag was removed (chart ≥ 1.5.0) — the per-key `proxy:` dict replaced it. Set `proxy.http` / `proxy.https` / `proxy.noProxy` directly; each key is active only when non-empty, and `proxy: {}` is fully off. Former `hpe_proxies: true` sites must now write the explicit proxy block (the chart no longer carries built-in HPE proxy defaults in shipped values).
| `ezua.enabled` | `true` | `false` skips the VirtualService (in-cluster-only exposure). |

## ezua / Istio wiring

When `ezua.enabled: true` the chart renders:

- **VirtualService** `<name>-vs` on gateway `ezua.virtualService.istioGateway` (default `istio-system/ezaf-gateway`) with two routes: `/mcp` → the MCP server (9090), everything else (`/`) → the SearXNG web UI (8080), both with `timeout: 660s`.
- **Kyverno pre-install ClusterPolicy** stamping `hpe-ezua/type: vendor-service` and `hpe-ezua/app: searxng-mcp` labels — the marker PCAI uses to recognize vendor services.

The values carry an `ezua.authorizationPolicy` block (`enabled`, `namespace: istio-system`, `providerName: oauth2-proxy`) for the PCAI gateway-gate convention. Note this chart ships **no** AuthorizationPolicy template itself — gateway-side authentication for the exposed host is managed at the PCAI/gateway level; the server performs no client auth of its own, and in-cluster callers reach `/mcp` directly without the gateway.

## Upgrading

Edit values, re-apply:

1. Change the values in the PCAI **Helm Values** editor (or re-submit via the PCAI API) — e.g. a new `image.tag` (keep it in lockstep with the chart's `appVersion`) or a refreshed `searxng.image.tag` pin.
2. Apply. PCAI re-renders the ConfigMap and rolls the Deployment.
3. Confirm the new pod is ready and re-run the [VERIFICATION](VERIFICATION.md) checks — after a sidecar-image bump, also validate `GET /config` (JSON format enabled, engine list as expected) and one search call.

No migration steps exist — the server is stateless with no persistent volumes; the settings ConfigMap is re-rendered on every apply.

## See also

- [`../helm/values-examples/`](../helm/values-examples/README.md) — paste-ready full-values examples (G2 lab cluster — sanitized from the real site file — and hosted trial).
- [`../helm/values.yaml`](../helm/values.yaml) — every key with its default and inline comments.
