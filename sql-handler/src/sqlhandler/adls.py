"""ADLS Gen2 (Azure Data Lake Storage Gen2) backend for Parquet/Delta data.

This is the ADLS-flavoured DataProvider: it discovers "tables" as Parquet
files under a container+prefix and exposes each as a pyarrow Dataset. It is
the S3 backend's sibling — same folder conventions, same raw landing-zone
rules, same Delta detection — with pyarrow's native ``AzureFileSystem``
(HNS/ADLS Gen2 is that filesystem's target storage type) and delta-rs's
Azure object-store client doing the talking.

Credentials (see :class:`sqlhandler.config.AdlsConfig`):

* ``anon`` — no credential parameters at all: pyarrow 25 sends an
  UNAUTHENTICATED request (verified: no Authorization header on the wire),
  which serves public containers, and falls through to ambient/DefaultAzure
  behavior for wired pods.
* ``client-secret`` — Entra service-principal client credentials. pyarrow's
  ``AzureFileSystem`` implements this NATIVELY on pyarrow 25
  (``tenant_id``/``client_id``/``client_secret`` constructor parameters, a
  ``ClientSecretCredential`` under the hood); no azure-identity Python object
  is threaded through. delta-rs takes the same three values as storage
  options (``azure_tenant_id``/``azure_client_id``/``azure_client_secret``,
  the exact keys onelake.py already passes for OneLake).

Sovereign clouds: ``endpoint_suffix`` rewrites both authorities
(``<account>.dfs.<suffix>`` and ``<account>.blob.<suffix>``) — US Gov and
China clouds need only the suffix, no other changes.

Delta tables ride delta-rs; when the disk block cache is on, their data-file
reads route through the cache-wrapped ``DeltaStorageHandler`` rebuild (the
onelake.py pattern, which composes cleanly on pyarrow 25 — the handler is a
pure PyFileSystem over the table URI and never touches the filesystem
object).

Known credential roadmap (honest limitations of pyarrow 25's surface):
``AzureFileSystem`` offers account-key and SAS-token modes too, but those
secrets are LONG-LIVED and weaker than Entra client credentials, so they are
deliberately not exposed as config; ``anonymous`` is not a constructor flag —
the no-credential-params shape above is the anonymous path. Managed identity
 rides the DefaultAzureCredential chain (ambient identity) via ``anon``.
"""

from __future__ import annotations

import json
import logging
import os

import pyarrow.dataset as pad
import pyarrow.fs as pafs

from .config import AdlsConfig
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

logger = logging.getLogger("sqlhandler.adls")

_PARQUET_SUFFIX = ".parquet"
_DELTA_LOG = "_delta_log"


def build_adls_fs(config: AdlsConfig) -> pafs.AzureFileSystem:
    """Create a pyarrow AzureFileSystem from an AdlsConfig (shared by the ADLS
    and Delta read paths' listing half).

    pyarrow 25's ``AzureFileSystem`` speaks ADLS Gen2 (hierarchical namespace)
    natively and detects HNS automatically. Credential resolution mirrors the
    config: no credential parameter in anon mode (unauthenticated request —
    public containers), tenant/client/secret in client-secret mode.

    ``SQLHANDLER_ADLS_OPTIONS`` (a JSON object) is merged into the constructor
    kwargs, so operators can tune pyarrow's Azure layer — emulator endpoints,
    retry limits — without code changes (the S3_OPTIONS pattern). Malformed
    JSON is an operator config error and fails loudly.
    """
    suffix = config.endpoint_suffix.strip().lstrip(".")
    kwargs: dict = {
        "account_name": config.account,
        "dfs_storage_authority": f"{config.account}.dfs.{suffix}",
        "blob_storage_authority": f"{config.account}.blob.{suffix}",
    }
    if config.auth == "client-secret":
        # All three are required together (pyarrow raises otherwise); the
        # config's is_configured gate already enforced the trio.
        kwargs.update(
            {
                "tenant_id": config.tenant_id,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            }
        )
    raw = os.environ.get("SQLHANDLER_ADLS_OPTIONS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object")  # noqa: TRY004
            kwargs.update(extra)
        except Exception as exc:
            raise LakehouseError(f"SQLHANDLER_ADLS_OPTIONS is not valid JSON options: {exc}") from exc
    try:
        return pafs.AzureFileSystem(**kwargs)
    except Exception as exc:
        # Never echo the secret: AzureFileSystem errors do not embed it, but
        # an operator-provided OPTIONS override could — scrub defensively.
        scrubbed = _scrub(str(exc), config.client_secret)
        raise LakehouseError(f"Could not create ADLS filesystem: {scrubbed}") from exc


