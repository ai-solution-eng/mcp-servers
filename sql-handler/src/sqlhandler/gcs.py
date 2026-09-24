"""Google Cloud Storage backend for Parquet/Delta data.

This is the GCS-flavoured DataProvider: it discovers "tables" as Parquet
files under a bucket+prefix and exposes each as a pyarrow Dataset — the S3
backend's sibling with pyarrow's native ``GcsFileSystem`` (uniform bucket
access, no S3-interop shim) and delta-rs's GCS object-store client.

Credentials (see :class:`sqlhandler.config.GcsConfig`):

* ``anonymous=True`` — public buckets, no credential lookup at all.
* a credentials file path — a service-account JSON key file. pyarrow 25's
  ``GcsFileSystem`` has NO constructor parameter for the key file: it
  resolves credentials the application-default way, i.e. the
  ``GOOGLE_APPLICATION_CREDENTIALS`` env var (or the ambient metadata server
  on GCP). This module therefore sets ``GOOGLE_APPLICATION_CREDENTIALS`` to
  the configured file path for the process when one is configured — the
  documented pyarrow mechanism, not a workaround — and only when the
  operator has not already pointed it somewhere (an existing value wins; a
  mounted key file plus a metadata-server deployment conflict, and ambient
  wins by pyarrow's own precedence).

Delta tables ride delta-rs (its ``google_service_account`` storage option
consumes the same key file) — the onelake.py storage-options pattern.
Block-cache decision: the parquet/raw path wraps cleanly through
``maybe_block_cache`` (pure pyarrow filesystem); the Delta path rebuilds
``DeltaStorageHandler`` + wraps it, the same shape OneLake proved on pyarrow
25 — the handler is a pure PyFileSystem over gs:// URIs and never touches
the GcsFileSystem object.
"""

from __future__ import annotations

import json
import logging
import os

import pyarrow.dataset as pad
import pyarrow.fs as pafs

from .config import GcsConfig
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version
from .rawfiles import (
    build_raw_dataset,
    classify_raw_file,
    is_raw_format,
    log_skipped,
    raw_discovery_enabled,
    raw_size_cap_mb,
    raw_table_within_cap,
)

logger = logging.getLogger("sqlhandler.gcs")

_PARQUET_SUFFIX = ".parquet"
_DELTA_LOG = "_delta_log"


def build_gcs_fs(config: GcsConfig) -> pafs.GcsFileSystem:
    """Create a pyarrow GcsFileSystem from a GcsConfig.

    Anonymous for public buckets; otherwise the application-default chain,
    pinned to the configured key file via ``GOOGLE_APPLICATION_CREDENTIALS``
    when one is set and the env var does not already point somewhere (see
    the module docstring for why the env var — not a constructor param —
    carries the file path on pyarrow 25).

    ``SQLHANDLER_GCS_OPTIONS`` (a JSON object) is merged into the constructor
    kwargs (the S3_OPTIONS pattern): emulator endpoints, retry limits,
    custom endpoints for fake-gcs-server in tests. Malformed JSON fails
    loudly.
    """
    if config.credentials_file and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = config.credentials_file
    kwargs: dict = {"anonymous": config.anonymous}
    if config.project_id:
        kwargs["project_id"] = config.project_id
    raw = os.environ.get("SQLHANDLER_GCS_OPTIONS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object")  # noqa: TRY004
            kwargs.update(extra)
        except Exception as exc:
            raise LakehouseError(f"SQLHANDLER_GCS_OPTIONS is not valid JSON options: {exc}") from exc
    try:
        return pafs.GcsFileSystem(**kwargs)
    except Exception as exc:
        raise LakehouseError(f"Could not create GCS filesystem: {exc}") from exc


def gcs_delta_storage_options(config: GcsConfig) -> dict:
    """delta-rs object-store options mirroring the GCS config.

    Verified against deltalake 1.6.3's Azure/GCS resolver: a key file rides
    ``google_service_account`` (delta-rs opens it itself, so the
    ``GOOGLE_APPLICATION_CREDENTIALS`` env dance is unnecessary there);
    anonymous public buckets ride ``skip_signature``. Any other key passes
    through to delta-rs untouched (``SQLHANDLER_GCS_STORAGE_OPTIONS``).
    """
    opts: dict = {}
    if config.credentials_file:
        opts["google_service_account"] = config.credentials_file
    if config.anonymous:
        opts["skip_signature"] = "true"
    raw = os.environ.get("SQLHANDLER_GCS_STORAGE_OPTIONS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object")  # noqa: TRY004
            opts.update(extra)
        except Exception as exc:
            raise LakehouseError(
                f"SQLHANDLER_GCS_STORAGE_OPTIONS is not valid JSON options: {exc}"
            ) from exc
    return opts


