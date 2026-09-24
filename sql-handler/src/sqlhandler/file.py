"""Local / NFS mounted directory of Parquet and Delta Lake tables.

The "nfs" backend reads tables from a directory mounted into the container
(NFS via a PV/PVC, hostPath, or any volume). It supports both:
  * Apache Delta Lake tables - folders containing a _delta_log/
  * plain Parquet files/folders
Discovery mirrors the S3 backend for Parquet and adds Delta-log detection;
reading is backed by pyarrow's LocalFileSystem and deltalake, so no
credentials or endpoint are needed.

Raw-text landing zone (``SQLHANDLER_RAW_FORMATS=on``, the default): the same
conventions discover small csv/tsv/json/ndjson/jsonl files (``.gz`` variants
included) as raw tables — see :mod:`sqlhandler.rawfiles` for the exact rules
(raw suffixes, the per-file size cap — applied to the compressed size for
``.gz``, same as S3 so the feature semantics stay uniform across backends —
raw-vs-parquet precedence, and the collision policy). Partition folders fold
into the raw table by directory like Parquet's do, but partition-COLUMN
extraction stays Parquet-only — no ``key=value`` columns are derived for raw
tables (pyarrow hive partitioning is deliberately out of scope for the
landing zone). NFS reads are fast, but the cap applies here too so a landing
zone means the same thing on every backend.
"""

from __future__ import annotations

import logging
import os

import pyarrow.dataset as pad
import pyarrow.fs as pafs

from .config import FileConfig
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

logger = logging.getLogger("sqlhandler.file")

_PARQUET_SUFFIX = ".parquet"
_DELTA_LOG = "_delta_log"


