{{/*
pcai-fleet-lib authorizationPolicy — the oauth2-proxy-ready Istio
AuthorizationPolicy, DISABLED BY DEFAULT like prometheus's existing template
(the fleet-canonical shape; K8S-MCP and SQLhandler carry the same bytes):
gateway-level auth gate delegating the decision for this host to the PCAI
oauth2-proxy extension (SSO / bearer tokens). OFF by default — the
rotating-token pain for machine MCP callers is a known, accepted trade on the
lab cluster; flipping it on enforces PCAI tokens for this host ON TOP of the
server's own API key, not instead of it.

    {{- if and .Values.ezua.enabled .Values.ezua.authorizationPolicy.enabled }}
    {{- include "pcai-fleet-lib.authorizationPolicy" (dict
        "name" (include "<chart>.fullname" .)
        "namespace" .Values.ezua.authorizationPolicy.namespace
        "providerName" .Values.ezua.authorizationPolicy.providerName
        "endpoint" .Values.ezua.virtualService.endpoint) }}
    {{- end }}

Returns "" when "enabled" is falsy.
*/}}
{{- define "pcai-fleet-lib.authorizationPolicy" -}}
{{- if .enabled -}}
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: {{ required "pcai-fleet-lib authorizationPolicy: 'name' is required" .name }}-auth-policy
  namespace: {{ required "pcai-fleet-lib authorizationPolicy: 'namespace' is required" .namespace }}
spec:
  action: CUSTOM
  provider:
    name: {{ default "oauth2-proxy" .providerName }}
  rules:
    - to:
        - operation:
            hosts:
              - {{ required (default "\nValid .Values.ezua.virtualService.endpoint is required when the AuthorizationPolicy is enabled !" .endpointRequiredMsg) .endpoint }}
  selector:
    matchLabels:
      istio: "ingressgateway"
{{- end -}}
{{- end -}}
