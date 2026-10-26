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

# Build-speed fix (mirrors SQLhandler): dependency wheels live in a layer
# keyed on pyproject.toml ONLY, so a code-only .py edit rebuilds just the
# cheap COPY/chmod/install layers instead of re-resolving and re-downloading
# every dependency. The list below is the explicit dependencies from pyproject
# [project].dependencies plus the [browser] extra — names AND version floors
# carried VERBATIM, so bumping a floor in pyproject also changes this layer
# key and forces a fresh resolve (uv resolves their transitives from PyPI at
# build time). KEEP IT IN SYNC with pyproject.toml — a dependency added to
# pyproject but not to this RUN is caught by the import gate at the bottom of
# this file, in the build log, instead of as a first-query ImportError in the
# pod.
#
# The [browser] extra = playwright — the playwright *client* library (~40 MB
# pip package, no browser download) so fetch_content can escalate JS-only
# pages to the searxng-mcp-browser sidecar. Without it the server still runs;
# rendering is simply unavailable and fetch_content degrades to plain HTTP.
COPY pyproject.toml ./
RUN uv pip install --system \
        "httpx2>=2.12.0" "mcp>=2.0.0" "starlette>=0.37.0" "uvicorn>=0.27.0" \
        "brotli>=1.1.0" "trafilatura>=2.0.0" "beautifulsoup4>=4.12.0" \
        "html2text>=2024.2.26" "curl_cffi>=0.9.0" \
        "playwright>=1.49"

# App code — deliberately AFTER the dependency layer so editing any of these
# files never invalidates the wheel layer above. ALL of server.py's runtime
# imports must be in the build context: mcp_auth (auth wave), mcp_metrics
# (Wave-3 C3) AND url_policy (Wave-1 P0-6 — added after this COPY line was
# last touched; the Wave-6 packaging proof caught it missing). The
# non-editable `uv pip install --system --no-deps .` below silently omits a
# declared py-module whose file is not present, so a rebuilt image
# crash-loops at startup without these (ModuleNotFoundError).
COPY server.py searxng_client.py fetcher.py browser_client.py url_policy.py mcp_auth.py mcp_metrics.py ./
RUN chmod -R a+rX /app || true
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the INSTALLED code must import with zero
# dependency resolution. Runs from /tmp (not WORKDIR /app) so `import server`
# resolves from site-packages — the wheel the image actually runs — not the
# source copy sitting in the build directory. Every direct dependency is
# imported here explicitly (beautifulsoup4 under its import name bs4); this
# is the drift-catcher for "dep added to pyproject but not to the RUN above"
# (server.py imports most of these lazily, so a missing wheel would otherwise
# surface only on first use).
RUN cd /tmp && python -c "import server, httpx2, mcp, starlette, uvicorn, brotli, trafilatura, bs4, html2text, curl_cffi, playwright; print('searxng-mcp imports clean')"

EXPOSE 9090

CMD ["searxng-mcp", "--transport", "streamable-http", "sse", "--host", "0.0.0.0", "--port", "9090"]
