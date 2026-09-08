FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# System CA certificates — REQUIRED. The uv base image omits them (uv itself
# uses rustls with bundled roots), but python httpx (fetch_content) verifies
# TLS against the OS trust store; without this every https fetch fails with
# CERTIFICATE_VERIFY_FAILED. (Same base image made ddgs-lite's httpx fallback
# silently dead too — its primp primary just never needed the OS store.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the project and all its dependencies from pyproject.toml
COPY pyproject.toml server.py searxng_client.py fetcher.py ./
RUN uv pip install --system .

EXPOSE 9090

CMD ["searxng-mcp", "--transport", "streamable-http", "sse", "--host", "0.0.0.0", "--port", "9090"]
