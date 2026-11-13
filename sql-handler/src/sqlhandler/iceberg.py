"""Apache Iceberg backend (read-only) via the pyiceberg catalog.

Iceberg tables live in an object store as Parquet/ORC data files plus a
metadata layer (snapshots, manifests with per-file stats). pyiceberg talks
to a catalog (REST by default, or a local SQL catalog) to list/describe
tables and resolve the current snapshot to its data files; this provider
exposes those files as a pyarrow Dataset, so the shared engine (DuckDB,
caches, scans) works unchanged:

  * list_tables / describe_table  -> catalog namespaces + table schema
  * open_dataset                  -> manifest-listed Parquet files as a
                                     pyarrow Dataset

Catalog types (``ICEBERG_CATALOG_TYPE``):
  * ``rest``    - Iceberg REST catalog (Dremio, Nessie's REST endpoint,
                  Databricks Unity Catalog, S3 Tables, ...)
  * ``sql``     - SQL catalog (SQLite/Postgres) for local dev/tests
  * ``glue``    - AWS Glue Data Catalog (needs the ``pyiceberg[glue]``
                    extras; AWS credentials/region come from the standard
                    AWS_* environment / instance role / IRSA — never values)
  * ``hive``    - Hive metastore over thrift (needs ``pyiceberg[hive]``)
  * ``nessie``  - Project Nessie via the REST catalog with the branch/tag
                    pinned in the URI (no native nessie type in pyiceberg)

Requires the optional dependency ``pyiceberg``:
    pip install 'sqlhandler[iceberg]'
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.dataset as pad

from .blockcache import maybe_block_cache
from .config import IcebergConfig, listing_max_entries
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version
from .s3 import build_s3fs

logger = logging.getLogger("sqlhandler.iceberg")

# Manifest data-file paths are REMOTE input (the catalog decides them). A
# file://, absolute or schemeless path reads the LOCAL filesystem — pyarrow
# datasets bypass the DuckDB fs lockdown — so local reads are opt-in AND
# realpath-contained in the warehouse (audit 2026-11-12 P1-12; same pattern
# as file.py:_contained_path).
LOCAL_FILES_ENV = "SQLHANDLER_ICEBERG_ALLOW_LOCAL_FILES"


def _local_files_allowed() -> bool:
    return os.environ.get(LOCAL_FILES_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _contain_local_path(local: str, warehouse: str, requested: str) -> str:
    """Return a local manifest path after the opt-in and containment gate.

    The path is checked realpath-style against ICEBERG_WAREHOUSE (a symlink
    planted inside cannot lead out — file.py's rule). Raises LakehouseError
    when local reads are disabled (the default) or the path escapes.
    """
    if not _local_files_allowed():
        raise LakehouseError(
            f"Iceberg manifest path {requested!r} addresses the local filesystem; local reads are "
            f"disabled by default — set {LOCAL_FILES_ENV}=1 to allow warehouse-contained local files"
        )
    wh = (warehouse or "").rstrip("/")
    if wh:
        root = os.path.realpath(wh)
        target = os.path.realpath(local if os.path.isabs(local) else os.path.join(root, local))
        if target != root and not target.startswith(root + os.sep):
            raise LakehouseError(f"Iceberg manifest path {requested!r} escapes the warehouse {wh!r}")
        return target
    # Opted in with no warehouse configured: only an absolute local path is
    # meaningful — a relative one would silently resolve against the CWD.
    if not os.path.isabs(local):
        raise LakehouseError(
            f"Iceberg manifest path {requested!r} is relative but no ICEBERG_WAREHOUSE is configured to contain it"
        )
    return local


def _scrub(text: str, *secrets: str) -> str:
    """Replace secret occurrences with '***' (s3/adls/gcs pattern).

    check_connection's string reaches the readiness surface; it is the one
    error path per provider that did not scrub (audit 2026-11-12 P3).
    """
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


_MISSING_MSG = (
    "Iceberg support requires the optional 'pyiceberg' package. Install with:  pip install 'sqlhandler[iceberg]'"
)

# The glue/hive catalog classes import boto3/thrift at module load — pyiceberg
# raises its own NotInstalledError when the extra is missing; we translate it
# into a LakehouseError with the right pip target.
_GLUE_MISSING_MSG = (
    "Iceberg catalog type 'glue' requires the pyiceberg AWS extras (boto3/botocore). "
    "Install with:  pip install 'pyiceberg[glue]'  (AWS credentials come from the "
    "standard AWS_* environment variables / instance role / IRSA — never config values)"
)
_HIVE_MISSING_MSG = (
    "Iceberg catalog type 'hive' requires the pyiceberg thrift extra. Install with:  pip install 'pyiceberg[hive]'"
)

# Remote object-store schemes pass through to pyarrow unchanged. file:// is
# deliberately NOT remote: it addresses the LOCAL filesystem and goes through
# the _contain_local_path gate (audit 2026-11-12 P1-12).
_REMOTE_SCHEMES = ("s3://", "gs://", "abfs://", "abfss://")
# Schemes _REMOTE_SCHEMES carries that open_dataset cannot actually READ yet
# (only s3:// has a filesystem branch; gs://abfs:// used to fail deep inside
# pyarrow with a confusing error — audit 2026-11-12 P2): refuse them at the
# normalization edge with the fix-shaped message instead.
_UNSUPPORTED_REMOTE = ("gs://", "abfs://", "abfss://")


class IcebergProvider(DataProvider):
    """Apache Iceberg catalog backend (REST/SQL/glue/hive/nessie), read-only."""

    kind = "iceberg"

    def __init__(self, config: IcebergConfig):
        """Wrap a pyiceberg catalog + its storage credentials."""
        if not config.is_configured:
            raise LakehouseError(
                "Iceberg connection is not configured. Set ICEBERG_CATALOG_URI "
                "(the REST/Nessie/thrift endpoint) or ICEBERG_CATALOG_TYPE=glue "
                "with the AWS_* environment set."
            )
        self.config = config
        self._cat = None

    # ------------------------------------------------------------ catalog
    def _catalog(self):
        """Lazily build the pyiceberg catalog from the config."""
        if self._cat is None:
            try:
                from pyiceberg.catalog import load_catalog
                from pyiceberg.catalog.sql import SqlCatalog
            except ImportError as exc:
                raise LakehouseError(_MISSING_MSG) from exc
            try:
                from pyiceberg.exceptions import NotInstalledError
            except ImportError:  # pragma: no cover - ancient pyiceberg
                NotInstalledError = ()
            cfg = self.config
            try:
                if cfg.catalog_type == "sql":
                    self._cat = SqlCatalog(
                        cfg.catalog_name,
                        uri=cfg.catalog_uri,
                        warehouse=cfg.warehouse or None,
                    )
                elif cfg.catalog_type == "glue":
                    # boto3 resolves credentials/region from the standard AWS
                    # environment (env vars, instance role, IRSA) — NO
                    # credential values ever travel through this config.
                    self._cat = load_catalog(
                        "default",
                        type="glue",
                        warehouse=cfg.warehouse or None,
                    )
                elif cfg.catalog_type == "hive":
                    self._cat = load_catalog(
                        "default",
                        type="hive",
                        uri=cfg.catalog_uri,
                        warehouse=cfg.warehouse or None,
                    )
                elif cfg.catalog_type == "nessie":
                    # No native "nessie" type in pyiceberg's registry: Nessie's
                    # Iceberg REST endpoint is a plain REST catalog whose
                    # branch/tag rides the URI path (the REST `prefix`
                    # property does NOT work for pyiceberg — projectnessie.org).
                    self._cat = load_catalog(
                        "default",
                        type="rest",
                        uri=self._nessie_uri(),
                        token=cfg.catalog_token or None,
                        warehouse=cfg.warehouse or None,
                    )
                else:
                    self._cat = load_catalog(
                        "default",
                        type="rest",
                        uri=cfg.catalog_uri,
                        token=cfg.catalog_token or None,
                        warehouse=cfg.warehouse or None,
                    )
            except NotInstalledError as exc:
                msg = _GLUE_MISSING_MSG if cfg.catalog_type == "glue" else _HIVE_MISSING_MSG
                raise LakehouseError(f"{msg} ({exc})") from exc
            except Exception as exc:
                raise LakehouseError(f"Iceberg catalog init failed: {exc}") from exc
        return self._cat

    def _nessie_uri(self) -> str:
        """The Nessie Iceberg REST URI with the pinned branch/tag appended.

        Per projectnessie.org's Iceberg-REST guide the ref travels in the URI
        (``http://host:19120/iceberg/<branch>``); a URI that already carries a
        branch/warehouse segment (``.../iceberg/<ref>`` or
        ``.../iceberg/<ref>|<warehouse>``) wins — the env ref is then ignored.
        """
        uri = (self.config.catalog_uri or "").rstrip("/")
        ref = (self.config.nessie_ref or "").strip().strip("/")
        if not ref:
            return uri
        path = urlparse(uri).path
        _, _, rest = path.partition("/iceberg")
        if rest and rest != "/":
            return uri
        return f"{uri}/{ref}"

    def _namespaces(self) -> list[str]:
        """Namespaces (schemas) to list, after the optional namespace filter."""
        catalog = self._catalog()
        try:
            namespaces = [ns[0] for ns in catalog.list_namespaces()]
        except Exception as exc:
            raise LakehouseError(f"Iceberg namespace listing failed: {exc}") from exc
        if self.config.namespace:
            namespaces = [ns for ns in namespaces if ns == self.config.namespace]
        return sorted(namespaces)

    # -------------------------------------------------------------- listing
    def list_tables(self) -> list[TableInfo]:
        """Enumerate Iceberg tables across the catalog's namespaces."""
        catalog = self._catalog()
        infos: list[TableInfo] = []
        for ns in self._namespaces():
            try:
                tables = catalog.list_tables(ns)
            except Exception as exc:
                logger.warning("Iceberg list_tables(%s) failed: %s", ns, exc)
                continue
            for _, name in tables:
                infos.append(TableInfo(name=name, schema=ns, format="parquet"))
                cap = listing_max_entries()
                if cap and len(infos) > cap:
                    raise LakehouseError(
                        f"Iceberg listing exceeded SQLHANDLER_LISTING_MAX_ENTRIES={cap} tables "
                        "(refusing rather than silently truncating — narrow ICEBERG_NAMESPACE or raise the knob)"
                    )
        return sorted(infos, key=lambda ti: ti.path)

    def table_uri(self, info: TableInfo) -> str:
        """The table's storage location (its warehouse path)."""
        return str(self._load_table(info).location())

    def check_connection(self) -> str | None:
        """Cheap readiness check: resolve the catalog namespaces."""
        try:
            self._namespaces()
            return None
        except Exception as exc:
            return _scrub(
                str(exc),
                self.config.catalog_token,
                self.config.storage.access_key,
                self.config.storage.secret_key,
            )

    # ------------------------------------------------------------- dataset
    def _load_table(self, info: TableInfo):
        """Load a pyiceberg table for a logical schema/name."""
        try:
            return self._catalog().load_table((info.schema, info.name))
        except Exception as exc:
            raise LakehouseError(f"Iceberg load_table {info.path} failed: {exc}") from exc

    def _normalize_path(self, path: str) -> str:
        """Make a data-file path usable by pyarrow on the right filesystem.

        Remote-scheme paths pass through. file://, absolute and schemeless
        paths address the LOCAL filesystem: opt-in (default off) and
        realpath-contained in ICEBERG_WAREHOUSE — the catalog manifest must
        not choose what this pod reads off its own disk (P1-12).
        """
        for scheme in _UNSUPPORTED_REMOTE:
            if path.startswith(scheme):
                raise LakehouseError(
                    f"Iceberg data file {path!r} uses unsupported scheme {scheme!r} — "
                    "this backend reads s3:// warehouses (or local files behind the opt-in); "
                    "use s3 or a local warehouse, or open a GitHub issue for this scheme"
                )
        if any(path.startswith(s) for s in _REMOTE_SCHEMES):
            return path
        if path.startswith("file://"):
            return _contain_local_path(path[len("file://") :], self.config.warehouse, path)
        wh = (self.config.warehouse or "").rstrip("/")
        if os.path.isabs(path):
            return _contain_local_path(path, wh, path)
        # Schemeless relative: under a REMOTE warehouse it names a remote
        # object (the normal Iceberg layout — no local read involved);
        # otherwise it is a local path and goes through the gate.
        candidate = f"{wh}/{path}" if wh else path
        if any(candidate.startswith(s) for s in _REMOTE_SCHEMES):
            return candidate
        return _contain_local_path(candidate, wh, path)

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open an Iceberg table as a pyarrow Dataset (cached by the engine).

        The dataset is built from the Parquet files listed in the table's
        snapshot manifest, so only the metadata (not the rows) is fetched
        here; predicates/projections still push down into the Parquet scan
        at query time. ``version`` selects a historical snapshot id (time
        travel); ``None`` scans the current snapshot.
        """
        table = self._load_table(info)
        scan = (
            table.scan() if version is None else table.scan(snapshot_id=_validate_snapshot_version(version, "Iceberg"))
        )
        try:
            files = [f.file.file_path for f in scan.plan_files()]
        except Exception as exc:
            raise LakehouseError(f"Iceberg scan planning {info.path} failed: {exc}") from exc
        files = [self._normalize_path(p) for p in files]
        if not files:
            # Empty table: return an empty dataset with the table's schema.
            schema = table.schema().as_arrow()
            empty = pa.table([pa.array([], type=f.type) for f in schema], schema=schema)
            return pad.dataset(empty)
        if any(p.startswith("s3://") for p in files):
            fs = maybe_block_cache(build_s3fs(self.config.storage), purpose=f"iceberg:{info.path}")
            paths = [p[len("s3://") :] for p in files]
            try:
                return pad.dataset(paths, filesystem=fs, format="parquet")
            except Exception as exc:
                raise LakehouseError(f"Could not open Iceberg dataset {info.path}: {exc}") from exc
        try:
            return pad.dataset(files, format="parquet")
        except Exception as exc:
            raise LakehouseError(f"Could not open Iceberg dataset {info.path}: {exc}") from exc
