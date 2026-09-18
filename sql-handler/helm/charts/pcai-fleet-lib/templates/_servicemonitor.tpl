{{/*
pcai-fleet-lib serviceMonitor — Prometheus Operator ServiceMonitor for the
chart's /metrics endpoint. RAG's template is the fleet's only ServiceMonitor
today; this reproduces its exact shape.

DEFAULT-OFF GATING IS PART OF THE CONTRACT (FLEET-EXECUTION-PLAN C3/C5):
metrics.serviceMonitor / metrics.enabled defaults to false in the consuming
chart and the CALLER keeps the {{- if }} gate, so the default render stays
byte-identical:

    {{- if .Values.metrics.serviceMonitor }}
    {{- include "pcai-fleet-lib.serviceMonitor" (dict
        "name" (include "<chart>.fullname" .)
        "namespace" .Release.Namespace
        "labels" (dict "app" .Values.deployment.appName)
        "selectorLabels" (dict "app" .Values.deployment.appName)
        "port" "http"
        "path" "/metrics"
        "interval" .Values.metrics.interval) }}
    {{- end }}

Returns "" when "enabled" is falsy. interval renders quoted (RAG renders
`interval: "30s"`).
*/}}
{{- define "pcai-fleet-lib.serviceMonitor" -}}
{{- if .enabled -}}
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {{ required "pcai-fleet-lib serviceMonitor: 'name' is required" .name }}
  namespace: {{ required "pcai-fleet-lib serviceMonitor: 'namespace' is required" .namespace }}
{{- with .labels }}
  labels:
    {{- toYaml . | nindent 4 }}
{{- end }}
spec:
  selector:
    matchLabels:
      {{- toYaml (required "pcai-fleet-lib serviceMonitor: 'selectorLabels' is required" .selectorLabels) | nindent 6 }}
  endpoints:
    - port: {{ default "http" .port }}
      path: {{ default "/metrics" .path }}
      interval: {{ default "30s" .interval | quote }}
{{- end -}}
{{- end -}}