class GcsProvider(DataProvider):
    """Google Cloud Storage (gs://) Parquet/Delta backend — S3's GCP sibling."""

    kind = "gcs"

    def __init__(self, config: GcsConfig):
        """Wrap credentials + listing/read access for one GCS bucket/prefix."""
        if not config.is_configured:
            raise LakehouseError(
                "GCS connection is not configured. Set GCS_BUCKET, plus "
                "GCS_CREDENTIALS_FILE (a mounted service-account JSON key file) "
                "or GCS_ANONYMOUS=true for a public bucket."
            )
        self.config = config
        self._fs: pafs.GcsFileSystem | None = None

    # ------------------------------------------------------------- client
    def _gcsfs(self) -> pafs.GcsFileSystem:
        """Lazily build (and keep) the pyarrow GCS filesystem handle."""
        if self._fs is None:
            self._fs = build_gcs_fs(self.config)
        return self._fs

    # -------------------------------------------------------- addressing
    def _base(self) -> str:
        """Bucket/prefix root (no leading or trailing slash), e.g. 'mybucket'."""
        parts = [self.config.bucket.strip("/")]
        if self.config.prefix:
            parts.append(self.config.prefix.strip("/"))
        return "/".join(p for p in parts if p)

    def table_uri(self, info: TableInfo) -> str:
        """gs:// URI of the table folder (or single Parquet file)."""
        location = info.location or info.path
        return f"gs://{self._base()}/{location}"

    @staticmethod
    def _is_parquet(name: str) -> bool:
        return name.lower().endswith(_PARQUET_SUFFIX)

    def _derive(self, rel: str, fmt: str = "parquet") -> TableInfo | None:
        """Map a bucket-relative data-file path to a TableInfo.

        Byte-identical semantics to s3.py's ``_derive`` (file -> table by
        stem, folder -> table, schema folder one level up, Hive partition
        folders folded, hidden folders skipped, raw stems drop the FULL raw
        suffix incl. ``.gz``).
        """
        parts = [p for p in rel.split("/") if p]
        if not parts:
            return None
        if fmt == "parquet" and not self._is_parquet(parts[-1]):
            return None
        if any(p.startswith(".") for p in parts[:-1]):
            return None
        dirs = list(parts[:-1])
        # Skip Hive partition folders (e.g. dt=2024) - they are not tables.
        while dirs and "=" in dirs[-1]:
            dirs.pop()
        if not dirs:
            stem = parts[-1]
            if fmt == "parquet":
                name = stem[: -len(_PARQUET_SUFFIX)]
            else:
                # raw single file: strip the FULL raw suffix (incl. .gz),
                # so orders.csv.gz -> orders and events.jsonl -> events
                lower = stem.lower()
                raw_fmt = classify_raw_file(stem) or ""
                suffix = "." + raw_fmt
                name = stem[: -len(suffix)] if raw_fmt and lower.endswith(suffix) else stem.rsplit(".", 1)[0]
            return TableInfo(name=name, schema="default", format=fmt, location=stem)
        name = dirs[-1]
        schema = dirs[-2] if len(dirs) >= 2 else "default"
        location = "/".join(dirs)
        return TableInfo(name=name, schema=schema, format=fmt, location=location)

    # ----------------------------------------------------------- listing
    def list_tables(self) -> list[TableInfo]:
        """Enumerate the tables under bucket/prefix (recursive).

        Delta detection (``_delta_log`` folders), the raw landing-zone
        (``SQLHANDLER_RAW_FORMATS`` / ``SQLHANDLER_RAW_MAX_FILE_MB``), and the
        Delta > Parquet > raw precedence all mirror s3.py exactly.
        """
        fs = self._gcsfs()
        root = self._base()
        try:
            selector = pafs.FileSelector(root, recursive=True)
            infos = fs.get_file_info(selector)
        except Exception as exc:
            raise LakehouseError(f"Could not list GCS path '{root}': {exc}") from exc

        seen: dict[str, TableInfo] = {}
        delta_roots: set[str] = set()
        for fi in infos:
            if fi.type != pafs.FileType.File:
                continue
            parts = fi.path.split("/")
            if _DELTA_LOG in parts:
                delta_roots.add("/".join(parts[: parts.index(_DELTA_LOG)]))
        for dr in sorted(delta_roots):
            rel = dr[len(root) :].strip("/")
            if not rel:
                continue
            parts = rel.split("/")
            name = parts[-1]
            schema = parts[-2] if len(parts) >= 2 else "default"
            seen[f"{schema}/{name}"] = TableInfo(name=name, schema=schema, format="delta", location=rel)

        want_raw = raw_discovery_enabled()
        cap_mb = raw_size_cap_mb() if want_raw else 0
        raw_candidates: dict[str, dict[str, list[tuple[str, int]]]] = {}
        for fi in infos:
            if fi.type != pafs.FileType.File:
                continue
            rel = fi.path[len(root) :].strip("/")
            if not rel:
                continue
            if any(fi.path.startswith(dr + "/") for dr in delta_roots):
                continue  # data file inside a Delta table, not its own table
            if _DELTA_LOG in rel.split("/"):
                # Delta log JSON is table METADATA, never a raw landing-zone
                # file (the same exclusion s3.py made after integration).
                continue
            if self._is_parquet(rel):
                info = self._derive(rel)
                if info is not None:
                    seen.setdefault(info.path, info)
            elif want_raw:
                raw_fmt = classify_raw_file(rel)
                if raw_fmt is None:
                    continue
                info = self._derive(rel, fmt=raw_fmt)
                if info is not None:
                    raw_candidates.setdefault(info.path, {}).setdefault(raw_fmt, []).append(
                        (fi.path, int(fi.size or 0))
                    )

        for path in sorted(raw_candidates):
            by_fmt = raw_candidates[path]
            if path in seen:  # parquet (or delta) already owns this table path
                log_skipped(
                    path,
                    "ignored raw file(s) — the table folder is "
                    f"{'delta' if seen[path].format == 'delta' else 'parquet'}",
                )
                continue
            fmts = sorted(by_fmt)
            fmt = fmts[0]
            if len(fmts) > 1:
                logger.warning(
                    "raw-format collision on table %s: %s — using %s (first alphabetically)",
                    path,
                    ", ".join("." + f for f in fmts),
                    "." + fmt,
                )
            entries = by_fmt[fmt]
            sizes = [size for _, size in entries]
            if not raw_table_within_cap(sizes, cap_mb):
                log_skipped(
                    path,
                    f"raw file(s) exceed SQLHANDLER_RAW_MAX_FILE_MB={cap_mb} (cap applies to compressed size for .gz)",
                )
                continue
            info = self._derive(entries[0][0][len(root) :].strip("/"), fmt=fmt)
            if info is not None:
                # Raw tables open over their EXACT file list (absolute
                # bucket-relative paths on the GCS filesystem).
                object.__setattr__(info, "raw_files", [key for key, _ in entries])
                seen[info.path] = info
        return sorted(seen.values(), key=lambda ti: ti.path)

    # ------------------------------------------------------------- delta
    def _open_delta(self, info: TableInfo, version: int | None = None):
        """Open a deltalake DeltaTable over gs:// (optionally a past version)."""
        try:
            from deltalake import DeltaTable as DeltaTableCls
        except ImportError as exc:  # pragma: no cover - deltalake is a hard dep
            raise LakehouseError(f"deltalake is required for GCS Delta tables: {exc}") from exc
        uri = self.table_uri(info)
        try:
            if version is None:
                return DeltaTableCls(uri, storage_options=gcs_delta_storage_options(self.config))
            return DeltaTableCls(
                uri, version=version, storage_options=gcs_delta_storage_options(self.config)
            )
        except Exception as exc:
            raise LakehouseError(f"Could not open GCS Delta table {info.path!r}: {exc}") from exc

    def _delta_version(self, info: TableInfo) -> int | None:
        """Latest Delta version from the table's _delta_log keys (cheap list)."""
        location = info.location or info.path
        log_dir = f"{self._base()}/{location}/{_DELTA_LOG}"
        try:
            entries = self._gcsfs().get_file_info(pafs.FileSelector(log_dir, recursive=False))
        except Exception:
            return None
        versions = [0]
        for fi in entries:
            stem = fi.path.split("/")[-1].split(".", 1)[0]
            if stem.isdigit():
                versions.append(int(stem))
        return max(versions)

    def check_version(self, info: TableInfo) -> int | None:
        """Cheap version token: Delta log listing (delta) / None (parquet)."""
        if info.format != "delta":
            return None
        try:
            return self._delta_version(info)
        except Exception:
            return None

    # ----------------------------------------------------------- dataset
    def check_connection(self) -> str | None:
        """Cheap readiness check: list the bucket/prefix root."""
        try:
            root = self._base()
            self._gcsfs().get_file_info(pafs.FileSelector(root, recursive=False))
            return None
        except Exception as exc:
            return str(exc)

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open a table as a pyarrow Dataset (cached by engine).

        Delta tables ride delta-rs; plain Parquet and raw-text tables open
        through the pyarrow GcsFileSystem. Plain Parquet and raw tables have
        no version history and refuse one, exactly like their s3.py
        counterparts.
        """
        if info.format == "delta":
            if version is not None:
                _validate_snapshot_version(version, "Delta")
                dt = self._open_delta(info, version=int(version))
            else:
                dt = self._open_delta(info)
            try:
                fs = self._delta_data_fs(dt, info, version)
                if fs is None:
                    return dt.to_pyarrow_dataset()
                return dt.to_pyarrow_dataset(filesystem=fs)
            except Exception as exc:
                raise LakehouseError(f"Could not open GCS Delta dataset {info.path!r}: {exc}") from exc
        if is_raw_format(info.format):
            if version is not None:
                raise LakehouseError(
                    f"Time travel is not supported for raw-format table '{info.path}' "
                    "(only Delta and Iceberg tables have version history)."
                )
            files = getattr(info, "raw_files", None)
            if not files:
                # Hand-constructed TableInfo (tests/tools): open the single
                # location — correct for single-file tables, and folder
                # tables re-derive nothing here by design (discovery owns
                # the file list).
                files = [f"{self._base()}/{info.location or info.path}"]
            fs = self._maybe_block_cache(info)
            try:
                return build_raw_dataset(info.location or info.path, info.format, list(files), fs)
            except Exception as exc:
                raise LakehouseError(
                    f"Could not open GCS raw-format table '{info.path}' ({info.format}): {exc}"
                ) from exc
        if version is not None:
            raise LakehouseError(
                f"Time travel is not supported for plain Parquet table '{info.path}' "
                "(only Delta and Iceberg tables have version history)."
            )
        location = info.location or info.path
        # GCS keys are flat so ".." cannot escape the bucket, but a traversal
        # segment is always a caller bug — fail fast (the s3.py rule).
        if any(part == ".." for part in location.split("/")):
            raise LakehouseError(f"Invalid GCS table location: {location!r}")
        fs = self._maybe_block_cache(info)
        root = f"{self._base()}/{location}"
        try:
            return pad.dataset(root, filesystem=fs, format="parquet")
        except Exception as exc:
            raise LakehouseError(f"Could not open GCS dataset '{info.path}': {exc}") from exc

    def _maybe_block_cache(self, info: TableInfo) -> pafs.FileSystem:
        """Wrap the pyarrow filesystem in the disk block cache when enabled.

        Parquet/raw reads go through the pyarrow GcsFileSystem, so the
        standard ``maybe_block_cache`` wrap applies unchanged. Delta
        data-file reads are wrapped at the ``DeltaStorageHandler`` layer in
        ``open_dataset``'s delta branch (snapshot-version-scoped, the
        onelake pattern).
        """
        from .blockcache import maybe_block_cache

        return maybe_block_cache(self._gcsfs(), purpose=f"gcs:{info.path}")

    def _delta_data_fs(self, dt, info: TableInfo, version: int | None):
        """The block-cache-wrapped filesystem for Delta data-file reads, or None.

        Mirrors onelake.py's ``_delta_data_fs``: when the disk block cache is
        enabled, rebuild the SAME handler delta-rs would build internally
        (``DeltaStorageHandler`` over the gs:// table URI with this provider's
        storage options, seeded with the snapshot's file sizes from the delta
        log) and wrap it in the cache. Verified feasible on pyarrow 25 /
        deltalake 1.6.3: the handler is a pure ``PyFileSystem`` over the URI
        and storage options — it never touches the GcsFileSystem object, so
        the wrap composes cleanly on this backend. Returns None — leaving
        delta-rs's built-in path in place — when the cache is disabled or
        anything fails: the cache is an accelerator, never a dependency.
        Snapshot consistency (delta-v<N> scope) matches onelake.py: a re-open
        at a different snapshot — a new ETL commit or a time-travel read —
        can never be served another snapshot's cached bytes.
        """
        from .blockcache import block_cache_enabled, maybe_block_cache

        if not block_cache_enabled():
            return None
        try:
            import pyarrow.fs as pafs_mod
            from deltalake.fs import DeltaStorageHandler

            handler = DeltaStorageHandler(
                self.table_uri(info),
                options=gcs_delta_storage_options(self.config),
                known_sizes=self._delta_file_sizes(dt),
            )
            snapshot = int(version) if version is not None else int(dt.version())
            return maybe_block_cache(
                pafs_mod.PyFileSystem(handler),
                purpose=f"gcs:{info.path}",
                scope=f"delta-v{snapshot}",
            )
        except Exception:
            logger.warning("GCS block-cache wrap failed; reading uncached", exc_info=True)
            return None

    @staticmethod
    def _delta_file_sizes(dt) -> dict[str, int] | None:
        """Relative data-file path -> size, from the snapshot's add actions.

        The delta log already carries every file's size, so seeding the
        handler with it (exactly what delta-rs does internally) saves one
        HEAD request per file per open. Returns None when the stats cannot
        be read and the cache falls back to stat()-per-file behavior.
        """
        try:
            import pyarrow as pa

            adds = pa.table(dt.get_add_actions(flatten=True))
            return dict(zip(adds.column("path").to_pylist(), adds.column("size_bytes").to_pylist()))
        except Exception:
            return None
