"""Configuration for the SQLhandler Microsoft Fabric OneLake access.

All sensitive values are read from the environment, never hardcoded. Populate a
``.env`` file (see ``config/.env.example``) or export the variables in the
deployment environment. The temporary service-principal credential provided by
Toromont must NOT be committed to source control.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import DataProvider


from .provider import LakehouseError


def _getenv(name: str, default: str = "") -> str:
    """Read an environment variable with a stripped fallback."""
    return os.environ.get(name, default).strip()


def _to_int(name: str, env: Mapping[str, str], default: int) -> int:
    """Read an env var as an int, falling back to ``default`` on garbage."""
    raw = _getenv(name, env.get(name, ""))
    try:
        return int(raw)
    except ValueError:
        return default


def _to_bool(name: str, env: Mapping[str, str], default: bool = False) -> bool:
    """Read an env var as a bool (true/1/yes/on), falling back on garbage."""
    raw = _getenv(name, env.get(name, "")).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return default


# Raw-text landing-zone discovery (csv/tsv/json/ndjson/jsonl + .gz variants)
# on the s3 and nfs/file backends. A LANDING-ZONE feature by design: raw text
# has no row groups/statistics, so every scan reads whole files — never the
# scan path for big data (promote with the write tier's COPY TO parquet).
RAW_TEXT_SUFFIXES = (
    ".csv",
    ".tsv",
    ".json",
    ".ndjson",
    ".jsonl",
)
RAW_GZ_SUFFIXES = tuple(s + ".gz" for s in RAW_TEXT_SUFFIXES)


def raw_formats_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether raw-text table discovery is on (``SQLHANDLER_RAW_FORMATS``).

    Values: ``on`` (default) | ``off`` — the master switch for the
    landing-zone feature; ``off`` hides every raw-format table so discovery
    is byte-identical to the Parquet/Delta-only behavior.
    """
    return _to_bool("SQLHANDLER_RAW_FORMATS", env or os.environ, True)


def raw_max_file_mb(env: Mapping[str, str] | None = None) -> int:
    """Per-file size cap (MB) for raw-format discovery (``SQLHANDLER_RAW_MAX_FILE_MB``).

    A raw-format table is DISCOVERED only if every file that would feed it is
    at or under this size; any single oversized file disqualifies the whole
    table (a partitioned table with one huge shard is skipped too — partial
    tables would lie). ``0`` = unlimited. For ``.gz`` files the cap applies to
    the COMPRESSED size (an uncompressed estimate needs a decompression pass
    per object at listing time); gzip means the cap is approximate and a
    decompression bomb can exceed it — the default stays on because the
    landing zone is operator-authored storage, not untrusted upload.
    """
    return max(_to_int("SQLHANDLER_RAW_MAX_FILE_MB", env or os.environ, 64), 0)


@dataclass(frozen=True)
class FabricConfig:
    """Connection details for a Microsoft Fabric / OneLake workspace.

    Two ways to address a lakehouse are supported:
      * ``lakehouse_abfss_url`` - a full ABFS/OneLake URL to the lakehouse
        "Tables" directory (preferred; works with both DuckDB and pyarrow).
      * explicit ``workspace_id`` + ``lakehouse_id`` - assembled into the URL.
    """

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    # Full ABFS URL to the lakehouse, e.g.
    #   abfss://<workspace-id>@onelake.dfs.fabric.microsoft.com/<lakehouse-id>
    lakehouse_abfss_url: str = ""

    # Fallback: assemble the URL from workspace + lakehouse GUIDs.
    workspace_id: str = ""
    lakehouse_id: str = ""

    # Optional: override the fabric DFS authority (defaults to the public one).
    fabric_authority: str = "onelake.dfs.fabric.microsoft.com"

    @property
    def is_configured(self) -> bool:
        """Whether we have enough to build an ABFS URL and authenticate."""
        has_identity = all((self.tenant_id, self.client_id, self.client_secret))
        has_url = bool(self.lakehouse_abfss_url)
        has_ids = bool(self.workspace_id and self.lakehouse_id)
        return has_identity and (has_url or has_ids)

    @property
    def abfss_tables_url(self) -> str:
        """The ABFS URL pointing at the lakehouse ``Tables`` directory."""
        if self.lakehouse_abfss_url:
            base = self.lakehouse_abfss_url.rstrip("/")
            # If the user passed a URL already ending in /Tables, keep it.
            return base if base.endswith("/Tables") else f"{base}/Tables"
        return f"abfss://{self.workspace_id}@{self.fabric_authority}/{self.lakehouse_id}/Tables"


