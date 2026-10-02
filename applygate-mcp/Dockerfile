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

# Layer-ordering fix (2026-09, mirrors the SQLhandler build): dependency
# wheels live in a layer keyed ONLY on the manifests (pyproject.toml), so a
# code-only change no longer re-resolves and re-downloads every dependency.
# The list below is the FULL transitive closure of [project].dependencies —
# verified against pyproject; keep the two in sync when dependencies change.
# The local package then installs --no-deps (all deps present by construction).
COPY pyproject.toml ./

RUN uv pip install --system mcp kubernetes PyYAML starlette uvicorn

# App code is copied after the dependency layer (cheap code-only rebuilds):
# the web console asset: webui.py serves ui/index.html from /app/ui
# (it also honors an APPLYGATE_WEBUI_HTML override for out-of-image assets).
COPY server.py webui.py mcp_auth.py ./
COPY ui/ ./ui/
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the local package must import with the
# dependency closure from the RUN above — catches "dep added to pyproject
# but not to the RUN" drift in the build log instead of as a first-query
# ImportError in the pod. server.py imports mcp_auth, so this also covers
# the whole py-modules list (Wave-6 packaging fix).
RUN python -c "import server, webui, mcp_auth; from importlib.metadata import version; print('applygate-mcp', version('applygate-mcp'), 'imports clean')"

# Default home of the JSONL audit trail; the Helm chart mounts a PVC (or, if
# persistence is disabled, an emptyDir) here — the volume wins over this
# image layer at runtime.
RUN mkdir -p /data && chown -R 10001:10001 /data

EXPOSE 9102

RUN chmod -R a+rX /app

USER 10001

CMD ["applygate-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9102"]