class FileProvider(DataProvider):
    """NFS / local-filesystem backend: Delta Lake tables and Parquet files."""

    kind = "nfs"

    def __init__(self, config: FileConfig):
        """Wrap a mounted directory of Delta/Parquet tables."""
        if not config.is_configured:
            raise LakehouseError(
                "NFS/file backend is not configured. Set NFS_ROOT to the mounted directory containing the tables."
            )
        self.config = config
        self._lfs: pafs.LocalFileSystem | None = None

    def _fs(self) -> pafs.LocalFileSystem:
        if self._lfs is None:
            self._lfs = pafs.LocalFileSystem()
        return self._lfs

    def _root(self) -> str:
        return self.config.root_dir.rstrip("/")

    def _contained_path(self, info: TableInfo) -> str:
        """Absolute path for a table, verified to stay under the NFS root.

        The engine resolves user-supplied table names into
        ``<schema>/<name>`` locations; without this check a name like
        ``../secret.parquet`` would read files outside the mounted root.
        Symlinks are resolved on both sides (realpath) so a link planted
        inside the root cannot point out either.
        """
        location = info.location or info.path
        root = os.path.realpath(self._root())
        target = os.path.realpath(os.path.join(root, location))
        if target != root and not target.startswith(root + os.sep):
            raise LakehouseError(f"Table location {location!r} is outside the NFS root {self._root()!r}")
        return target

    def table_uri(self, info: TableInfo) -> str:
        """Absolute path of the table folder (or single Parquet file)."""
        return self._contained_path(info)

    @staticmethod
    def _is_parquet(name: str) -> bool:
        return name.lower().endswith(_PARQUET_SUFFIX)

    @staticmethod
    def _hidden(path_parts) -> bool:
        return any(p.startswith(".") for p in path_parts)

    def _derive(self, rel: str, fmt: str = "parquet") -> TableInfo | None:
        """Map a relative data-file path to a TableInfo (mirrors S3).

        ``fmt`` is the file's format string (``"parquet"`` or a raw-text
        suffix name from :func:`classify_raw_file`); raw single files at the
        root drop the whole raw suffix (``orders.csv.gz`` -> table
        ``orders``), matching the Parquet stem rule.
        """
        parts = [p for p in rel.split("/") if p]
        if not parts:
            return None
        if fmt == "parquet" and not self._is_parquet(parts[-1]):
            return None
        if self._hidden(parts[:-1]):
            return None
        dirs = list(parts[:-1])
        while dirs and "=" in dirs[-1]:
            dirs.pop()
        if not dirs:
            stem = parts[-1]
            if fmt == "parquet":
                name = stem[: -len(_PARQUET_SUFFIX)]
            else:
                # raw single file: strip the FULL raw suffix (incl. .gz),
                # so orders.csv.gz -> orders and events.jsonl -> events
                raw_suffix = "." + (classify_raw_file(stem) or "")
                name = stem[: -len(raw_suffix)] if stem.lower().endswith(raw_suffix) else stem.rsplit(".", 1)[0]
            return TableInfo(name=name, schema="default", format=fmt, location=stem)
        name = dirs[-1]
        schema = dirs[-2] if len(dirs) >= 2 else "default"
        location = "/".join(dirs)
        return TableInfo(name=name, schema=schema, format=fmt, location=location)

    def list_tables(self) -> list[TableInfo]:
        """Enumerate Delta Lake tables, Parquet tables and raw-text tables
        under the root.

        Delta/Parquet discovery is exactly as before (byte-identical for
        parquet-only directories). Raw-text landing-zone discovery — when
        ``SQLHANDLER_RAW_FORMATS=on`` (default) — folds in small csv/tsv/
        json/ndjson/jsonl tables with the same folder conventions and the
        shared rules of :mod:`sqlhandler.rawfiles`: Delta > Parquet > raw
        precedence per table path, alphabetical-by-suffix collision
        resolution with a warning, and the ``SQLHANDLER_RAW_MAX_FILE_MB``
        per-file cap (compressed size for ``.gz``) skipping whole tables
        with a log line.
        """
        fs = self._fs()
        root = self._root()
        try:
            selector = pafs.FileSelector(root, recursive=True)
            infos = fs.get_file_info(selector)
        except Exception as exc:
            raise LakehouseError(f"Could not list NFS path '{root}': {exc}") from exc

        dirs = [fi for fi in infos if fi.type == pafs.FileType.Directory]
        files = [fi for fi in infos if fi.type == pafs.FileType.File]

        # Delta tables: directories directly containing a _delta_log dir.
        delta_roots: set[str] = set()
        for d in dirs:
            if d.path.rstrip("/").endswith("/" + _DELTA_LOG):
                delta_roots.add(d.path.rstrip("/")[: -len("/" + _DELTA_LOG)])

        seen: dict[str, TableInfo] = {}
        for dr in sorted(delta_roots, key=len):
            rel = dr[len(root) :].strip("/")
            if not rel or self._hidden(rel.split("/")):
                continue
            parts = rel.split("/")
            name = parts[-1]
            schema = parts[-2] if len(parts) >= 2 else "default"
            location = "/".join(parts)
            ti = TableInfo(name=name, schema=schema, format="delta", location=location)
            seen.setdefault(ti.path, ti)

        want_raw = raw_discovery_enabled()
        cap_mb = raw_size_cap_mb() if want_raw else 0
        # Raw candidate collection mirrors the S3 provider exactly: gather
        # first (the listing is already in hand), then apply the cap and
        # precedence rules, so both backends share one decision path.
        raw_candidates: dict[str, dict[str, list[tuple[str, int]]]] = {}
        for fi in files:
            rel = fi.path[len(root) :].strip("/")
            if not rel:
                continue
            if any(fi.path.startswith(dr + "/") for dr in delta_roots):
                continue  # data files inside a Delta table, not their own table
            if _DELTA_LOG in rel.split("/"):
                # A Delta log JSON (t/_delta_log/00….json) is table METADATA,
                # not a raw landing-zone file — same exclusion as s3.py.
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
                # Raw tables open over their EXACT file list (absolute paths
                # on the local fs), never a folder scan — see rawfiles.py.
                object.__setattr__(info, "raw_files", [path_ for path_, _ in entries])
                seen[info.path] = info
        return sorted(seen.values(), key=lambda ti: ti.path)

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open a Delta table, Parquet folder/file or raw-text table as a
        pyarrow Dataset.

        ``version`` (Delta only) pins a historical snapshot for time travel;
        raw tables and plain Parquet have no version history and refuse one.
        Raw tables open over their discovered file list with the matching
        pyarrow format (csv / tab-delimited csv / newline-delimited json);
        schema inference happens at open and a failure surfaces as the
        standard LakehouseError.
        """
        root = self._contained_path(info)
        try:
            if info.format == "delta":
                from deltalake import DeltaTable

                if version is None:
                    return DeltaTable(root).to_pyarrow_dataset()
                _validate_snapshot_version(version, "Delta")
                return DeltaTable(root, version=int(version)).to_pyarrow_dataset()
            if is_raw_format(info.format):
                if version is not None:
                    raise LakehouseError(
                        f"Time travel is not supported for raw-format table '{info.path}' "
                        "(only Delta and Iceberg tables have version history)."
                    )
                files = getattr(info, "raw_files", None) or [root]
                return build_raw_dataset(info.location or info.path, info.format, list(files), self._fs())
            if version is not None:
                raise LakehouseError(
                    f"Time travel is not supported for plain Parquet table '{info.path}' "
                    "(only Delta and Iceberg tables have version history)."
                )
            return pad.dataset(root, filesystem=self._fs(), format="parquet")
        except LakehouseError:
            raise
        except Exception as exc:
            raise LakehouseError(f"Could not open NFS table '{info.path}': {exc}") from exc

    def check_version(self, info: TableInfo) -> int | None:
        """Latest Delta snapshot version from the local _delta_log listing.

        Cheap (a single directory listing) so the engine can refresh a cached
        Delta dataset right after an ETL commit. Non-Delta tables: None.
        """
        if info.format != "delta":
            return None
        try:
            delta_log = os.path.join(self._contained_path(info), _DELTA_LOG)
            names = os.listdir(delta_log)
        except (OSError, LakehouseError):
            return None
        versions = [0]
        for name in names:
            stem = name.split(".", 1)[0]
            if stem.isdigit():
                versions.append(int(stem))
        return max(versions)

    def check_connection(self) -> str | None:
        """Verify the mounted directory is present and readable."""
        try:
            root = self._root()
            fi = self._fs().get_file_info(root)
            if fi.type != pafs.FileType.Directory:
                return f"NFS root '{root}' is not a directory"
            return None
        except Exception as exc:
            return str(exc)
