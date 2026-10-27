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

# NOTE: prometheus-client is REQUIRED for /metrics (APPLYGATE_METRICS_ENABLED).
# It was missing here while the server import-guarded the client, so the write
# path shipped emitting ZERO samples and the failure was silent — the route
# still answered 200 with an explanatory comment, and Prometheus recorded
# scrape_samples_scraped=0. Keep this list in step with pyproject.toml; the
# sanity gate below fails the build if they drift apart again.
RUN uv pip install --system mcp kubernetes PyYAML starlette uvicorn "prometheus-client>=0.20.0"

# App code is copied after the dependency layer (cheap code-only rebuilds):
# the app modules live in the fleet src layout (src/applygate_mcp/ — the wheel
# builds from there via pyproject's package-dir) and mcp_auth.py stays a
# TOP-LEVEL module at the build root (fleet hardlink -> top-level py-module).
# The web console asset: webui serves ui/index.html from /app/ui
# (it also honors an APPLYGATE_WEBUI_HTML override for out-of-image assets).
COPY src/ ./src/
COPY mcp_auth.py ./
COPY ui/ ./ui/
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the local package must import with the
# dependency closure from the RUN above — catches "dep added to pyproject
# but not to the RUN" drift in the build log instead of as a first-query
# ImportError in the pod. Run from /tmp so the gate proves the INSTALLED
# wheel (package from site-packages + the top-level mcp_auth py-module)
# rather than accidentally resolving out of the /app/src build tree — that
# is the Wave-6 packaging fix, made explicit.
RUN cd /tmp && python -c "from applygate_mcp import server, webui; import mcp_auth; from importlib.metadata import version; print('applygate-mcp', version('applygate-mcp'), 'imports clean')"

# Metrics gate (2026-10-20): importing is not enough. The /metrics handler
# import-guards prometheus_client and degrades to a comment-only body, so a
# missing wheel produced a GREEN build, a healthy pod, and zero samples. Assert
# the client is importable AND that generate_latest() emits a real sample, so
# this class of silent observability loss fails the BUILD instead of prod.
RUN python -c "from prometheus_client import Counter, generate_latest; c = Counter('applygate_build_gate_total', 'build gate'); c.inc(); out = generate_latest().decode(); assert 'applygate_build_gate_total' in out, 'prometheus_client present but emits nothing'; print('metrics exposition OK')"

# Default home of the JSONL audit trail; the Helm chart mounts a PVC (or, if
# persistence is disabled, an emptyDir) here — the volume wins over this
# image layer at runtime.
RUN mkdir -p /data && chown -R 10001:10001 /data

EXPOSE 9102

RUN chmod -R a+rX /app

USER 10001

CMD ["applygate-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9102"]
