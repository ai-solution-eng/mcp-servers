# statistical-visualization-mcp (seaborn MCP) — v0.1.0
# MCP 2.0 SDK (MCPServer), stateless streamable-http, httpx2-only.
FROM python:3.12-slim

WORKDIR /app

# OS trust store for httpx2 (TLS via truststore) + build deps for
# matplotlib/numpy wheels on slim. ca-certificates is explicit by fleet
# convention (python:3.12-slim bundles the CA store, but we pin it visibly).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libfreetype6-dev \
    libpng-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py schema.py mcp_instance.py ./
COPY utils ./utils
COPY renderers ./renderers
COPY tools ./tools

# Writable matplotlib/font cache (read-only HOME breaks fontconfig in K8s)
ENV MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONUNBUFFERED=1

EXPOSE 9092

CMD ["python", "server.py"]
