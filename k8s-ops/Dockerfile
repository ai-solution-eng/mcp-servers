FROM python:3.14-slim

# Pinned for reproducible builds; verify the official sha256 before install.
ARG KUBECTL_VERSION=v1.37.0
# TARGETARCH is set automatically by buildx (amd64/arm64/...) so multi-arch
# and cross-builds fetch the matching kubectl binary.
ARG TARGETARCH=amd64

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" && \
    curl -fsSLO "https://dl.k8s.io/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" && \
    echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check --strict && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl kubectl.sha256 && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
# Built-in HPE ops console (static shell served at /ui/ by server.py).
COPY ui/ ./ui/
# COPY preserves source file modes, and workspace defaults can be tighter
# than the container user (uid 10001) needs: a+rX = directories traversable,
# files readable, nothing executable that was not already.
RUN chmod -R a+rX /app

RUN addgroup --system --gid 10001 k8smcp && \
    adduser --system --uid 10001 --ingroup k8smcp k8smcp

USER 10001

EXPOSE 9090

CMD ["python", "server.py"]