def load_config(env: dict | None = None) -> FabricConfig:
    """Build a :class:`FabricConfig` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      FABRIC_TENANT_ID
      FABRIC_CLIENT_ID
      FABRIC_CLIENT_SECRET
      FABRIC_LAKEHOUSE_ABFSS_URL
      FABRIC_WORKSPACE_ID
      FABRIC_LAKEHOUSE_ID
      FABRIC_AUTHORITY
    """
    e = env if env is not None else os.environ
    return FabricConfig(
        tenant_id=_getenv("FABRIC_TENANT_ID", e.get("FABRIC_TENANT_ID", "")),
        client_id=_getenv("FABRIC_CLIENT_ID", e.get("FABRIC_CLIENT_ID", "")),
        client_secret=_getenv("FABRIC_CLIENT_SECRET", e.get("FABRIC_CLIENT_SECRET", "")),
        lakehouse_abfss_url=_getenv(
            "FABRIC_LAKEHOUSE_ABFSS_URL",
            e.get("FABRIC_LAKEHOUSE_ABFSS_URL", ""),
        ),
        workspace_id=_getenv("FABRIC_WORKSPACE_ID", e.get("FABRIC_WORKSPACE_ID", "")),
        lakehouse_id=_getenv("FABRIC_LAKEHOUSE_ID", e.get("FABRIC_LAKEHOUSE_ID", "")),
        fabric_authority=_getenv("FABRIC_AUTHORITY", e.get("FABRIC_AUTHORITY", "onelake.dfs.fabric.microsoft.com")),
    )


@dataclass(frozen=True)
class S3Config:
    """Connection details for an S3-compatible object store (MinIO, AWS).

    ``endpoint_url`` is the S3 API endpoint; for MinIO that is e.g.
    ``http://127.0.0.1:9000`` (scheme optional - https is inferred from
    ``use_ssl``). ``bucket`` is required and ``prefix`` scopes the search to a
    sub-tree (e.g. ``datasets``). Path-style addressing is on by default,
    which is what MinIO and most S3-compatible stores use.
    """

    endpoint_url: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    session_token: str = ""
    bucket: str = ""
    prefix: str = ""
    anonymous: bool = False
    use_ssl: bool = False
    path_style_access: bool = True
    # Table format: "auto" (default) detects Delta tables by their
    # _delta_log and reads everything else as plain Parquet; "parquet"
    # forces plain Parquet; "delta" treats every discovered table as Delta.
    format: str = "auto"

    @property
    def is_configured(self) -> bool:
        """Whether we have enough to point at the bucket and authenticate."""
        if not self.bucket:
            return False
        if self.anonymous:
            return True
        return bool(self.access_key and self.secret_key)


def load_s3_config(env: dict | None = None) -> S3Config:
    """Build a :class:`S3Config` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      S3_ENDPOINT_URL
      S3_REGION
      S3_ACCESS_KEY
      S3_SECRET_KEY
      S3_SESSION_TOKEN
      S3_BUCKET
      S3_PREFIX
      S3_ANONYMOUS   (true/1/on enables anonymous read of a public bucket)
      S3_USE_SSL     (force https when endpoint_url has no scheme)
      S3_PATH_STYLE  (default true; MinIO uses path-style addressing)
      S3_FORMAT      (auto | parquet | delta; auto detects Delta tables by
                      their _delta_log and reads the rest as plain Parquet)
      plus the SQLHANDLER_RAW_FORMATS / SQLHANDLER_RAW_MAX_FILE_MB knobs
      (see :func:`raw_formats_enabled` / :func:`raw_max_file_mb`) for
      raw-text landing-zone discovery on this backend
    """
    e = env if env is not None else os.environ
    return S3Config(
        endpoint_url=_getenv("S3_ENDPOINT_URL", e.get("S3_ENDPOINT_URL", "")),
        region=_getenv("S3_REGION", e.get("S3_REGION", "us-east-1")),
        access_key=_getenv("S3_ACCESS_KEY", e.get("S3_ACCESS_KEY", "")),
        secret_key=_getenv("S3_SECRET_KEY", e.get("S3_SECRET_KEY", "")),
        session_token=_getenv("S3_SESSION_TOKEN", e.get("S3_SESSION_TOKEN", "")),
        bucket=_getenv("S3_BUCKET", e.get("S3_BUCKET", "")),
        prefix=_getenv("S3_PREFIX", e.get("S3_PREFIX", "")),
        anonymous=_to_bool("S3_ANONYMOUS", e, False),
        use_ssl=_to_bool("S3_USE_SSL", e, False),
        path_style_access=_to_bool("S3_PATH_STYLE", e, True),
        format=_getenv("S3_FORMAT", e.get("S3_FORMAT", "auto")).lower() or "auto",
    )


