# SQLhandler MCP server — direct SQL access to columnar data (MCP server).
# NOTE: pin the base image by digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    TMPDIR=/tmp

# Runtime-only system deps: pyarrow/numpy wheels need libgomp1 at runtime.
# NO build-essential — every dependency ships manylinux wheels and the
# local package is pure Python (hatchling), so keeping a compiler
# toolchain in a network-facing server image is pure attack surface.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR ${APP_HOME}

# Install the package + deps. The explicit azure-* pair is also listed in
# pyproject.toml (keep the two in sync): deltalake's OneLake service-principal
# flow uses them. The [iceberg] extra backs the iceberg backend the helm
# chart advertises. The build requires network (pip).
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system \
        pyarrow duckdb azure-identity azure-storage-file-datalake pandas tabulate uvicorn starlette pyyaml pygments \
    && uv pip install --system -e ".[iceberg]"

# Preinstall the external-database scanner extensions (SQLHANDLER_ATTACH):
# DuckDB extension autoinstall is DISABLED at runtime (fs lockdown), so these
# must be baked. They load from /opt/duckdb-ext (SQLHANDLER_DUCKDB_EXTENSION_DIR,
# applied by sqlhandler.external before LOAD); the network-facing pod never
# downloads extensions at runtime — the egress this step needs belongs to the
# build machine, not the pod. Core extensions (postgres_scanner, mysql_scanner,
# sqlite_scanner) download from extensions.duckdb.org; mssql additionally pulls
# from community-extensions.duckdb.org — DuckDB's community repo — so BOTH hosts
# must be reachable at build time. mariadb connections ride mysql_scanner.
# The SQL Server scanner is published as `mssql` on DuckDB 1.5.x: the older
# ODBC-based `sqlserver` community extension is no longer published for this
# version (INSTALL sqlserver 404s and would fail the build). mssql speaks TDS
# 7.4 natively and needs NO unixODBC/msodbcsql18, so no Microsoft apt repo or
# EULA'd driver is installed here — dead weight in a network-facing image.
# The external.py "sqlserver" adapter name refers to this extension; its ATTACH
# must pass (TYPE mssql) — the casing DuckDB's ATTACH accepts for the type.
RUN python -c "import duckdb; con = duckdb.connect(); con.execute(\"SET extension_directory='/opt/duckdb-ext'\"); [con.execute(stmt) for stmt in ('INSTALL postgres_scanner', 'INSTALL mysql_scanner', 'INSTALL sqlite_scanner', 'INSTALL mssql FROM community')]" \
    && chmod -R a+rX /opt/duckdb-ext
ENV SQLHANDLER_DUCKDB_EXTENSION_DIR=/opt/duckdb-ext

# The build context may carry restrictive file modes (e.g. 600 from a
# hardlink-based working tree). COPY preserves modes, and the app now runs
# as uid 1000 — without this it cannot read its own source and crashes on
# the first import (PermissionError on /app/src/sqlhandler/__init__.py).
# a+rX: read for everyone, execute only on directories. Files stay
# root-owned on purpose: the unprivileged runtime cannot modify its code.
RUN chmod -R a+rX "${APP_HOME}"

ENV PYTHONPATH="/app/src"

# Run unprivileged — the helm chart's hardened profile (security.hardened)
# also enforces runAsNonRoot at the pod level.
USER app

EXPOSE 9097

# Streamable HTTP (MCP 2.0) by default — the PCAI helm chart serves /mcp.
# Override to stdio with: --transport stdio  (or SQLHANDLER_TRANSPORT=stdio)
CMD ["python", "-m", "sqlhandler.server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9097"]