{{/*
pcai-fleet-lib probes — the fleet's liveness/readiness/startup boilerplate.

Encodes every probe shape the eight charts render today:
  - MCP-trio httpGet pattern (workbench/applygate/logsearch/prometheus):
    startup period 5s ×12, readiness 10s ×3, liveness 30s ×3 on /healthz.
  - SQLhandler's /health + /ready pattern (per-probe knobs; its quirk of
    taking the startup probe's timeoutSeconds from livenessProbe.timeoutSeconds
    is encoded by the CALLER passing that value — not "fixed" here).
  - tcpSocket pattern (K8S-MCP, searxng).

Contract — `pcai-fleet-lib.probes` takes a LIST of probe dicts (or a dict
with "probes" + "indent"); probes render in caller order and a falsy
"enabled" drops the probe entirely (today's {{- if }} semantics):

    (dict "name"     "startupProbe"          ; required
          "enabled"  .Values.startupProbe.enabled
          "httpGet"  (dict "path" "/health" "port" "http")   ; or "tcpSocket"
          "fields"   (list                            ; ordered key/value pairs
                      (dict "key" "periodSeconds" "value" 5)
                      (dict "key" "timeoutSeconds" "value" .Values.livenessProbe.timeoutSeconds)
                      (dict "key" "failureThreshold" "value" 24)))

The library owns ONLY formatting (fixed key order, indentation); the chart
owns every value. Returns "" when no probe is enabled (no stray lines — the
indentation is applied inside the template, guarded on a non-empty result).
Call it flush at the container-field level; pass dict-form
{"probes": [...], "indent": N} to change the base indent (default 10):

    {{- include "pcai-fleet-lib.probes" (list ...) }}
*/}}
{{- define "pcai-fleet-lib.probes" -}}
{{- $probes := . -}}
{{- $ind := 10 -}}
{{- if kindIs "map" . -}}
{{- $probes = required "pcai-fleet-lib.probes: pass a LIST of probe dicts, or a dict with a 'probes' key" .probes -}}
{{- $ind = int (default 10 .indent) -}}
{{- end -}}
{{- $active := list -}}
{{- range $p := $probes -}}
{{- if $p.enabled -}}{{- $active = append $active $p -}}{{- end -}}
{{- end -}}
{{- if $active -}}
{{- $blocks := list -}}
{{- range $p := $active -}}{{- $blocks = append $blocks (include "pcai-fleet-lib._probeLines" $p) -}}{{- end -}}
{{- join "\n" $blocks | nindent $ind -}}
{{- end -}}
{{- end -}}

{{/*
Single-probe variant of pcai-fleet-lib.probes — same per-probe dict, plus an
optional "indent" (default 10). Returns "" when disabled.
    {{- include "pcai-fleet-lib.probe" (dict "name" "readinessProbe" "enabled" true
        "httpGet" (dict "path" "/ready" "port" "http")
        "fields" (list (dict "key" "periodSeconds" "value" 10))) }}
*/}}
{{- define "pcai-fleet-lib.probe" -}}
{{- if .enabled -}}
{{- include "pcai-fleet-lib._probeLines" . | nindent (int (default 10 .indent)) -}}
{{- end -}}
{{- end -}}

{{/*
Internal: the relative lines of ONE probe (name at 0, fields at +2).
httpGet emits path-then-port (every fleet chart's order); tcpSocket port;
exec a flow-style command list. `fields` pairs render in caller order.
*/}}
{{- define "pcai-fleet-lib._probeLines" -}}
{{- $lines := list (printf "%s:" (required "pcai-fleet-lib probe: 'name' is required" .name)) -}}
{{- with .httpGet -}}
{{- $lines = append $lines "  httpGet:" -}}
{{- $lines = append $lines (printf "    path: %v" (required "pcai-fleet-lib probe: httpGet.path is required" .path)) -}}
{{- $lines = append $lines (printf "    port: %v" (required "pcai-fleet-lib probe: httpGet.port is required" .port)) -}}
{{- end -}}
{{- with .tcpSocket -}}
{{- $lines = append $lines "  tcpSocket:" -}}
{{- $lines = append $lines (printf "    port: %v" (required "pcai-fleet-lib probe: tcpSocket.port is required" .port)) -}}
{{- end -}}
{{- with .exec -}}
{{- $lines = append $lines "  exec:" (printf "    command: [%s]" (join ", " .command)) -}}
{{- end -}}
{{- range .fields -}}
{{- $lines = append $lines (printf "  %s: %v" (required "pcai-fleet-lib probe field: 'key' is required" .key) .value) -}}
{{- end -}}
{{- join "\n" $lines -}}
{{- end -}}