@dataclass(frozen=True)
class IcebergConfig:
    """Connection details for an Apache Iceberg catalog.

    ``catalog_type`` selects how tables are listed/described:
      * ``rest`` (default) - Iceberg REST catalog (the modern standard; what
        Amazon S3 Tables / Dremio / Nessie / Databricks Unity Catalog expose)
      * ``sql``            - a SQL catalog (SQLite/Postgres), handy for local
        development and tests
      * ``glue``           - AWS Glue Data Catalog (boto3 reads the standard
        AWS_* env vars / instance role / IRSA for region + credentials —
        NEVER config values here)
      * ``hive``           - Hive metastore over thrift; ``catalog_uri`` is
        the thrift address (``thrift://host:9083``)
      * ``nessie``         - Project Nessie via pyiceberg's REST catalog:
        ``catalog_uri`` is the Nessie API endpoint, ``nessie_ref`` (optional)
        pins the branch/tag

    ``catalog_uri`` is the REST endpoint URL, the thrift URI (hive) or e.g.
    ``sqlite:///path`` (sql). ``storage`` carries the credentials/endpoint of
    the object store the Parquet data files live on (reuses the ``S3_*`` env
    vars); it is ignored when the warehouse is on the local filesystem.
    """

    catalog_type: str = "rest"
    catalog_uri: str = ""
    catalog_token: str = ""
    catalog_name: str = "sqlhandler"  # SQL catalogs partition metadata by this name
    warehouse: str = ""
    namespace: str = ""  # optional filter for list_tables (default: all)
    # Nessie branch/tag to pin reads to (catalog_type "nessie" only).
    nessie_ref: str = ""
    storage: S3Config = field(default_factory=S3Config)

    # Catalog types this config knows (mirrors pyiceberg's native registry
    # plus "nessie", which rides the REST type with a nessie URI/ref —
    # verified against the installed pyiceberg's CatalogType).
    KNOWN_CATALOG_TYPES = ("rest", "sql", "glue", "hive", "nessie")

    @property
    def is_configured(self) -> bool:
        """Whether we have the minimum config for the selected catalog type.

        Per type (mirrors what ``iceberg._catalog`` passes to pyiceberg):
          * ``rest``/``nessie``/``sql``/``hive``: a catalog URI
            (REST endpoint / Nessie API / sqlite-or-postgres URL / thrift
            metastore address respectively)
          * ``glue``: nothing but the AWS environment — region and
            credentials resolve through boto3's standard chain (AWS_* env
            vars, instance role, IRSA); the warehouse is optional
        """
        if self.catalog_type not in self.KNOWN_CATALOG_TYPES:
            return False
        if self.catalog_type == "glue":
            return True
        return bool(self.catalog_uri)


def load_iceberg_config(env: dict | None = None) -> IcebergConfig:
    """Build an :class:`IcebergConfig` from the environment (or a provided dict).

    Environment variables:
      ICEBERG_CATALOG_TYPE   (rest | sql | glue | hive | nessie; default rest;
                              an unknown value falls back to rest — same
                              LakehouseError-adjacent tolerance as before)
      ICEBERG_CATALOG_URI    (REST/Nessie endpoint, thrift://host:9083 for
                              hive, or sqlite:///path for sql)
      ICEBERG_CATALOG_TOKEN  (optional bearer token for REST/nessie/UC)
      ICEBERG_CATALOG_NAME   (catalog name; SQL catalogs partition by it)
      ICEBERG_WAREHOUSE      (optional table location root)
      ICEBERG_NAMESPACE      (optional namespace filter)
      ICEBERG_NESSIE_REF     (optional Nessie branch/tag to pin reads to)
      plus the S3_* variables for the storage the data files live on.
      For glue, the standard AWS_* environment variables (AWS_REGION,
      AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
      AWS_SESSION_TOKEN, AWS_PROFILE, ...) are read directly by boto3 —
      there are deliberately no SQLhandler-specific AWS knobs.
    """
    e = env if env is not None else os.environ
    ctype = _getenv("ICEBERG_CATALOG_TYPE", e.get("ICEBERG_CATALOG_TYPE", "rest")).strip().lower()
    if ctype not in IcebergConfig.KNOWN_CATALOG_TYPES:
        ctype = "rest"
    return IcebergConfig(
        catalog_type=ctype,
        catalog_uri=_getenv("ICEBERG_CATALOG_URI", e.get("ICEBERG_CATALOG_URI", "")),
        catalog_token=_getenv("ICEBERG_CATALOG_TOKEN", e.get("ICEBERG_CATALOG_TOKEN", "")),
        catalog_name=_getenv("ICEBERG_CATALOG_NAME", e.get("ICEBERG_CATALOG_NAME", "sqlhandler")),
        warehouse=_getenv("ICEBERG_WAREHOUSE", e.get("ICEBERG_WAREHOUSE", "")),
        namespace=_getenv("ICEBERG_NAMESPACE", e.get("ICEBERG_NAMESPACE", "")),
        nessie_ref=_getenv("ICEBERG_NESSIE_REF", e.get("ICEBERG_NESSIE_REF", "")),
        storage=load_s3_config(env),
    )


@dataclass(frozen=True)
class FileConfig:
    """Readable local / NFS mounted directory of Parquet files.

    Backed by pyarrow's LocalFileSystem, so it works for any directory
    mounted into the container (hostPath, NFS via PV/PVC, or other).
    Table discovery uses the same folder conventions as the S3 backend.
    """

    root_dir: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.root_dir)


def load_file_config(env: dict | None = None) -> FileConfig:
    """Build a :class:`FileConfig` from the environment.

    Environment variables:
      NFS_ROOT - absolute path to the mounted root directory (required)
      plus the SQLHANDLER_RAW_FORMATS / SQLHANDLER_RAW_MAX_FILE_MB knobs
      (see :func:`raw_formats_enabled` / :func:`raw_max_file_mb`) for
      raw-text landing-zone discovery on this backend
    """
    e = env if env is not None else os.environ
    return FileConfig(root_dir=_getenv("NFS_ROOT", e.get("NFS_ROOT", "")))


