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
# src/ carries the whole prometheus_mcp package (server, prom_client, webui,
# saved_queries, mcp_metrics — all inside the package now); the fleet-
# hardlinked mcp_auth.py stays a top-level py-module at the repo root. The
# non-editable `uv pip install --system --no-deps .` ships exactly what
# pyproject declares (package-dir packages + py-modules), so both COPYs are
# required — a missing one makes the wheel SILENTLY omit the module and the
# image crash-loops at startup (the W7 deploy-blocker class this section
# originally fixed for the loose-script mcp_metrics.py).
COPY src/ ./src/
COPY mcp_auth.py ./
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `prometheus-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN chmod -R a+rX /app || true

# The local package installs --no-deps: every dependency is present by
# construction from the manifest-keyed layer above.
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the gate imports every shipped module of
# the INSTALLED package by name (server pulls the package together via its
# relative imports; mcp_auth rides as the top-level py-module) against the
# dependency layer — catching "dep added to pyproject but not to the RUN
# above" drift in the build log instead of as a ModuleNotFoundError in the
# pod.
#
# cd /tmp is LOAD-BEARING: from WORKDIR /app, python puts /app first on
# sys.path, so /app/mcp_auth.py (a COPY'd build-context file) would satisfy
# the import even if the WHEEL did not ship it — the gate would pass while
# the installed console script crash-looped. From /tmp every name must
# resolve out of site-packages.
RUN cd /tmp && python -c "from prometheus_mcp import server, prom_client, webui, saved_queries, mcp_metrics; import mcp_auth; print('prometheus-mcp imports clean')"

EXPOSE 9095

CMD ["prometheus-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9095"]
