# logsearch-mcp — read-only, namespace-scoped Kubernetes pod-log search over MCP.
# NOTE: pin the base image by digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# OS CA certificates: the uv base omits them; needed if the cluster API is
# ever reached through an https:// endpoint (in-cluster default is plain http).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml server.py webui.py MANIFEST.in ./
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `logsearch-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN uv pip install --system .

# Read-only by design: run as non-root; the only cluster access is the
# read-only Role (pods get/list + pods/log get — see helm/templates/rbac.yaml).
RUN chmod -R a+rX /app

USER 10001

EXPOSE 9101

CMD ["logsearch-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9101"]