def load_backend_config(env: dict | None = None) -> tuple[str, object]:
    """Select and load the backend config from the environment.

    ``SQLHANDLER_BACKEND`` chooses the data source:
      * ``onelake`` (default) - Microsoft Fabric OneLake, Delta Lake (ABFS)
      * ``s3`` / ``minio``     - S3-compatible object store, Parquet files
      * ``nfs`` / ``file``     - local / mounted (NFS) directory, Parquet files
      * ``iceberg``            - Apache Iceberg REST/SQL catalog
      * ``sharing``            - Delta Sharing server (open protocol, read-only)
      * ``adls``               - Azure Data Lake Storage Gen2 (Parquet/Delta)
      * ``gcs``                - Google Cloud Storage (Parquet/Delta)

    Returns ``(backend_name, config)``; feed the config to
    :func:`sqlhandler.provider.make_provider` to build the provider.
    """
    e = env if env is not None else os.environ
    backend = _getenv("SQLHANDLER_BACKEND", e.get("SQLHANDLER_BACKEND", "onelake")).lower()
    if backend in ("nfs", "file", "local"):
        return "nfs", load_file_config(env)
    if backend in ("s3", "minio", "parquet"):
        return "s3", load_s3_config(env)
    if backend == "iceberg":
        return "iceberg", load_iceberg_config(env)
    if backend == "sharing":
        return "sharing", load_sharing_config(env)
    if backend == "adls":
        return "adls", load_adls_config(env)
    if backend == "gcs":
        return "gcs", load_gcs_config(env)
    return "onelake", load_config(env)


def load_source_providers(env: dict | None = None) -> DataProvider | None:
    """Build a federated :class:`MultiProvider` from ``SQLHANDLER_SOURCES``.

    ``SQLHANDLER_SOURCES`` is an optional JSON array of source objects. When
    set, the engine federates every source behind one endpoint (cross-source
    joins included); when unset, single-source mode behaves exactly as before.

    Each entry mirrors the backend env vars, e.g.::

      [
        {"name": "sales",      "backend": "s3",     "bucket": "bucket-a",
         "endpointUrl": "http://minio:9000", "accessKey": "...", "secretKey": "..."},
        {"name": "inventory",  "backend": "s3",     "bucket": "bucket-b",
         "prefix": "raw", "endpointUrl": "http://minio:9000",
         "accessKey": "...", "secretKey": "..."}
      ]

    Supported backends: ``s3``/``minio`` (S3_BUCKET/PREFIX/ENDPOINT_URL/
    REGION/ACCESS_KEY/SECRET_KEY/ANONYMOUS/USE_SSL), ``onelake``
    (abfssUrl or workspaceId+lakehouseId), ``nfs`` (rootDir) and ``iceberg``
    (catalogType/catalogUri/warehouse).

    Source labels (``name``) must be unique and valid identifiers
    (letters/digits/underscores, not starting with a digit) — they become
    the DuckDB name prefix for that source's tables. Violations raise
    ``ValueError`` at startup instead of silently shadowing a source.
    """
    e = env if env is not None else os.environ
    raw = _getenv("SQLHANDLER_SOURCES", e.get("SQLHANDLER_SOURCES", "")).strip()
    if not raw:
        return None
    try:
        sources = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SQLHANDLER_SOURCES is not valid JSON: {exc}") from exc
    if not isinstance(sources, list) or not sources:
        raise ValueError("SQLHANDLER_SOURCES must be a non-empty JSON array")

    from .provider import MultiProvider, make_provider

    labels: list[str] = []
    providers: list = []
    for idx, src in enumerate(sources):
        if not isinstance(src, dict):
            raise TypeError(f"SQLHANDLER_SOURCES[{idx}] must be an object")
        label = str(src.get("name") or f"source{idx + 1}")
        # Source labels become DuckDB identifier prefixes (source_schema_name)
        # and routing keys. Duplicates would silently make the earlier source
        # unreachable (dict(zip(...)) keeps the last), and non-identifier
        # characters produce ambiguous SQL names — both are user errors worth
        # failing on at startup rather than debugging at query time.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            raise ValueError(
                f"SQLHANDLER_SOURCES[{idx}] name {label!r} must be a valid identifier "
                "(letters, digits, underscores; not starting with a digit)"
            )
        if label in labels:
            raise ValueError(f"SQLHANDLER_SOURCES[{idx}] name {label!r} is duplicated; source names must be unique")
        backend = str(src.get("backend") or "s3").lower()
        src_env = {
            "SQLHANDLER_BACKEND": backend,
            "S3_ENDPOINT_URL": src.get("endpointUrl", ""),
            "S3_REGION": src.get("region", "us-east-1"),
            "S3_ACCESS_KEY": src.get("accessKey", ""),
            "S3_SECRET_KEY": src.get("secretKey", ""),
            "S3_SESSION_TOKEN": src.get("sessionToken", ""),
            "S3_BUCKET": src.get("bucket", ""),
            "S3_PREFIX": src.get("prefix", ""),
            "S3_ANONYMOUS": "true" if src.get("anonymous") else "false",
            "S3_USE_SSL": "true" if src.get("useSsl") else "false",
            "S3_FORMAT": src.get("format", "auto"),
            "FABRIC_LAKEHOUSE_ABFSS_URL": src.get("abfssUrl", ""),
            "FABRIC_WORKSPACE_ID": src.get("workspaceId", ""),
            "FABRIC_LAKEHOUSE_ID": src.get("lakehouseId", ""),
            "NFS_ROOT": src.get("rootDir", ""),
            "ICEBERG_CATALOG_TYPE": src.get("catalogType", "rest"),
            "ICEBERG_CATALOG_URI": src.get("catalogUri", ""),
            "ICEBERG_CATALOG_NAME": src.get("catalogName", "sqlhandler"),
            "ICEBERG_WAREHOUSE": src.get("warehouse", ""),
        }
        _, cfg = load_backend_config(src_env)
        providers.append(make_provider(cfg))
        labels.append(label)
    return MultiProvider(providers, labels)


