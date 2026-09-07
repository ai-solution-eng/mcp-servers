FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libfreetype6-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py schema.py mcp_instance.py ./
COPY utils ./utils
COPY renderers ./renderers
COPY tools ./tools

RUN find /app -name "types.py" -delete 2>/dev/null || true

EXPOSE 9092

CMD ["python", "server.py"]