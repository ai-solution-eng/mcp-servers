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

# App code is copied after the dependency layer (cheap code-only rebuilds):
# src/ carries the whole logsearch_mcp package (server.py imports mcp_metrics
# — Wave-3 C3 — and webui, all inside the package now), and the fleet-
# hardlinked mcp_auth.py stays a top-level py-module at the repo root. The
# non-editable `uv pip install --system --no-deps .` ships exactly what
# pyproject declares (package-dir packages + py-modules), so both COPYs are
# required — a missing one makes the wheel SILENTLY omit the module and the
# image crash-loops at startup (the Wave-6 deploy-blocker class).
# MANIFEST.in ships ui/index.html in sdists (wheels don't carry data files —
# the image copies ui/ explicitly and webui.py has a /app/ui fallback, so
# this only matters for source installs).
COPY src/ ./src/
COPY mcp_auth.py MANIFEST.in ./
# The web UI asset: webui.py falls back to /app/ui/index.html when the
# module is installed into site-packages (the installed `logsearch-mcp`
# script runs from there, not from /app).
COPY ui/ ./ui/
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the INSTALLED package must import with
# the dependency closure from the RUN above — catches "dep added to pyproject
# but not to the RUN" drift in the build log instead of as a first-query
# ImportError in the pod. Naming every shipped module covers the whole set:
# the logsearch_mcp package modules (relative imports wire them together)
# plus the top-level mcp_auth py-module (Wave-6 packaging fix, carried over
# to the src layout).
#
# cd /tmp is LOAD-BEARING: from WORKDIR /app, python puts /app first on
# sys.path, so /app/mcp_auth.py (a COPY'd build-context file) would satisfy
# the import even if the WHEEL did not ship it — the gate would pass while
# the installed console script crash-looped. From /tmp every name must
# resolve out of site-packages.
RUN cd /tmp && python -c "from logsearch_mcp import server, webui, mcp_metrics; import mcp_auth; from importlib.metadata import version; print('logsearch-mcp', version('logsearch-mcp'), 'imports clean')"

# Read-only by design: run as non-root; the only cluster access is the
# read-only Role (pods get/list + pods/log get — see helm/templates/rbac.yaml).
RUN chmod -R a+rX /app

USER 10001

EXPOSE 9101

CMD ["logsearch-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9101"]