@dataclass(frozen=True)
class CacheConfig:
    """In-process caching knobs for the SQL MCP server.

    ``list_tables`` and ``describe_table`` hit the lakehouse (DFS metadata and
    the Delta ``_delta_log``) on every call, and agents/OWUI re-run them on
    every session. Both are effectively static between ETL runs, so caching
    them in-process for a few minutes makes repeat calls near-instant without
    any external cache service.
    """

    ttl_seconds: int = 3600
    prewarm_tables: tuple[str, ...] = ()
    # Reuse open pyarrow Delta datasets per table (avoids re-reading the
    # Delta _delta_log on every query; the underlying rows still come from
    # OneLake on each scan). TTL in seconds, LRU cap in tables.
    dataset_cache_ttl: int = 3600
    dataset_cache_tables: int = 8
    # How often (seconds) the Delta snapshot version is re-checked to
    # invalidate a cached Dataset. 0 = check on every reuse.
    version_check_interval: int = 10
    # Serve list_tables from the cache immediately and refresh it in the
    # background (plus an automatic refresh every ttl_seconds), so callers
    # never block on the S3/DFS listing. Ignored when ttl_seconds == 0.
    list_async_refresh: bool = True

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0


def load_cache_config(env: dict | None = None) -> CacheConfig:
    """Build a :class:`CacheConfig` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      SQLHANDLER_CACHE_TTL       - seconds to keep list_tables / describe_table
                                   results in memory (0 disables caching)
      SQLHANDLER_PREWARM_TABLES  - comma-separated Delta table names whose
                                   schemas are warmed into the describe cache
                                   at server startup (the tables your queries
                                   hit most often)
      SQLHANDLER_DATASET_CACHE_TTL - seconds to reuse open Delta datasets
                                   per table (0 disables; default 3600)
      SQLHANDLER_DATASET_CACHE_TABLES - max tables kept in the dataset LRU
                                        (default 8)
      SQLHANDLER_VERSION_CHECK_INTERVAL - how often to re-check a Delta
                                        snapshot version (seconds; 0 = every
                                        reuse; default 10)
      SQLHANDLER_LIST_ASYNC_REFRESH - serve list_tables from the cache
                                        immediately and refresh in the
                                        background every ttl_seconds
                                        (true/1/on; default true)
    """
    e = env if env is not None else os.environ
    raw_ttl = _getenv("SQLHANDLER_CACHE_TTL", e.get("SQLHANDLER_CACHE_TTL", "3600"))
    try:
        ttl = int(raw_ttl)
    except ValueError:
        ttl = 3600
    raw_prewarm = _getenv("SQLHANDLER_PREWARM_TABLES", e.get("SQLHANDLER_PREWARM_TABLES", ""))
    prewarm = tuple(t.strip() for t in raw_prewarm.split(",") if t.strip())
    dttl = _to_int("SQLHANDLER_DATASET_CACHE_TTL", e, 3600)
    dcap = _to_int("SQLHANDLER_DATASET_CACHE_TABLES", e, 8)
    vci = _to_int("SQLHANDLER_VERSION_CHECK_INTERVAL", e, 10)
    async_list = _to_bool("SQLHANDLER_LIST_ASYNC_REFRESH", e, True)
    return CacheConfig(
        ttl_seconds=max(ttl, 0),
        prewarm_tables=prewarm,
        dataset_cache_ttl=max(dttl, 0),
        dataset_cache_tables=max(dcap, 0),
        version_check_interval=max(vci, 0),
        list_async_refresh=async_list,
    )


@dataclass(frozen=True)
class CompressionConfig:
    """HTTP response-compression knobs for the streamable-HTTP server.

    Agent-facing tool results are markdown/JSON-heavy text, so gzip
    compresses them 5-10x — a straight win for callers on constrained links
    (and for the ingress hop). SSE/streaming responses are never compressed:
    Starlette's GZipMiddleware excludes ``text/event-stream`` by default, and
    for plain streaming responses it compresses chunk-wise with a Z_SYNC_FLUSH
    per chunk, so streams stay incremental (nothing buffers whole-body).
    """

    # "gzip" (default) adds GZipMiddleware to the app stack; "off" removes it.
    mode: str = "gzip"
    # Responses smaller than this many bytes are passed through uncompressed
    # (compressing a probe-sized body costs more than it saves).
    min_size: int = 1024


