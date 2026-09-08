# =============================================================================
# searxng-mcp-browser — headless Chromium sidecar for searxng-mcp
#
# Playwright's Chromium (headless shell preferred) serving a CDP debugging
# port on pod-localhost:9222. The searxng-mcp container connects to it via
# playwright's connect_over_cdp to render JavaScript-only pages.
#
# Build & push (from this directory):
#   docker buildx build \
#     --build-arg PLAYWRIGHT_VERSION=1.49.1 \
#     -t ghcr.io/ai-solution-eng/searxng-mcp-browser:v1.1.0 . --push
#
# Security posture:
# * CDP binds loopback only (default of --remote-debugging-port). All
#   containers in a pod share one network namespace, so the MCP container
#   reaches it on 127.0.0.1 — and nothing outside the pod can. Do NOT add
#   --remote-debugging-address=0.0.0.0: CDP is unauthenticated remote
#   code-execution-as-browser-user, and the cluster has no NetworkPolicies.
# * --no-sandbox is required under k8s defaults; acceptable ONLY because
#   this container mounts no secrets and its crash domain is isolated.
# * The corporate MITM CA (if configured) is installed at container start
#   from a mounted ConfigMap, then the process drops to an unprivileged
#   user via gosu.
# =============================================================================

# Base pinned to Debian 12 (bookworm) DELIBERATELY: playwright's
# `install-deps` supports bookworm by name, but as of 1.49.x it does not
# know Debian 13 (trixie) — and `python:3.12-slim` now resolves to trixie.
# On trixie the installer falls back to its hardcoded Ubuntu 20.04 package
# list, which references ttf-ubuntu-font-family / ttf-unifont — names that
# don't exist in Debian 13 — so the build dies at apt. Revisit only when
# bumping to a playwright version that officially lists trixie.
ARG PLAYWRIGHT_VERSION=1.49.1
FROM docker.io/library/python:3.12-slim-bookworm

ARG PLAYWRIGHT_VERSION

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# gosu: drop from root (needed to install the mounted CA at start) to the
# unprivileged 'browser' user before exec'ing Chromium.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 browser \
    && mkdir -p /ms-playwright /certs-src /certs-installed \
    && chown -R browser:browser /ms-playwright

# Playwright downloads the browser at build time. --only-shell installs just
# the headless shell (~100 MB instead of ~400 MB); the flag needs a recent
# playwright — if the pinned version predates it, the fallback installs the
# full Chromium build instead (still correct, just larger). Separate layers:
# a deps-install failure must not re-download the browser on rebuild.
RUN pip install "playwright==${PLAYWRIGHT_VERSION}"

RUN (playwright install chromium --only-shell || playwright install chromium) \
    && chmod -R a+rX /ms-playwright

RUN playwright install-deps chromium

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# No USER directive: the entrypoint may need root to install the mounted
# corporate CA, and execs Chromium as 'browser' via gosu immediately after.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
