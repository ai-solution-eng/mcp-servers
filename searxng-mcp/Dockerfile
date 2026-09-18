FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# System CA certificates — REQUIRED. The uv base image omits them (uv itself
# uses rustls with bundled roots), but python httpx (fetch_content) verifies
# TLS against the OS trust store; without this every https fetch fails with
# CERTIFICATE_VERIFY_FAILED. (Same base image made ddgs-lite's httpx fallback
# silently dead too — its primp primary just never needed the OS store.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the project and all its dependencies from pyproject.toml.
# The 'browser' extra pulls in the playwright *client* library (~40 MB pip
# package, no browser download) so fetch_content can escalate JS-only pages
# to the searxng-mcp-browser sidecar. Without it the server still runs;
# rendering is simply unavailable and fetch_content degrades to plain HTTP.
# ALL of server.py's runtime imports must be in the build context:
# mcp_auth (auth wave), mcp_metrics (Wave-3 C3) AND url_policy (Wave-1
# P0-6 — added after this COPY line was last touched; the Wave-6 packaging
# proof caught it missing). The non-editable `uv pip install --system .`
# silently omits a declared py-module whose file is not present, so a
# rebuilt image crash-loops at startup without these (ModuleNotFoundError).
COPY pyproject.toml server.py searxng_client.py fetcher.py browser_client.py url_policy.py mcp_auth.py mcp_metrics.py ./
RUN chmod -R a+rX /app || true
RUN uv pip install --system ".[browser]"

EXPOSE 9090

CMD ["searxng-mcp", "--transport", "streamable-http", "sse", "--host", "0.0.0.0", "--port", "9090"]