def load_compression_config(env: dict | None = None) -> CompressionConfig:
    """Build a :class:`CompressionConfig` from the environment.

    Environment variables (also loadable from ``config/.env``):
      SQLHANDLER_COMPRESSION          - gzip (default) | off; any other value
                                        falls back to "gzip" (fail loud in the
                                        log at startup, never silently off)
      SQLHANDLER_COMPRESSION_MIN_SIZE - integer bytes below which responses
                                        are NOT compressed (default 1024;
                                        garbage falls back to the default)
    """
    e = env if env is not None else os.environ
    mode = _getenv("SQLHANDLER_COMPRESSION", e.get("SQLHANDLER_COMPRESSION", "gzip")).lower()
    if mode not in ("gzip", "off"):
        mode = "gzip"
    return CompressionConfig(mode=mode, min_size=_to_int("SQLHANDLER_COMPRESSION_MIN_SIZE", e, 1024))


def load_dotenv(path: str | None = None) -> None:
    """Minimal .env loader (no external dep). Reads ``KEY=VALUE`` lines.

    Set ``SQLHANDLER_ENV_FILE`` to point at your file, or pass ``path``. Values
    are only set into ``os.environ`` if not already present.
    """
    if path is None:
        path = os.environ.get("SQLHANDLER_ENV_FILE", "")
    if not path:
        # Conventional location: <repo>/config/.env next to the checked-out
        # src/ tree (two levels up from this module). Do NOT probe further up
        # the tree — picking up an unrelated sibling project's .env is worse
        # than finding nothing.
        here = os.path.dirname(os.path.abspath(__file__))
        for candidate in (os.path.join(here, "..", "..", "config", ".env"),):
            if os.path.exists(candidate):
                path = candidate
                break
    if not path or not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Delta Sharing backend (read-only, open protocol)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharingConfig:
    """Connection details for a Delta Sharing server (open protocol).

    Auth mirrors the protocol's standard *profile file* (what
    ``delta-sharing://...`` URLs and the reference CLI consume):

        shareCredentialsVersion: 1
        endpoint: https://<sharing-server>/delta-sharing
        bearerToken: <token>

    ``profile_path`` points at that file (YAML or JSON — the reference
    profile is YAML, but JSON is a subset of YAML so both parse identically
    via pyyaml). For env-only deployments without a file, DELTA_SHARING_ENDPOINT
    + DELTA_SHARING_BEARER_TOKEN override (or replace) the profile fields —
    the env endpoint/token win when both are set, matching how the other
    backends let env vars layer on top of file-based config.

    Secrets discipline: the bearer token lives ONLY in this config object
    and in the sharing client's request headers. It must never reach a tool
    response, a log line, or an exception message — the provider scrubs it
    from every error it raises (see :mod:`sqlhandler.sharing`).
    """

    endpoint: str = ""
    bearer_token: str = ""
    profile_path: str = ""
    # Sharing-server request timeout in seconds (list + query calls).
    timeout_seconds: int = 30

    @property
    def is_configured(self) -> bool:
        """Whether we have an endpoint AND a bearer token to authenticate."""
        return bool(self.endpoint and self.bearer_token)

    @property
    def display_endpoint(self) -> str:
        """Credential-free endpoint for tool output (scheme + host only)."""
        endpoint = self.endpoint
        if "://" in endpoint:
            scheme, _, rest = endpoint.partition("://")
            host = rest.partition("/")[0]
            return f"{scheme}://{host}"
        return endpoint.partition("/")[0]


def _read_sharing_profile(path: str) -> dict:
    """Parse a Delta Sharing profile file (YAML or JSON) into a dict.

    Both formats load through pyyaml (JSON is a YAML subset — the reference
    client does the same). A YAML document that parses to a non-mapping, or
    a file that fails to parse, raises :class:`LakehouseError` naming the
    problem without echoing file contents (the profile carries the token).
    """
    import yaml  # pyyaml is a hard dependency (semantic catalogs)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise LakehouseError(f"Delta Sharing profile file could not be read ({path}): {exc}") from exc
    except yaml.YAMLError as exc:
        raise LakehouseError(f"Delta Sharing profile file is not valid YAML/JSON: {path}") from exc
    if not isinstance(data, dict):
        raise LakehouseError(f"Delta Sharing profile file must be a YAML/JSON object: {path}")
    return data


def load_sharing_config(env: dict | None = None) -> SharingConfig:
    """Build a :class:`SharingConfig` from the environment (or a provided dict).

    Environment variables:
      SQLHANDLER_BACKEND          - "sharing" selects this backend
      DELTA_SHARING_PROFILE       - path to the profile file (YAML or JSON)
      DELTA_SHARING_ENDPOINT      - env-only endpoint override
      DELTA_SHARING_BEARER_TOKEN  - env-only token override (wins over profile)
      DELTA_SHARING_TIMEOUT       - request timeout seconds (default 30)

    Precedence: profile file first, then the DELTA_SHARING_ENDPOINT /
    DELTA_SHARING_BEARER_TOKEN env vars layered on top (either may be set
    alone — e.g. rotate the token via env while the endpoint stays in the
    profile). A configured profile file that is missing/unreadable raises
    ``LakehouseError`` at config-load time (startup), not at first query.
    """
    e = env if env is not None else os.environ
    profile_path = _getenv("DELTA_SHARING_PROFILE", e.get("DELTA_SHARING_PROFILE", ""))
    endpoint = _getenv("DELTA_SHARING_ENDPOINT", e.get("DELTA_SHARING_ENDPOINT", ""))
    token = _getenv("DELTA_SHARING_BEARER_TOKEN", e.get("DELTA_SHARING_BEARER_TOKEN", ""))
    if profile_path:
        # Profile fields are the BASE; env overrides win per-field. A
        # missing file is a loud config error (LakehouseError), consistent
        # with how a bad SQLHANDLER_ATTACH file fails at startup.
        profile = _read_sharing_profile(profile_path)
        endpoint = endpoint or str(profile.get("endpoint", "") or "").strip()
        token = token or str(profile.get("bearerToken", "") or "").strip()
    return SharingConfig(
        endpoint=endpoint.rstrip("/"),
        bearer_token=token,
        profile_path=profile_path,
        timeout_seconds=_to_int("DELTA_SHARING_TIMEOUT", e, 30),
    )


