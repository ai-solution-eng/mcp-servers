# applygate-mcp — the governed WRITE half of the fleet's Kubernetes story
# (server-side apply into allowlisted namespaces; the read-only K8s MCP is
# its sibling). NOTE: pin the base image by digest for production builds;
# this tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# OS CA certificates: the uv base omits them. The in-cluster API server is
# https://, so TLS verification against kube-apiserver needs a trust store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml server.py webui.py ./
# The read-only web console asset: webui.py serves ui/index.html from /app/ui
# (it also honors an APPLYGATE_WEBUI_HTML override for out-of-image assets).
COPY ui/ ./ui/
RUN uv pip install --system .

# Default home of the JSONL audit trail; the Helm chart mounts a PVC (or, if
# persistence is disabled, an emptyDir) here — the volume wins over this
# image layer at runtime.
RUN mkdir -p /data && chown -R 10001:10001 /data

EXPOSE 9102

RUN chmod -R a+rX /app

USER 10001

CMD ["applygate-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9102"]