def _scrub(text: str, *secrets: str) -> str:
    """Replace every secret occurrence in ``text`` with '***' (sharing.py pattern).

    Applied at every error-raising edge that could carry an
    operator-supplied override or a credential fragment. Best-effort by
    design (URL-encoded shapes slip past a literal replace) — the primary
    containment is that the secret never rides in a URI this module builds.
    """
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


class AdlsProvider(DataProvider):
    """ADLS Gen2 (abfs) Parquet/Delta backend — the S3 provider's Azure sibling."""

    kind = "adls"

    def __init__(self, config: AdlsConfig):
        """Wrap credentials + listing/read access for one ADLS container/prefix."""
        if not config.is_configured:
            raise LakehouseError(
                "ADLS Gen2 connection is not configured. Set ADLS_ACCOUNT and "
                "ADLS_CONTAINER, plus ADLS_AUTH=client-secret with ADLS_TENANT_ID / "
                "ADLS_CLIENT_ID / ADLS_CLIENT_SECRET_ENV for a private container "
                "(anon is the default for a public one)."
            )
        self.config = config
        self._fs: pafs.AzureFileSystem | None = None

    # ------------------------------------------------------------- client
    def _adlsfs(self) -> pafs.AzureFileSystem:
        """Lazily build (and keep) the pyarrow Azure filesystem handle."""
        if self._fs is None:
            self._fs = build_adls_fs(self.config)
        return self._fs

    # -------------------------------------------------------- addressing
    def _base(self) -> str:
        """Container/prefix root (no leading or trailing slash)."""
        parts = [self.config.container.strip("/")]
        if self.config.prefix:
            parts.append(self.config.prefix.strip("/"))
        return "/".join(p for p in parts if p)

    def table_uri(self, info: TableInfo) -> str:
        """abfs:// URI of the table folder (or single Parquet file)."""
        location = info.location or info.path
        return f"abfs://{self.config.container}@{self.config.dfs_authority}/{self._strip_container(location)}"

    def _strip_container(self, location: str) -> str:
        """Drop a leading container segment (paths here are container-relative)."""
        container = self.config.container.strip("/")
        parts = location.strip("/").split("/")
        if parts and parts[0] == container:
            return "/".join(parts[1:])
        return "/".join(p for p in parts if p)

    @staticmethod
    def _is_parquet(name: str) -> bool:
        return name.lower().endswith(_PARQUET_SUFFIX)

    def _derive(self, rel: str, fmt: str = "parquet") -> TableInfo | None:
        """Map a container-relative data-file path to a TableInfo.

        Byte-identical semantics to s3.py's ``_derive`` (kept here rather than
        shared because the raw-suffix stem rule reads clearer per backend and
        the two trees evolve independently): walk up past Hive partition
        folders to the table folder; the folder above it is the schema; a
        single file at the prefix root becomes a ``default``-schema table
        named by its stem (raw files drop the FULL raw suffix incl. ``.gz``).
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
        """Enumerate the tables under container/prefix (recursive).

        Delta detection (``_delta_log`` folders), the raw landing-zone
        (``SQLHANDLER_RAW_FORMATS`` / ``SQLHANDLER_RAW_MAX_FILE_MB``), and the
        Delta > Parquet > raw precedence all mirror s3.py exactly — the walk
        is the same shape; only the listing source (pyarrow AzureFileSystem
        instead of S3FileSystem) differs.
        """
        fs = self._adlsfs()
        root = self._base()
        try:
            selector = pafs.FileSelector(root, recursive=True)
            infos = fs.get_file_info(selector)
        except Exception as exc:
            raise LakehouseError(
                f"Could not list ADLS path '{self.config.container}/{root.strip('/') if root else ''}': "
                f"{_scrub(str(exc), self.config.client_secret)}"
            ) from exc

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
                # container-relative paths on the ADLS filesystem).
                object.__setattr__(info, "raw_files", [key for key, _ in entries])
                seen[info.path] = info
        return sorted(seen.values(), key=lambda ti: ti.path)

    # ------------------------------------------------------------- delta
    def _delta_storage_options(self) -> dict:
        """delta-rs object-store options mirroring the ADLS config.

        The ``azure_*`` keys are the exact trio onelake.py passes for OneLake;
        ``account_name`` plus the dfs/blob endpoint overrides handle both the
        public and the sovereign-cloud authorities. Anon mode omits every
        credential key (delta-rs then attempts unauthenticated reads).
        """
        c = self.config
        opts: dict = {
            "account_name": c.account,
            "dfs_endpoint": c.dfs_authority,
            "blob_endpoint": c.blob_authority,
        }
        if c.auth == "client-secret":
            opts.update(
                {
                    "azure_tenant_id": c.tenant_id,
                    "azure_client_id": c.client_id,
                    "azure_client_secret": c.client_secret,
                }
            )
        raw = os.environ.get("SQLHANDLER_ADLS_STORAGE_OPTIONS", "").strip()
        if raw:
            try:
                extra = json.loads(raw)
                if not isinstance(extra, dict):
                    raise ValueError("must be a JSON object")  # noqa: TRY004
                opts.update(extra)
            except Exception as exc:
                raise LakehouseError(
                    f"SQLHANDLER_ADLS_STORAGE_OPTIONS is not valid JSON options: {exc}"
                ) from exc
        return opts

    def _open_delta(self, info: TableInfo, version: int | None = None):
        """Open a deltalake DeltaTable over abfs:// (optionally a past version).

        The Delta path deliberately rides DELTA-RS (not the pyarrow
        filesystem): delta-rs owns the transaction-log protocol, and its
        Azure client consumes the same storage options onelake.py already
        proved in production. Block-cache decision: the onelake pattern
        (``DeltaStorageHandler`` rebuild + ``maybe_block_cache``) applies
        here unchanged — it wraps cleanly on pyarrow 25 because the handler
        is a pure ``PyFileSystem`` over delta-rs URIs and never touches the
        AzureFileSystem object — so ADLS Delta data reads route through the
        disk cache the same snapshot-version-scoped way OneLake's do.
        """
        try:
            from deltalake import DeltaTable as DeltaTableCls
        except ImportError as exc:  # pragma: no cover - deltalake is a hard dep
            raise LakehouseError(f"deltalake is required for ADLS Delta tables: {exc}") from exc
        uri = self.table_uri(info)
        try:
            if version is None:
                return DeltaTableCls(uri, storage_options=self._delta_storage_options())
            return DeltaTableCls(uri, version=version, storage_options=self._delta_storage_options())
        except Exception as exc:
            raise LakehouseError(
                f"Could not open ADLS Delta table {info.path!r}: {_scrub(str(exc), self.config.client_secret)}"
            ) from exc

    def _delta_version(self, info: TableInfo) -> int | None:
        """Latest Delta version from the table's _delta_log keys (cheap list)."""
        location = info.location or info.path
        log_dir = f"{self._base()}/{location}/{_DELTA_LOG}"
        try:
            entries = self._adlsfs().get_file_info(pafs.FileSelector(log_dir, recursive=False))
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
        """Cheap readiness check: list the container/prefix root."""
        try:
            root = self._base()
            self._adlsfs().get_file_info(pafs.FileSelector(root, recursive=False))
            return None
        except Exception as exc:
            return _scrub(str(exc), self.config.client_secret)

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open a table as a pyarrow Dataset (cached by engine).

        Delta tables ride delta-rs; when the disk block cache is ON their
        DATA-file reads route through the cache-wrapped ``DeltaStorageHandler``
        rebuild (see :meth:`_delta_data_fs`), the exact onelake.py pattern.
        Plain Parquet and raw-text tables open through the pyarrow
        AzureFileSystem. Plain Parquet and raw tables have no version history
        and refuse one, exactly like their s3.py counterparts.
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
                raise LakehouseError(
                    f"Could not open ADLS Delta dataset {info.path!r}: "
                    f"{_scrub(str(exc), self.config.client_secret)}"
                ) from exc
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
                    f"Could not open ADLS raw-format table '{info.path}' ({info.format}): "
                    f"{_scrub(str(exc), self.config.client_secret)}"
                ) from exc
        if version is not None:
            raise LakehouseError(
                f"Time travel is not supported for plain Parquet table '{info.path}' "
                "(only Delta and Iceberg tables have version history)."
            )
        location = info.location or info.path
        # ADLS paths are flat under the container, but a traversal segment is
        # always a caller bug — fail fast instead of a confusing 404.
        if any(part == ".." for part in location.split("/")):
            raise LakehouseError(f"Invalid ADLS table location: {location!r}")
        fs = self._maybe_block_cache(info)
        root = f"{self._base()}/{location}"
        try:
            return pad.dataset(root, filesystem=fs, format="parquet")
        except Exception as exc:
            raise LakehouseError(
                f"Could not open ADLS dataset '{info.path}': {_scrub(str(exc), self.config.client_secret)}"
            ) from exc

    def _maybe_block_cache(self, info: TableInfo) -> pafs.FileSystem:
        """Wrap the pyarrow filesystem in the disk block cache when enabled.

        Parquet/raw reads go through the pyarrow AzureFileSystem, so the
        standard ``maybe_block_cache`` wrap applies unchanged. Delta data-file
        reads are wrapped at the ``DeltaStorageHandler`` layer in
        ``open_dataset``'s delta branch (snapshot-version-scoped, the onelake
        pattern) — that rebuild is feasible on pyarrow 25 because the handler
        never touches the AzureFileSystem object.
        """
        from .blockcache import maybe_block_cache

        return maybe_block_cache(self._adlsfs(), purpose=f"adls:{info.path}")

    def _delta_data_fs(self, dt, info: TableInfo, version: int | None):
        """The block-cache-wrapped filesystem for Delta data-file reads, or None.

        Mirrors onelake.py's ``_delta_data_fs`` exactly: when the disk block
        cache is enabled, rebuild the SAME handler delta-rs would build
        internally (``DeltaStorageHandler`` over the table URI with this
        provider's storage options, seeded with the snapshot's file sizes from
        the delta log), wrap it in the cache, and hand it back so parquet
        footers/column chunks are fetched once and re-served from pod-local
        disk. Verified feasible on pyarrow 25 / deltalake 1.6.3: the handler
        is a pure ``PyFileSystem`` over the table URI and storage options —
        it never touches the AzureFileSystem object, so the wrap composes
        cleanly on this backend (nothing to "not wrap cleanly"). Returns None
        — leaving delta-rs's built-in path in place — when the cache is
        disabled or anything fails: the cache is an accelerator, never a
        dependency. Snapshot consistency (delta-v<N> scope) matches onelake.py:
        a re-open at a different snapshot — a new ETL commit or a time-travel
        read — can never be served another snapshot's cached bytes.
        """
        from .blockcache import block_cache_enabled, maybe_block_cache

        if not block_cache_enabled():
            return None
        try:
            import pyarrow.fs as pafs_mod
            from deltalake.fs import DeltaStorageHandler

            handler = DeltaStorageHandler(
                self.table_uri(info),
                options=self._delta_storage_options(),
                known_sizes=self._delta_file_sizes(dt),
            )
            snapshot = int(version) if version is not None else int(dt.version())
            return maybe_block_cache(
                pafs_mod.PyFileSystem(handler),
                purpose=f"adls:{info.path}",
                scope=f"delta-v{snapshot}",
            )
        except Exception:
            logger.warning("ADLS block-cache wrap failed; reading uncached", exc_info=True)
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
