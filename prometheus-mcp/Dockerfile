# Prometheus MCP server — thin read-only client over the Prometheus HTTP API.
# NOTE: pin the base image by digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# OS CA certificates: the uv base omits them; needed if PROM_URL is ever an
# https:// endpoint (the default in-cluster target is plain http).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install deps and the local package as SEPARATE layers (SQLhandler build-speed
# fix, fleet-wide): the dependency wheels live in a layer keyed ONLY on the
# manifest (pyproject.toml — this project declares no readme field and
# .dockerignore excludes README.md, so nothing else belongs in this COPY), so
# a code-only release rebuilds the cheap COPY/chmod layers instead of
# re-resolving + re-downloading every dependency. The list below is the
# explicit expansion of pyproject [project].dependencies (there are no
# extras) — keep the two in sync. server.py imports mcp_types directly; that
# arrives as mcp's own transitive dependency, which the sanity gate at the
# end of this file pins in the build log.
COPY pyproject.toml ./

RUN uv pip install --system "httpx2>=2.12.0" "mcp>=2.0.0" "starlette>=0.37.0" "uvicorn>=0.27.0"

# App code + assets (cheap layers, keyed on the source files themselves).
COPY server.py prom_client.py webui.py saved_queries.py mcp_auth.py ./
COPY mcp_metrics.py ./mcp_metrics.py
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `prometheus-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN chmod -R a+rX /app || true

# The local package installs --no-deps: every dependency is present by
# construction from the manifest-keyed layer above.
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): these are loose scripts ([tool.setuptools]
# py-modules, not a package), so the gate imports every shipped module by
# name against the dependency layer — catching "dep added to pyproject but
# not to the RUN above" drift in the build log instead of as a
# ModuleNotFoundError in the pod.
RUN python -c "import server, prom_client, webui, saved_queries, mcp_metrics, mcp_auth; print('prometheus-mcp imports clean')"

EXPOSE 9095

CMD ["prometheus-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9095"]
