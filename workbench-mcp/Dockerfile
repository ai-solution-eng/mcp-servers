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

COPY pyproject.toml server.py webui.py mcp_auth.py ./
# The vendored mcp_fleet_common package (Wave-6 G1, D16): server.py imports
# `from mcp_fleet_common import …` — without this COPY the non-editable
# `uv pip install --system .` builds a wheel without the package and the
# installed console script crash-loops at import (same class as mcp_auth).
COPY mcp_fleet_common/ ./mcp_fleet_common/
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `workbench-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN uv pip install --system . \
    && chmod -R a+rX /app

# Scratch pad by design: runs as non-root; /data is a PVC mounted by the
# chart (fsGroup 10001) and holds no secrets.
USER 10001

EXPOSE 9103

CMD ["workbench-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9103"]
