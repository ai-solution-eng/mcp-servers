{{/*
pcai-fleet-lib kyvernoPolicy — the hpe-ezua vendor-label ClusterPolicy.

The body carried byte-identically by applygate/searxng/prometheus (and the
pcai-helm-port skill's fallback pattern): a pre-install ClusterPolicy that
stamps the PCAI vendor-service labels (`hpe-ezua/type: vendor-service`,
`hpe-ezua/app: <chart>`) onto the chart's Pod/Deployment/Service objects so
the EZUA console/tooling recognizes them. Hook annotations (pre-install,
weight -5, before-hook-creation) keep re-installs idempotent.

DEFAULT-OFF GATING IS PART OF THE CONTRACT: a ClusterPolicy is cluster-scoped,
so the consuming chart gates on `kyverno.enabled: false` (fleet values
default) and keeps the {{- if }} — the default render stays byte-identical:

    {{- if .Values.kyverno.enabled }}
    {{- include "pcai-fleet-lib.kyvernoPolicy" (dict
        "releaseName" .Release.Name
        "chartName" .Chart.Name
        "namespace" .Release.Namespace
        "extraNamespaces" (list)) }}
    {{- end }}

SQLhandler's chart carries a DIVERGENT variant (post-install,post-upgrade
hook, background: true, instance selector) — keep it chart-native until the
library grows explicit knobs for it.
*/}}
{{- define "pcai-fleet-lib.kyvernoPolicy" -}}
{{- if .enabled -}}
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: {{ printf "add-vendor-app-labels-%s-%s" (required "pcai-fleet-lib kyvernoPolicy: 'releaseName' is required" .releaseName) (required "pcai-fleet-lib kyvernoPolicy: 'chartName' is required" .chartName) }}
  annotations:
    "helm.sh/hook": {{ default "pre-install" .hook }}
    "helm.sh/hook-weight": {{ default "-5" .hookWeight | quote }}
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  background: {{ default false .background }}
  rules:
  - name: add-vendor-app-labels
    match:
      any:
      - resources:
          namespaces:
          - {{ required "pcai-fleet-lib kyvernoPolicy: 'namespace' is required" .namespace }}
          {{- range .extraNamespaces }}
          - {{ . }}
          {{- end }}
          kinds:
          - Pod
          - Deployment
          - Service
    mutate:
      patchStrategicMerge:
        metadata:
            labels:
              "hpe-ezua/type": vendor-service
              "hpe-ezua/app": {{ required "pcai-fleet-lib kyvernoPolicy: 'chartName' is required" .chartName }}
{{- end -}}
{{- end -}}
