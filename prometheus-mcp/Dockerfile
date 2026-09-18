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

COPY pyproject.toml server.py prom_client.py webui.py saved_queries.py ./
COPY mcp_metrics.py ./mcp_metrics.py
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `prometheus-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN chmod -R a+rX /app || true
RUN uv pip install --system .

EXPOSE 9095

CMD ["prometheus-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9095"]
