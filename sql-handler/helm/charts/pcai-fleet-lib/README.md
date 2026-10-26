# pcai-fleet-lib — the MCP fleet's shared Helm LIBRARY chart

Named templates for the boilerplate the fleet's eight charts previously
byte-copied between them (audit §3.5: "same template set byte-copied ×8, only
K8S-MCP has `_helpers.tpl`"). `type: library` — it renders **nothing** on its
own and ships no resources; consuming charts call the templates with `include`
and keep full control of their rendered bytes.

Design rule the templates encode: **the library owns formatting, the chart
owns semantics.** Every knob (paths, ports, timings, gate flags) is passed in
explicitly by the caller — the library never invents defaults that could
silently change a chart's render. The one exception is a small set of
documented fallbacks (indent 10, `/metrics`, `30s`, `oauth2-proxy`,
`RuntimeDefault`) that mirror what every fleet chart already uses.

## Consuming (dependency + vendoring)

In the consuming chart's `Chart.yaml` (path is relative to that chart dir):

```yaml
dependencies:
  - name: pcai-fleet-lib
    version: 0.1.0
    repository: file://../../pcai-solutions/tools/pcai-helm-port/chart/pcai-fleet-lib
```

Then vendor so the chart is self-contained and `helm template` never depends
on a sibling checkout being present:

```bash
helm dep build <chart-dir>   # → <chart-dir>/Chart.lock + charts/pcai-fleet-lib-<ver>.tgz
```

Commit both `Chart.lock` and the vendored `charts/*.tgz`. A `type: library`
dependency contributes **zero rendered bytes** — adding it does not change a
chart's render by itself, which is what makes per-concern byte-identical
adoption possible.

## Template reference

All templates take a **single dict argument** (an `include` receives only its
argument, never the parent's context — pass `.Values.*`, `.Release.*`,
`.Chart.*` explicitly). Multi-doc templates return `""` when disabled.
Field-line templates return only the indented field lines; the caller writes
the parent YAML key and applies `nindent`.

### `pcai-fleet-lib.probes` — liveness/readiness/startup block

Encodes every probe shape the fleet renders today: the MCP-trio httpGet
pattern (workbench/applygate/logsearch/prometheus), SQLhandler's /health +
/ready pattern (including its quirk of taking the startup probe's timeout from
`livenessProbe.timeoutSeconds`), and the tcpSocket pattern (K8S-MCP, searxng).
Probes are rendered in **caller order**; a falsy `enabled` drops the probe
entirely (matching today's `{{- if }}` gating).

```yaml
{{- include "pcai-fleet-lib.probes" (list
    (dict "name" "startupProbe" "enabled" .Values.startupProbe.enabled
          "httpGet" (dict "path" "/health" "port" "http")
          "fields" (list
            (dict "key" "periodSeconds" "value" .Values.startupProbe.periodSeconds)
            (dict "key" "timeoutSeconds" "value" .Values.livenessProbe.timeoutSeconds)
            (dict "key" "failureThreshold" "value" .Values.startupProbe.failureThreshold)))
    (dict "name" "livenessProbe" "enabled" .Values.livenessProbe.enabled
          "httpGet" (dict "path" "/health" "port" "http")
          "fields" (list
            (dict "key" "initialDelaySeconds" "value" .Values.livenessProbe.initialDelaySeconds)
            (dict "key" "periodSeconds" "value" .Values.livenessProbe.periodSeconds)
            (dict "key" "timeoutSeconds" "value" .Values.livenessProbe.timeoutSeconds)))
    (dict "name" "readinessProbe" "enabled" .Values.readinessProbe.enabled
          "httpGet" (dict "path" "/ready" "port" "http")
          "fields" (list
            (dict "key" "initialDelaySeconds" "value" .Values.readinessProbe.initialDelaySeconds)
            (dict "key" "periodSeconds" "value" .Values.readinessProbe.periodSeconds)
            (dict "key" "timeoutSeconds" "value" .Values.readinessProbe.timeoutSeconds))))
  | nindent 10 }}
```

Per-probe dict keys: `name` (required), `enabled`, `httpGet {path, port}`,
`tcpSocket {port}`, `exec {command: [...]}` (flow style — probes whose
quoting is exotic, like searxng's browser exec probe, stay chart-native),
`fields` (ordered `{key, value}` pairs emitted after the type block).
Top-level: pass a **list** (base indent defaults to 10) or a
`dict "probes" <list> "indent" <n>`. For a single probe use
`pcai-fleet-lib.probe` (same per-probe dict + `indent`).

### `pcai-fleet-lib.securityContext` — pod-level hardened profile

The fleet's pod profile (workbench, SQLhandler `security.hardened`):
non-root + seccomp `RuntimeDefault`. Returns the **field lines only** —
caller writes `securityContext:` and `nindent 8`.

```yaml
{{- include "pcai-fleet-lib.securityContext" (dict
    "runAsNonRoot" true          # optional, default true
    "runAsUser" .Values.security.runAsUser
    "fsGroup" 10001              # optional (workbench sets it for PVC group-write)
    "seccompType" "RuntimeDefault" # optional, default RuntimeDefault
    "indent" 8) | nindent 8 }}   # optional, default 8
```

`automountServiceAccountToken` is pod-spec level, not a securityContext —
charts keep rendering it inline.

### `pcai-fleet-lib.containerSecurityContext` — container-level hardened profile

The fleet's container profile: no privilege escalation, dropped capabilities,
read-only root filesystem. Returns the **field lines only**.

```yaml
{{- include "pcai-fleet-lib.containerSecurityContext" (dict
    "allowPrivilegeEscalation" false # optional, default false
    "readOnlyRootFilesystem" true    # optional, default true
    "capabilitiesDrop" "ALL"         # optional, default ALL; nil/"" omits the block
    "dropStyle" "block"              # "block" (default, toYaml shape) or "flow" (drop: ["ALL"] — SQLhandler's exact bytes)
    "indent" 12) | nindent 12 }}     # optional, default 12
```

`dropStyle` exists because the two hardened charts byte-differ here today
(workbench emits toYaml block style, SQLhandler emits `drop: ["ALL"]`) —
byte-identical adoption requires picking the chart's own shape.

### `pcai-fleet-lib.serviceMonitor` — Prometheus Operator ServiceMonitor

RAG's shape (the only ServiceMonitor in the fleet today). **Keep the
default-off gating**: `metrics.enabled` / `metrics.serviceMonitor` defaults to
`false` in the calling chart, and the caller keeps the `{{- if }}` gate so the
default render is byte-identical.

```yaml
{{- if .Values.metrics.enabled }}
{{- include "pcai-fleet-lib.serviceMonitor" (dict
    "name" (include "<chart>.fullname" .)
    "namespace" .Release.Namespace
    "labels" (dict "app" .Values.deployment.appName)   # optional
    "selectorLabels" (dict "app" .Values.deployment.appName)
    "port" "http"          # optional, default "http"
    "path" "/metrics"      # optional, default "/metrics"
    "interval" .Values.metrics.interval) | nindent 0 }}  # optional, default "30s", rendered quoted
{{- end }}
```

### `pcai-fleet-lib.kyvernoPolicy` — hpe-ezua vendor-label ClusterPolicy

The body carried byte-identically by applygate/searxng/prometheus (and the
SKILL's fallback pattern): pre-install hook, `background: false`, Pod /
Deployment / Service mutation stamping `hpe-ezua/type: vendor-service` and
`hpe-ezua/app: <chart>`. **Disabled by default** (`kyverno.enabled: false`) —
it is a cluster-scoped resource, so charts gate it and the default render
stays unchanged.

```yaml
{{- if .Values.kyverno.enabled }}
{{- include "pcai-fleet-lib.kyvernoPolicy" (dict
    "releaseName" .Release.Name
    "chartName" .Chart.Name
    "namespace" .Release.Namespace
    "extraNamespaces" (list)   # optional: chart-defined namespaces to also match
    "hook" "pre-install"       # optional, default "pre-install"
    "hookWeight" "-5"          # optional, default "-5"
    ) | nindent 0 }}
{{- end }}
```

SQLhandler's chart carries a *divergent* variant (post-install/post-upgrade
hook, `background: true`, instance selector) — keep that one chart-native or
extend the library with an explicit knob before adopting.

### `pcai-fleet-lib.authorizationPolicy` — oauth2-proxy gateway auth gate

Prometheus/K8S-MCP's shape (the fleet-canonical one), **disabled by default**
(`ezua.authorizationPolicy.enabled: false`) exactly like prometheus's
existing template: `action: CUSTOM`, oauth2-proxy provider, host-pinned rule,
`istio: ingressgateway` selector.

```yaml
{{- if and .Values.ezua.enabled .Values.ezua.authorizationPolicy.enabled }}
{{- include "pcai-fleet-lib.authorizationPolicy" (dict
    "name" (include "<chart>.fullname" .)     # renders "<name>-auth-policy"
    "namespace" .Values.ezua.authorizationPolicy.namespace
    "providerName" .Values.ezua.authorizationPolicy.providerName  # optional, default "oauth2-proxy"
    "endpoint" .Values.ezua.virtualService.endpoint) | nindent 0 }}
{{- end }}
```

RAG's policy renders the same YAML with different indent bytes — adopting RAG
on this template is a documented-delta decision, not a byte-identical one.

## Values contract

The library has no values of its own that affect renders (its `values.yaml`
is documentation only). The contract is the dict schemas above; anything
passed but unrecognized is ignored, anything required and missing fails loud
via `required`. Numbers/strings render via `%v` — quote at the call site
(`| quote`) where the chart's current bytes are quoted.

## Verification for every adoption

`helm dep build` → `helm lint <chart-dir>` → `helm template <chart-dir>`
(unnamed, defaults only) → `diff` against the chart's pre-change render.
Byte-identical or a documented delta — never a silent one.