# ---------------------------------------------------------------------------
# ADLS Gen2 backend (read-only Parquet/Delta over abfs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdlsConfig:
    """Connection details for an Azure Data Lake Storage Gen2 account.

    ``account`` is the storage account name (the abfs host is built from it:
    ``abfs://<container>@<account>.dfs.<suffix>/<prefix>``), ``container`` is
    the filesystem (ADLS Gen2's term for a container), and ``prefix`` scopes
    the search to a sub-tree.

    Credentials mirror the S3 backend's shape: ``auth="anon"`` reads a public
    container (pyarrow 25 then sends NO Authorization header at all — verified
    against a capture server), while ``auth="client-secret"`` is the Entra
    service-principal client-credentials flow (tenant + client id + secret).
    pyarrow 25's ``AzureFileSystem`` implements client-secret NATIVELY (no
    azure-identity Python dependency at read time), so the same three values
    feed both the pyarrow filesystem and delta-rs's storage options.

    Secrets discipline: ``client_secret_env`` holds the NAME of the env var
    carrying the secret — never the secret itself (the onelake.py pattern,
    and what makes the helm chart render the value via ``secretKeyRef``).
    The secret value lives only inside the resolved config object.
    """

    account: str = ""
    container: str = ""
    prefix: str = ""
    # "anon" (public container / ambient identity) | "client-secret" (Entra
    # service principal client-credentials flow).
    auth: str = "anon"
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    # NAME of the env var holding the client secret (never the secret).
    client_secret_env: str = ""
    # Authority suffix for sovereign clouds: "core.windows.net" (default,
    # global Azure) | "core.usgovcloudapi.net" (US Gov) | "core.chinacloudapi.cn".
    endpoint_suffix: str = "core.windows.net"

    @property
    def is_configured(self) -> bool:
        """Whether we have enough to point at a container and authenticate."""
        if not (self.account and self.container):
            return False
        if self.auth == "anon":
            return True
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @property
    def dfs_authority(self) -> str:
        """The DFS endpoint host for this account (e.g. acct.dfs.core.windows.net)."""
        return f"{self.account}.dfs.{self.endpoint_suffix}"

    @property
    def blob_authority(self) -> str:
        """The Blob endpoint host (pyarrow probes it for HNS support)."""
        return f"{self.account}.blob.{self.endpoint_suffix}"


def load_adls_config(env: dict | None = None) -> AdlsConfig:
    """Build an :class:`AdlsConfig` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      ADLS_ACCOUNT            - storage account name (required)
      ADLS_CONTAINER          - filesystem/container name (required; the
                                ADLS_FILESYSTEM alias is accepted)
      ADLS_PREFIX             - optional sub-tree within the container
      ADLS_AUTH               - anon (default) | client-secret
      ADLS_TENANT_ID          - Entra tenant (client-secret mode)
      ADLS_CLIENT_ID          - Entra app/client id (client-secret mode)
      ADLS_CLIENT_SECRET_ENV  - NAME of the env var holding the client secret
                                (never the secret itself — the helm chart wires
                                that var from a Kubernetes Secret)
      ADLS_ENDPOINT_SUFFIX    - core.windows.net (default) | core.usgovcloudapi.net
                                | core.chinacloudapi.cn (sovereign clouds)

    In client-secret mode the secret is resolved from the named env var at
    load time; a configured-but-unresolvable secret is a loud
    ``LakehouseError`` (startup), not a mystery 401 at first query.
    """
    e = env if env is not None else os.environ
    auth = _getenv("ADLS_AUTH", e.get("ADLS_AUTH", "anon")).lower() or "anon"
    if auth not in ("anon", "client-secret"):
        raise LakehouseError(
            f"Invalid ADLS_AUTH {auth!r}: use 'anon' (public container) or "
            "'client-secret' (Entra service principal)."
        )
    secret_env_name = _getenv("ADLS_CLIENT_SECRET_ENV", e.get("ADLS_CLIENT_SECRET_ENV", ""))
    secret = ""
    if auth == "client-secret" and secret_env_name:
        secret = _getenv(secret_env_name, e.get(secret_env_name, ""))
        if not secret:
            raise LakehouseError(
                f"ADLS_CLIENT_SECRET_ENV names env var {secret_env_name!r} but it is not set "
                "— the client secret must be present in the environment (via a Kubernetes "
                "Secret secretKeyRef in helm, or export it for local runs)."
            )
    return AdlsConfig(
        account=_getenv("ADLS_ACCOUNT", e.get("ADLS_ACCOUNT", "")),
        container=_getenv("ADLS_CONTAINER", e.get("ADLS_CONTAINER", "") or e.get("ADLS_FILESYSTEM", "")),
        prefix=_getenv("ADLS_PREFIX", e.get("ADLS_PREFIX", "")),
        auth=auth,
        tenant_id=_getenv("ADLS_TENANT_ID", e.get("ADLS_TENANT_ID", "")),
        client_id=_getenv("ADLS_CLIENT_ID", e.get("ADLS_CLIENT_ID", "")),
        client_secret=secret,
        client_secret_env=secret_env_name,
        endpoint_suffix=_getenv(
            "ADLS_ENDPOINT_SUFFIX", e.get("ADLS_ENDPOINT_SUFFIX", "core.windows.net")
        ),
    )


