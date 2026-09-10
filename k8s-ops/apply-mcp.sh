##
# Specify the domain name below for the MCP server. 
# E.g. pcai-se-ai-application.hst.rdlabs.hpecorp.net
##
export DOMAIN_NAME=""
export NAMESPACE=k8s-mcp-ops
export SUBDOMAIN_NAME=k8s-mcp-2-0-server
export MCP_BEARER_TOKEN_NAME=k8s-mcp-2-0-apikey
export K8S_MCP_BLOCKED_NAMESPACES="kube-system,kube-public,k8s-mcp-ops"
export K8S_MCP_EXEC_ENABLED=true
export K8S_MCP_EXEC_NAMESPACES="*"
export K8S_MCP_EXEC_REQUIRE_LABEL=false
export MCP_HOSTNAME=${SUBDOMAIN_NAME}.${DOMAIN_NAME}


if [ -z "$DOMAIN_NAME" ]; then
  echo "Error: DOMAIN_NAME is not set. Please edit this file and update the domain name." >&2
  exit 1
fi

kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"


if ! kubectl -n "$NAMESPACE" get secret "$MCP_BEARER_TOKEN_NAME" >/dev/null 2>&1; then
  BEARER_TOKEN=$(openssl rand -hex 32)
  kubectl -n "$NAMESPACE" create secret generic "$MCP_BEARER_TOKEN_NAME" \
    --from-literal="api-key=$BEARER_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "New API key generated — save it now: $BEARER_TOKEN"
else
  echo "Secret $MCP_BEARER_TOKEN_NAME exists — API key is:"
  export BEARER_TOKEN=$(kubectl -n "$NAMESPACE" get secret "$MCP_BEARER_TOKEN_NAME" -o jsonpath='{.data.api-key}' | base64 -d)
  echo $BEARER_TOKEN
fi


ACTION="$1"

case "$ACTION" in
  --apply)
    envsubst < k8s-mcp-2-0-server.yaml | kubectl apply -f -
    kubectl -n $NAMESPACE rollout status deploy/k8s-mcp-2-0-server
    ;;
  --delete)
    envsubst < k8s-mcp-2-0-server.yaml | kubectl delete -f -
    ;;
  *)
    echo "Usage: $0 --apply | --delete" >&2
    exit 1
    ;;
esac

echo "Use this in opencode:

{
  \"mcpServers\": {
    \"k8s-ops-mcp\": {
      \"type\": \"remote\",
      \"enabled\": true,
      \"url\": \"https://${SUBDOMAIN_NAME}.${DOMAIN_NAME}/mcp\",
      \"headers\": { \"Authorization\": \"Bearer ${BEARER_TOKEN}\" }
    }
  }
}

Or for restricted exec:

{
  \"mcpServers\": {
    \"k8s-ops-mcp\": {
      \"type\": \"remote\",
      \"enabled\": true,
      \"url\": \"https://${SUBDOMAIN_NAME}.${DOMAIN_NAME}/mcp\",
      \"headers\": { \"Authorization\": \"Bearer ${BEARER_TOKEN}\",
      \"X-Exec-Namespaces\": \"comma,separated,list,of,Namespaces,where,it,is,allowed,to,exec\" }
    }
  }
}
"
