#!/bin/bash
# configure-oidc-sql.sh — register this deployment's SSO callback in the UA
# realm's `ua` client and print the values block for the SQLhandler chart.
#
# The MM-RAG pattern (scripts/configure-oidc-rag.sh — the Open WebUI
# configure_oidc.sh recipe, fleet D22), adapted: run ONCE per deployment
# from a terminal with the cluster KUBECONFIG — it adds
# https://<endpoint>/oauth/oidc/callback to the ua client's valid redirect
# URIs and prints the client secret for security.oidc.sso.
#
# Idempotent: it will NOT add a duplicate redirect URI (checks first), so
# re-running is always safe.
#
# Requirements: kubectl (kubeconfig for the environment), curl, jq.
#
# SECURITY NOTES
#   * The script NEVER writes the client secret anywhere — it prints it to
#     the terminal ONCE (the same once-only contract as SQLhandler's key
#     minting). Pipe output to a file only if that file is a Secret.
#   * The ua client secret is SHARED by every OIDC app on this environment
#     (MM-RAG, OWUI, now SQLhandler) — handle it like the platform
#     credential it is; rotating it rotates every app's SSO at once.
#   * The PUT below sends ONLY clientId/name/redirectUris — Keycloak keeps
#     every other client setting (the same field-scoped update RAG's script
#     uses; a full-object PUT could clobber concurrent edits).

set -euo pipefail

REALM="UA"
CLIENT="ua"
# The deployment's VirtualService endpoint name (values: ezua.virtualService
# endpoint = sqlhandler.${DOMAIN_NAME}) — override if you deploy under a
# different hostname.
VIRTUAL_SERVICE_NAME="${VIRTUAL_SERVICE_NAME:-sqlhandler}"

command -v jq >/dev/null || { echo "jq is required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl is required"; exit 1; }

DOMAIN_NAME=$(kubectl get cm ezapp-manager-parameters -n ezapp-system -o jsonpath='{..DOMAIN_NAME}')
KEYCLOAK_URL="https://keycloak.${DOMAIN_NAME}"
echo "Keycloak: ${KEYCLOAK_URL}  realm: ${REALM}  client: ${CLIENT}  endpoint: ${VIRTUAL_SERVICE_NAME}.${DOMAIN_NAME}"

