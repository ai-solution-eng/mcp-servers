#!/bin/sh
# Entrypoint for the searxng-mcp-browser sidecar.
# 1. Install a mounted corporate MITM CA (if any) into the OS trust store —
#    Chromium verifies TLS against the NSS/OS store and does NOT honor
#    SSL_CERT_FILE or NODE_EXTRA_CA_CERTS. Needs root; we drop right after.
# 2. Locate the Chromium binary playwright downloaded (headless shell or
#    full build, depending on what the image installed).
# 3. Exec it as the unprivileged 'browser' user with a loopback-only CDP
#    port and the corporate proxy (Chromium ignores HTTP_PROXY env vars —
#    --proxy-server is the mechanism it honors).
set -e

# --- 1. CA installation (best-effort; a missing/mis-mounted CA must not
#       crash the sidecar — it would just fail TLS to MITM'd sites).
if [ -d /certs-src ] && [ -n "$(ls -A /certs-src 2>/dev/null)" ]; then
  cp /certs-src/*.crt /usr/local/share/ca-certificates/ 2>/dev/null || true
  update-ca-certificates >/dev/null 2>&1 || true
fi

# --- 2. Browser binary.
BIN="$(find /ms-playwright -type f \( -name headless_shell -o -name chrome \) 2>/dev/null | head -n1)"
if [ -z "$BIN" ]; then
  echo "FATAL: no chromium binary found under /ms-playwright" >&2
  exit 1
fi

# --- 3. Launch flags.
PROXY_ARGS=""
if [ -n "$BROWSER_PROXY" ]; then
  PROXY_ARGS="--proxy-server=$BROWSER_PROXY"
fi

ARGS="--headless --no-sandbox --disable-dev-shm-usage --disable-gpu \
--no-first-run --no-default-browser-check \
--user-data-dir=/tmp/chrome-profile \
--remote-debugging-port=${BROWSER_CDP_PORT:-9222} \
$PROXY_ARGS ${BROWSER_EXTRA_ARGS:-}"

echo "Starting headless browser: $BIN $ARGS"
exec gosu browser "$BIN" $ARGS
