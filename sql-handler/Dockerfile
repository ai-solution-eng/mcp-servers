# SQLhandler MCP server — direct SQL access to columnar data (MCP server).
# Base image pinned BY DIGEST (tag python3.12-bookworm-slim, digest captured
# 2026-10-02). To move the base: resolve the tag's digest and update BOTH
# lines — `docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.12-bookworm-slim`
# or the registry API. A digest pin is the only thing that stops a rebuild
# from silently shipping a different Python/uv.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

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

# Install deps and the local package as SEPARATE layers (bench follow-up,
# 2026-09 — build-speed fix): dependency wheels (~600 MB) live in a layer
# keyed on pyproject.toml+README only, so a code-only release rebuilds just
# the cheap COPY/chmod layers instead of re-downloading every wheel. The
# dependency list is the FULL transitive closure of pyproject's dependencies
# + the [iceberg] extra (verified against pyproject; keep the three in sync):
# the azure-* pair backs deltalake's OneLake service-principal flow; pyiceberg
# + sqlalchemy are the [iceberg] extra. The local package then installs
# --no-deps (its dependencies are all present by construction). The build
# requires network (pip).
# Deps layer keyed on requirements-deps.txt (generated from pyproject by
# bump_version.sh — deps + [iceberg] extra, WITHOUT the package's own
# version). History of this layer's cache key:
#   v1: COPY pyproject.toml README.md  — README doc edits re-downloaded
#       ~600 MB of wheels (live-seen: the ACL/admin README wave, three
#       consecutive builds);
#   v2: COPY pyproject.toml            — fixed README busts, but a VERSION
#       bump also rewrites pyproject → every release rebuilt AND re-pushed
#       the full wheel layer (live-seen: v3.1.1's 78s export + 69s push);
#   v3 (this): the deps manifest changes ONLY when dependencies change —
#       a version-only release leaves the ~600 MB layer cached AND
#       un-pushed (registry reuses the blob; release push drops to the
#       ~1 MB src layers). The single source of truth stays pyproject:
#       bump_version.sh regenerates the manifest from it at bump time, and
#       a drift-guard test refuses a manifest that diverges.
COPY requirements-deps.lock.txt ./

RUN uv pip install --system -r requirements-deps.lock.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-deps -e .

# Sanity gate (build-time, cheap): the local package must import with zero
# dependency resolution — catches "dep added to pyproject but not to the
# manifest" drift immediately, in the build log, instead of as a first-query
# ImportError in the pod. (Paired with tests/test_deps_manifest.py, which
# refuses a manifest whose dep set diverges from pyproject.)
RUN python -c "import sqlhandler; print('sqlhandler', sqlhandler.__version__, 'imports clean')"

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
# ducklake: baked here since 2.4 (vendored locally too — duckdb-ext/). The
# community attach types mongo + bigquery are OPT-IN: append
# 'INSTALL mongo FROM community', 'INSTALL bigquery FROM community' to the
# statement list below when a deployment needs them (they are NOT in the
# default image — external.py fails with a clear "extension not baked"
# message until the build is extended). There is NO clickhouse ATTACH
# extension for DuckDB 1.5.5 (only chsql_native scan functions) — do not add
# one, and never add motherduck (a bare md: ATTACH without a token hangs the
# pod in an interactive OAuth flow; use a ducklake md: catalog with
# motherduck_token instead).
# OPT-IN example (uncomment the whole RUN block and remove the default one
# above to bake the two community attach extensions):
# RUN python -c "import duckdb; con = duckdb.connect(); con.execute(\"SET extension_directory='/opt/duckdb-ext'\"); [con.execute(stmt) for stmt in ('INSTALL postgres_scanner', 'INSTALL mysql_scanner', 'INSTALL sqlite_scanner', 'INSTALL mssql FROM community', 'INSTALL ducklake', 'INSTALL mongo FROM community', 'INSTALL bigquery FROM community')]" \
#     && chmod -R a+rX /opt/duckdb-ext
RUN python -c "import duckdb; con = duckdb.connect(); con.execute(\"SET extension_directory='/opt/duckdb-ext'\"); [con.execute(stmt) for stmt in ('INSTALL postgres_scanner', 'INSTALL mysql_scanner', 'INSTALL sqlite_scanner', 'INSTALL mssql FROM community', 'INSTALL ducklake')]" \
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