# --- admin token (master realm, admin-cli) ------------------------------------
KEYCLOAK_ADMIN_PASSWORD=$(kubectl get secrets admin-pass -n keycloak -o template='{{.data.password | base64decode}}')
ADMIN_TOKEN=$(curl -k -s -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=admin" \
    -d "password=${KEYCLOAK_ADMIN_PASSWORD}" \
    -d "grant_type=password" \
    -d "client_id=admin-cli" | jq -r '.access_token')
[ -n "$ADMIN_TOKEN" ] && [ "$ADMIN_TOKEN" != "null" ] || { echo "FAILED to get an admin token"; exit 1; }

# --- the ua client -------------------------------------------------------------
CLIENT_ID=$(curl -k -s -X GET -H "Authorization: Bearer $ADMIN_TOKEN" \
                 "${KEYCLOAK_URL}/admin/realms/${REALM}/clients?clientId=${CLIENT}" | jq -r '.[0].id')
[ -n "$CLIENT_ID" ] && [ "$CLIENT_ID" != "null" ] || { echo "client '${CLIENT}' not found in realm ${REALM}"; exit 1; }

CLIENT_SECRET=$(curl -k -s -X GET "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${CLIENT_ID}" \
                     -H "Content-Type: application/json" -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.secret')

# --- register the callback URL (no duplicates) --------------------------------
OIDC_CALLBACK_URL="https://${VIRTUAL_SERVICE_NAME}.${DOMAIN_NAME}/oauth/oidc/callback"
CURRENT=$(curl -k -s -X GET "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${CLIENT_ID}" \
    -H "Content-Type: application/json" -H "Authorization: Bearer ${ADMIN_TOKEN}")
if echo "$CURRENT" | jq -e --arg u "$OIDC_CALLBACK_URL" '.redirectUris | index($u)' >/dev/null; then
  echo "redirect URI already registered: ${OIDC_CALLBACK_URL}"
else
  REDIRECT_URIS=$(echo "$CURRENT" | jq '.redirectUris' | jq ". += [\"${OIDC_CALLBACK_URL}\"]")
  curl -k -s -o /dev/null -w "redirect-URI registration HTTP %{http_code}\n" -X PUT \
    "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${CLIENT_ID}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -d "{
          \"clientId\": \"${CLIENT}\",
          \"name\": \"${CLIENT}\",
          \"redirectUris\": ${REDIRECT_URIS}
  }"
  # Verify the registration actually landed (a 200 from the PUT with a
  # stale body would otherwise look like success).
  NOW=$(curl -k -s -X GET "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${CLIENT_ID}" \
      -H "Content-Type: application/json" -H "Authorization: Bearer ${ADMIN_TOKEN}")
  echo "$NOW" | jq -e --arg u "$OIDC_CALLBACK_URL" '.redirectUris | index($u)' >/dev/null \
    || { echo "FAILED: redirect URI not present after registration — check the HTTP code above"; exit 1; }
  echo "redirect URI registered + verified: ${OIDC_CALLBACK_URL}"
fi

# --- what to put in the values -------------------------------------------------
cat <<VALUES

# --- paste into your values (security.oidc.sso) — the G2 overlay
# --- (helm/local/values.g2.yaml) already carries this shape; the ONLY
# --- decision is the secret path below -------------------------------
security:
  oidc:
    enabled: true
    issuer: "${KEYCLOAK_URL}/realms/${REALM}"
    audience: ua
    identityClaim: preferred_username
    jwksUrl: "http://keycloak-headless.keycloak.svc.cluster.local:8080/realms/${REALM}/protocol/openid-connect/certs"
    sso:
      enabled: true
      clientId: "${CLIENT}"
      redirectUri: "${OIDC_CALLBACK_URL}"
      # EXTERNAL discovery URL (rides the ezaf-gateway; the in-cluster mesh
      # path black-holes the token POST from a non-mesh pod — RAG's
      # live-learned note). TLS verifies via the platform CA.
      providerUrl: "${KEYCLOAK_URL}/realms/${REALM}/.well-known/openid-configuration"
      scopes: "openid profile email"
      cookieName: pcai-sso
      cookieMaxAge: "43200"
      cookieSecure: "true"
      # --- pick ONE secret path (the overlay defaults to the envsubst) ---
      # (a) PCAI envsubst (what MM-RAG runs): leave
      #     clientSecret: "\${OIDC_CLIENT_SECRET}"
      #     in the overlay — the values editor substitutes it at apply time.
      # (b) A Secret you own (rotation-safe, survives values re-paste):
      clientExistingSecret: ""    # e.g. oidc-client
      clientExistingSecretKey: clientSecret
      clientSecret: ""            # NEVER inline for real deploys

# Client secret (KEY MATERIAL — printed ONCE, here only; put it in the
# Secret named above or in the OIDC_CLIENT_SECRET envsubst variable):
#   $(echo "$CLIENT_SECRET" | sed 's/\(....\).*/\1…/')  <full value printed ONLY here>
${CLIENT_SECRET}

# Secret creation (option b):
#   kubectl -n sqlhandler create secret generic oidc-client \\
#     --from-literal=clientSecret='<the value above>'
VALUES

echo
echo "NOTE: the ua client secret is shared by every OIDC app on this environment (MM-RAG, OWUI, SQLhandler) — handle it like the platform credential it is."
echo "NOTE: the SQLhandler values editor must ALSO substitute \${OIDC_CLIENT_SECRET} for path (a) — verify after apply:"
echo "  kubectl -n sqlhandler get deploy sqlhandler -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"SQLHANDLER_OIDC_SSO_CLIENT_SECRET\")].value}'"
echo "  → a real secret string = substituted; the literal '\${OIDC_CLIENT_SECRET}' = NOT substituted (switch to option b)."
