FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# Install dependencies and the local module as SEPARATE layers (SQLhandler
# pattern, build-speed fix): the dependency wheels live in a layer keyed ONLY
# on pyproject.toml, so a code-only edit to server.py rebuilds just the two
# cheap layers below instead of re-resolving and re-downloading every wheel
# (ddgs pulls the C-extension heavies lxml + primp; mcp pulls pydantic,
# anyio, sse-starlette — verified against uv.lock). The list below is
# pyproject's [project].dependencies verbatim — keep the two in sync; the
# import gate at the bottom catches module-level drift. Pure-Python
# transitives still resolve from PyPI metadata exactly as before.
COPY pyproject.toml ./

RUN uv pip install --system \
        "mcp>=2.0.0" \
        "httpx>=0.27.0" \
        "starlette>=0.37.0" \
        "uvicorn>=0.27.0" \
        "brotli>=1.1.0" \
        "ddgs>=9.14.0"

# server.py is a loose module, not a package (pyproject:
# [tool.setuptools] py-modules = ["server"], script ddgs-lite = "server:main"),
# so the code layer is a single-file COPY plus a --no-deps install of the
# local package (its dependencies are all present by construction).
COPY server.py ./
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the module must import with zero dependency
# resolution — catches "dep added to pyproject but not to the RUN above" drift
# immediately, in the build log, instead of as a first-query ImportError in
# the pod. The console script is exercised too: it is what CMD runs.
RUN python -c "import server; print('ddgs_lite imports clean')" \
    && which ddgs-lite

EXPOSE 9090

CMD ["ddgs-lite", "--transport", "streamable-http", "sse", "--host", "0.0.0.0", "--port", "9090"]