# ---------------------------------------------------------------------------
# GCS backend (read-only Parquet/Delta over gs://)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GcsConfig:
    """Connection details for a Google Cloud Storage bucket.

    ``bucket`` is required and ``prefix`` scopes the search to a sub-tree
    (mirrors S3Config). Credentials: ``credentials_file`` is the path to a
    service-account JSON key file — pyarrow resolves it the same way
    ``GOOGLE_APPLICATION_CREDENTIALS`` does (application-default lookup), so
    the chart mounts the key file and this config points at the mounted path;
    ``anonymous=True`` reads a public bucket with no credential at all.

    Secrets discipline: a key FILE path (mounted read-only), never the key
    contents in values/env — the same posture as the sharing profile file.
    """

    bucket: str = ""
    prefix: str = ""
    project_id: str = ""
    # Path to a service-account JSON key file (mounted into the container).
    credentials_file: str = ""
    anonymous: bool = False

    @property
    def is_configured(self) -> bool:
        """Whether we have enough to point at a bucket and authenticate."""
        # With no explicit file, pyarrow falls back to ambient application-
        # default credentials (GOOGLE_APPLICATION_CREDENTIALS, workload
        # identity, metadata server) — a legitimate configuration.
        return bool(self.bucket)


def load_gcs_config(env: dict | None = None) -> GcsConfig:
    """Build a :class:`GcsConfig` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      GCS_BUCKET            - bucket name (required)
      GCS_PREFIX            - optional sub-tree within the bucket
      GCS_PROJECT_ID        - GCP project (only needed for bucket creation;
                              reads work without it)
      GCS_CREDENTIALS_FILE  - path to a service-account JSON key file
                              (mounted into the container; omit for ambient
                              application-default credentials)
      GCS_ANONYMOUS         - true/1/on reads a public bucket anonymously
    """
    e = env if env is not None else os.environ
    return GcsConfig(
        bucket=_getenv("GCS_BUCKET", e.get("GCS_BUCKET", "")),
        prefix=_getenv("GCS_PREFIX", e.get("GCS_PREFIX", "")),
        project_id=_getenv("GCS_PROJECT_ID", e.get("GCS_PROJECT_ID", "")),
        credentials_file=_getenv("GCS_CREDENTIALS_FILE", e.get("GCS_CREDENTIALS_FILE", "")),
        anonymous=_to_bool("GCS_ANONYMOUS", e, False),
    )


@dataclass(frozen=True)
class PerformanceConfig:
    """Performance-feature switches (preview fast path + data prewarm depth).

    Both features degrade to the plain code path when disabled — these are
    accelerators, never behavior contracts (a bare-LIMIT preview reads the
    same SQL semantics from fewer bytes; a data prewarm is invisible when
    the block cache is off).
    """

    # Bare `SELECT cols FROM t LIMIT n` previews take the first-row-group
    # fast path (SQLHANDLER_PREVIEW_FASTPATH; engine default ON — see
    # engine._preview_fastpath_enabled for the exact parsing).
    preview_fastpath: bool = True
    # First N row groups of each prewarm table read through the disk block
    # cache at startup (SQLHANDLER_PREWARM_ROWGROUPS; 0 = describe-only).
    prewarm_rowgroups: int = 1


def load_performance_config(env: dict | None = None) -> PerformanceConfig:
    """Build a :class:`PerformanceConfig` from the environment.

    Environment variables:
      SQLHANDLER_PREVIEW_FASTPATH   - on|off (default on; any of
                                      0/false/no/off switches the
                                      bare-LIMIT preview fast path off)
      SQLHANDLER_PREWARM_ROWGROUPS  - first N row groups prewarmed per
                                      table (default 1; 0 = off)
    """
    e = env if env is not None else os.environ
    raw_preview = _getenv("SQLHANDLER_PREVIEW_FASTPATH", e.get("SQLHANDLER_PREVIEW_FASTPATH", "")).lower()
    preview = True if not raw_preview else raw_preview not in ("0", "false", "no", "off")
    return PerformanceConfig(
        preview_fastpath=preview,
        prewarm_rowgroups=max(_to_int("SQLHANDLER_PREWARM_ROWGROUPS", e, 1), 0),
    )
