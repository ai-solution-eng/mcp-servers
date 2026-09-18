{{/*
pcai-fleet-lib securityContext — the fleet's hardened profile.

Pod-level (workbench + SQLhandler security.hardened shapes):
  runAsNonRoot true, runAsUser <uid>, optional runAsGroup/fsGroup,
  seccompProfile RuntimeDefault.

Container-level (pcai-fleet-lib.containerSecurityContext):
  allowPrivilegeEscalation false, capabilities.drop ALL,
  readOnlyRootFilesystem true.

Returns the FIELD LINES ONLY (no `securityContext:` key, no leading/trailing
newline) — the caller writes the parent key and lets the template indent:

      securityContext:
        {{- include "pcai-fleet-lib.securityContext" (dict "runAsUser" .Values.security.runAsUser) }}

Known fleet byte-divergence: capabilities.drop is block-style in workbench
(toYaml shape) but flow-style `drop: ["ALL"]` in SQLhandler — "dropStyle"
reproduces either; byte-identical adoption must pick the chart's own.

Booleans honor explicit false: omitted key = fleet default; pass the key to
override (sprig `default` cannot distinguish false from unset).
*/}}
{{- define "pcai-fleet-lib.securityContext" -}}
{{- $nonRoot := true -}}
{{- if hasKey . "runAsNonRoot" -}}{{- $nonRoot = .runAsNonRoot -}}{{- end -}}
{{- $seccomp := "RuntimeDefault" -}}
{{- if hasKey . "seccompType" -}}{{- $seccomp = .seccompType -}}{{- end -}}
{{- $lines := list
      (printf "runAsNonRoot: %v" $nonRoot)
      (printf "runAsUser: %v" (required "pcai-fleet-lib securityContext: 'runAsUser' is required (the fleet profile is non-root)" .runAsUser)) -}}
{{- with .runAsGroup -}}{{- $lines = append $lines (printf "runAsGroup: %v" .) -}}{{- end -}}
{{- with .fsGroup -}}{{- $lines = append $lines (printf "fsGroup: %v" .) -}}{{- end -}}
{{- if $seccomp -}}
{{- $lines = append $lines "seccompProfile:" -}}
{{- $lines = append $lines (printf "  type: %v" $seccomp) -}}
{{- end -}}
{{- join "\n" $lines | nindent (int (default 8 .indent)) -}}
{{- end -}}

{{/*
Container-level hardened profile — see pcai-fleet-lib.securityContext.
Keys: allowPrivilegeEscalation (default false), readOnlyRootFilesystem
(default true; false OMITS the line — SQLhandler's gating semantics),
capabilitiesDrop (default "ALL"; nil/"" omits the capabilities block),
dropStyle "block" (default, toYaml shape) | "flow" (drop: ["ALL"]).
*/}}
{{- define "pcai-fleet-lib.containerSecurityContext" -}}
{{- $privEsc := false -}}
{{- if hasKey . "allowPrivilegeEscalation" -}}{{- $privEsc = .allowPrivilegeEscalation -}}{{- end -}}
{{- $roRootfs := true -}}
{{- if hasKey . "readOnlyRootFilesystem" -}}{{- $roRootfs = .readOnlyRootFilesystem -}}{{- end -}}
{{- $drop := "ALL" -}}
{{- if hasKey . "capabilitiesDrop" -}}{{- $drop = .capabilitiesDrop -}}{{- end -}}
{{- $lines := list (printf "allowPrivilegeEscalation: %v" $privEsc) -}}
{{- if $drop -}}
{{- $lines = append $lines "capabilities:" -}}
{{- if eq .dropStyle "flow" -}}
{{- $lines = append $lines (printf "  drop: [\"%s\"]" $drop) -}}
{{- else -}}
{{- $lines = append $lines "  drop:" -}}
{{- $lines = append $lines (printf "  - %s" $drop) -}}
{{- end -}}
{{- end -}}
{{- if $roRootfs -}}
{{- $lines = append $lines "readOnlyRootFilesystem: true" -}}
{{- end -}}
{{- join "\n" $lines | nindent (int (default 12 .indent)) -}}
{{- end -}}
