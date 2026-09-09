{{/* Read-only rules shared by the ClusterRole (scope=cluster) and the
      namespaced Role (scope=namespace). Cluster-scoped resource types are
      harmless inside a Role (a Role can never grant access to them). */}}
{{- define "k8s-mcp.readonlyRules" -}}
rules:
- apiGroups: [""]
  resources:
    - pods
    - pods/log
    - pods/status
    - services
    - endpoints
    - configmaps
    - events
    - namespaces
    - nodes
    - persistentvolumeclaims
    - persistentvolumes
    - replicationcontrollers
    - serviceaccounts
    - limitranges
    - resourcequotas
  verbs: [get, list, watch]
- apiGroups: ["apps"]
  resources: [deployments, statefulsets, daemonsets, replicasets, controllerrevisions]
  verbs: [get, list, watch]
- apiGroups: ["batch"]
  resources: [jobs, cronjobs]
  verbs: [get, list, watch]
- apiGroups: ["networking.k8s.io"]
  resources: [ingresses, networkpolicies, ingressclasses]
  verbs: [get, list, watch]
- apiGroups: ["apiextensions.k8s.io"]
  resources: [customresourcedefinitions]
  verbs: [get, list, watch]
- apiGroups: ["metrics.k8s.io"]
  resources: [nodes, pods]
  verbs: [get, list]
- apiGroups: ["networking.istio.io"]
  resources: [virtualservices]
  verbs: [get, list, watch]
- apiGroups:
  {{- toYaml (default list .Values.rbac.extraResourceGroups) | nindent 4 }}
  resources: ["*"]
  verbs: [get, list, watch]
{{- end -}}
