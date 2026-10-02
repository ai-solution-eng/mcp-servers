# Workbench MCP server — persistent per-agent scratch workspaces on a PVC.
# NOTE: pin the base image by digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home workbench \
    && mkdir -p /data && chown workbench:workbench /data

# Install deps and the local package as SEPARATE layers (SQLhandler build-speed
# fix, fleet-wide): the dependency wheels live in a layer keyed ONLY on the
# manifest (pyproject.toml — this project declares no readme field, so
# nothing else belongs in this COPY), so a code-only release rebuilds the
# cheap COPY/install layers instead of re-resolving + re-downloading every
# dependency. The list below is the explicit expansion of pyproject
# [project].dependencies (there are no extras) — keep the two in sync.
COPY pyproject.toml ./

RUN uv pip install --system "mcp>=2.0.0" "starlette>=0.37.0" "uvicorn>=0.27.0"

# App code + assets (cheap layers, keyed on the source files themselves).
COPY server.py webui.py mcp_auth.py ./
# The vendored mcp_fleet_common package (Wave-6 G1, D16): server.py imports
# `from mcp_fleet_common import …` — without this COPY the non-editable
# `uv pip install --system --no-deps .` builds a wheel without the package
# and the installed console script crash-loops at import (same class as
# mcp_auth). Its dependencies are stdlib-only at import time, so it needs
# nothing beyond the manifest-keyed layer above.
COPY mcp_fleet_common/ ./mcp_fleet_common/
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `workbench-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN uv pip install --system --no-deps . \
    && chmod -R a+rX /app

# Sanity gate (build-time, cheap): imports the console-script entry module
# (server, = workbench-mcp server:main) plus every shipped module/package
# against the dependency layer — catching "dep added to pyproject but not to
# the RUN above" drift in the build log instead of as a ModuleNotFoundError
# crash-loop in the pod.
RUN python -c "import server, webui, mcp_auth, mcp_fleet_common; print('workbench-mcp imports clean')"

# Scratch pad by design: runs as non-root; /data is a PVC mounted by the
# chart (fsGroup 10001) and holds no secrets.
USER 10001

EXPOSE 9103

CMD ["workbench-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9103"]
