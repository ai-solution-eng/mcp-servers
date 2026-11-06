# skills-mcp — serves the team's markdown skills over MCP 2.0.
# NOTE: pin the base image by digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# CA certificates: the uv base omits them, and the refresh path fetches the
# skills tarball over https://codeload.github.com.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer keyed ONLY on pyproject.toml: a code-only change no longer
# re-resolves and re-downloads every dependency. This list must stay equal to
# [project].dependencies — the sanity gate at the bottom catches drift.
COPY pyproject.toml ./
RUN uv pip install --system mcp starlette uvicorn pyyaml

# App code after the dependency layer (cheap code-only rebuilds). The
# non-editable install ships exactly what pyproject declares (package-dir
# packages + py-modules), so BOTH COPYs are required: a missing one makes the
# wheel silently omit a module and the image crash-loops at startup.
COPY src/ ./src/
COPY mcp_auth.py ./
RUN uv pip install --system --no-deps .

# Bundled skills snapshot (D2): the SAME public tarball the runtime refresh
# pulls, fetched at BUILD time so the first boot works with zero network.
# SKILLS_REF is a build arg so a release can pin a commit instead of main.
# NOTE: ADD from a URL is cached by URL, not by content — rebuild with
# --no-cache when you want a fresh snapshot baked in.
ARG SKILLS_REPO=ai-solution-eng/tools
ARG SKILLS_REF=main
ADD https://codeload.github.com/${SKILLS_REPO}/tar.gz/${SKILLS_REF} /tmp/tools.tar.gz
RUN mkdir -p /opt/skills-bundled \
    && tar -xzf /tmp/tools.tar.gz -C /tmp \
    && cp -r /tmp/tools-*/skills* /opt/skills-bundled/ \
    && printf '%s' "${SKILLS_REF}" > /opt/skills-bundled/.ref \
    && rm -rf /tmp/tools*

# Build-time sanity gate: every shipped module must import OUT OF
# site-packages. `cd /tmp` is load-bearing — from /app, /app/mcp_auth.py would
# satisfy the import even if the wheel omitted it, and the gate would pass
# while the installed console script crash-looped.
RUN cd /tmp && python -c "from skills_mcp import server; import mcp_auth; from importlib.metadata import version; print('skills-mcp', version('skills-mcp'), 'imports clean')"

# Non-root by design; the only writable path at runtime is /data (an emptyDir
# in the chart), never the image. chmod neutralizes files that land mode-0600
# on the build host.
RUN chmod -R a+rX /app /opt/skills-bundled

USER 10001

EXPOSE 9101

CMD ["skills-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9101"]
