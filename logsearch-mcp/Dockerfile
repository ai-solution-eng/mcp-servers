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

# Layer-ordering fix (2026-09, mirrors the SQLhandler build): dependency
# wheels live in a layer keyed ONLY on the manifests (pyproject.toml), so a
# code-only change no longer re-resolves and re-downloads every dependency.
# The list below is the FULL transitive closure of [project].dependencies —
# verified against pyproject; keep the two in sync when dependencies change.
# The local package then installs --no-deps (all deps present by construction).
COPY pyproject.toml ./

RUN uv pip install --system mcp kubernetes starlette uvicorn

# mcp_metrics.py MUST be in the build context: server.py imports it
# (Wave-3 C3) and the non-editable `uv pip install --system --no-deps .`
# only ships what pyproject py-modules declares + what gets COPY'd here —
# a rebuilt image crash-loops at startup without it (same class as mcp_auth).
# App code is copied after the dependency layer (cheap code-only rebuilds);
# MANIFEST.in ships ui/index.html in sdists (wheels from the flat layout
# won't carry data files — the image copies ui/ explicitly and webui.py has
# a /app/ui fallback, so this only matters for source installs).
COPY server.py webui.py mcp_auth.py mcp_metrics.py MANIFEST.in ./
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `logsearch-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the local package must import with the
# dependency closure from the RUN above — catches "dep added to pyproject
# but not to the RUN" drift in the build log instead of as a first-query
# ImportError in the pod. server.py imports mcp_auth AND mcp_metrics, so
# this covers the whole py-modules list (Wave-6 packaging fix).
RUN python -c "import server, webui, mcp_auth, mcp_metrics; from importlib.metadata import version; print('logsearch-mcp', version('logsearch-mcp'), 'imports clean')"

# Read-only by design: run as non-root; the only cluster access is the
# read-only Role (pods get/list + pods/log get — see helm/templates/rbac.yaml).
RUN chmod -R a+rX /app

USER 10001

EXPOSE 9101

CMD ["logsearch-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9101"]
