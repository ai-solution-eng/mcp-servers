FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# Install the project and all its dependencies from pyproject.toml
COPY pyproject.toml server.py ./
RUN uv pip install --system .

EXPOSE 9090

CMD ["ddgs-lite", "--transport", "streamable-http", "sse", "--host", "0.0.0.0", "--port", "9090"]
