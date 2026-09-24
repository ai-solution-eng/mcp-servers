"""Opt-in, disk-backed BLOCK CACHE for the pyarrow filesystems.

Remote object-store reads (S3/MinIO, ABFS/OneLake, GCS-interop) dominate
repeated-scan latency: agent sessions re-read the same columns constantly,
and every uncached query pays the network + parquet decode again. This
module wraps any pyarrow ``FileSystem`` in a read-through block cache
served from pod-local disk: parquet footers and column chunks are fetched
once into ``cache_dir`` and every later read of the same bytes is local.

It is implemented as a ``pa.fs.PyFileSystem`` handler (a thin Python
``FileSystemHandler`` delegating to the real filesystem), so it needs NO
new dependencies and works uniformly across every backend that builds
datasets through a pyarrow filesystem. Delta-rs datasets are wrapped too:
``OneLakeProvider.open_dataset`` rebuilds the exact ``DeltaStorageHandler``
``DeltaTable.to_pyarrow_dataset()`` would build internally and hands the
cache-wrapped filesystem to delta-rs, so repeated Delta scans hit the cache
instead of re-reading object-store bytes (the NFS/`file` backend's delta
tables keep delta-rs's internal reader — local disk is the page cache's job).

Opt-in by design: ``SQLHANDLER_BLOCK_CACHE=1``. Off by default because a
cold BIG sequential scan pays a small Python-layer cost per block, while
the win is on repeated / filtered / preview-style reads — the operator
decides which trade their site wants.

Correctness notes:

* Cache freshness is keyed by (path, file size): an in-place file rewrite
  produces a different key, so stale blocks are never served.
* Callers can scope keys further with ``scope`` (the OneLake delta path
  scopes by Delta snapshot version — mirroring the result cache's
  base-snapshot keying — so a re-open at a different snapshot, ETL commit
  or time travel, can never serve another snapshot's cached bytes even if
  a backend rewrote a path in place).
* Blocks publish with temp-file + atomic rename — concurrent readers
  (DuckDB threads) never observe partial blocks.
* Every failure in this layer degrades to the plain filesystem: the cache
  is an accelerator, never a dependency.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
from pathlib import Path

import pyarrow as pa
import pyarrow.fs as pafs

logger = logging.getLogger("sqlhandler.blockcache")


def _cfg() -> dict:
    """Block-cache configuration from the environment (cheap, per call)."""

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return max(int(raw), 0) if raw else default
        except ValueError:
            return default

    return {
        "enabled": os.environ.get("SQLHANDLER_BLOCK_CACHE", "").strip().lower() in ("1", "true", "yes", "on"),
        "include_local": os.environ.get("SQLHANDLER_BLOCK_CACHE_INCLUDE_LOCAL", "").strip().lower()
        in ("1", "true", "yes", "on"),
        "dir": os.environ.get("SQLHANDLER_BLOCK_CACHE_DIR", "").strip()
        or str(Path(tempfile.gettempdir()) / "sqlhandler-block-cache"),
        "block_size": _int("SQLHANDLER_BLOCK_CACHE_BLOCK_SIZE", 8 * 1024 * 1024),
        "max_bytes": _int("SQLHANDLER_BLOCK_CACHE_MAX_BYTES", 4 * 1024**3),
    }


def block_cache_enabled() -> bool:
    """Whether the disk block cache is opted in (``SQLHANDLER_BLOCK_CACHE``).

    Lets providers decide BETWEEN build paths — e.g. the OneLake delta
    reader keeps delta-rs's built-in IO when the cache is off (byte-identical
    to the unwrapped behavior) and only wires the cache in when it will
    actually cache.
    """
    return _cfg()["enabled"]


class _BlockCacheHandler(pafs.FileSystemHandler):
    """A read-only ``FileSystemHandler`` adding a disk block cache."""

    def __init__(self, base: pafs.FileSystem, cfg: dict, scope: str = "", sizes: dict[str, int] | None = None):
        self._base = base
        self._cfg = cfg
        self._scope = scope or ""
        # Known file sizes from the caller (e.g. the delta log's add-action
        # stats): skips one stat round-trip per file per open and lets the
        # cache engage even when the base filesystem cannot stat a path.
        self._sizes = dict(sizes) if sizes else None
        self._lock = threading.Lock()
        self._bytes_written = 0

    # -- identity -----------------------------------------------------------

    def __eq__(self, other) -> bool:  # pyarrow caches datasets by fs identity
        return (
            isinstance(other, _BlockCacheHandler)
            and self._base == other._base
            and self._cfg["dir"] == other._cfg["dir"]
            and self._scope == other._scope
        )

    def __hash__(self) -> int:  # keep the wrapper usable as a dict key
        # NEVER str(self._base): pyarrow's reprs embed the inner filesystem
        # wrapper's OBJECT ADDRESS (``...LocalFileSystem object at 0x...``),
        # and a SubTreeFileSystem does not keep that wrapper alive — after a
        # GC re-materializes it, the same handler's str() prints a different
        # address, so a str-based hash changes under a LIVE object (violating
        # the eq/hash contract: dataset-cache identity lookups randomly miss,
        # defeating the cache — caught live as a flaky test assertion).
        # type_name + (for subtree fs) base_path is the stable value identity
        # that __eq__'s ``self._base == other._base`` compares equal on.
        base_path = getattr(self._base, "base_path", "")
        return hash((type(self).__name__, self._base.type_name, base_path, self._cfg["dir"], self._scope))

    # -- the two hot paths ----------------------------------------------------

    def open_input_file(self, path: str):
        return pa.PythonFile(self._cached_file(path), mode="rb")

    def open_input_stream(self, path: str):
        return pa.PythonFile(self._cached_file(path), mode="rb")

    def _cached_file(self, path: str) -> _RawStream | _CachedStream:
        path = str(path)
        npath = self.normalize_path(path)
        size = self._sizes.get(npath) if self._sizes else None
        if size is None:
            info = self._base.get_file_info(npath)
            # FileInfo.size is -1 (or None, on some handler-backed fs) when
            # the path cannot be statted.
            size = info.size if (info and info.size is not None) else -1
        if size < 0:
            # directories / unstatable paths: serve the raw stream unwrapped
            return _RawStream(self._open_sequential(npath))
        return _CachedStream(
            lambda: self._open_random(npath),  # opened lazily: a fully-warm
            path=npath,  # read never touches the base
            size=size,
            cfg=self._cfg,
            on_bytes=self._account,
            scope=self._scope,
        )

    def _open_random(self, path: str):
        """A seekable base stream. open_input_file is the random-access open;
        some wrappers (SubTreeFileSystem, notably) return a NON-seekable
        stream from open_input_stream — footers and block reads need seeks."""
        try:
            return self._base.open_input_file(path)
        except NotImplementedError:
            return self._base.open_input_stream(path)

    def _open_sequential(self, path: str):
        try:
            return self._base.open_input_file(path)
        except NotImplementedError:
            return self._base.open_input_stream(path)

    def _account(self, n: int) -> None:
        """Track bytes entering the cache; nuke-and-restart past the cap.

        Blocks are disposable, so eviction is a whole-directory reset —
        simple, bounded, and correct (the next reader refills what it needs).
        """
        with self._lock:
            self._bytes_written += n
            if self._cfg["max_bytes"] > 0 and self._bytes_written > self._cfg["max_bytes"]:
                cache_dir = self._cfg["dir"]
                logger.info("block cache exceeded %d bytes; resetting %s", self._cfg["max_bytes"], cache_dir)
                try:
                    import shutil

                    shutil.rmtree(cache_dir, ignore_errors=True)
                except Exception:
                    logger.warning("block cache reset failed; continuing uncached", exc_info=True)
                self._bytes_written = 0

    # -- metadata -------------------------------------------------------------

    def get_file_info(self, paths):
        """pyarrow's handler contract: a list of paths in, same-length
        FileInfo list out (single-path callers are tolerated too)."""
        if isinstance(paths, (list, tuple)):
            return self._base.get_file_info([str(p) for p in paths])
        return [self._base.get_file_info(str(paths))]

    def get_file_info_selector(self, selector: pafs.FileSelector):
        # pyarrow >= 21 folded the selector-aware listing into
        # ``get_file_info(paths_or_selector)`` and REMOVED the dedicated
        # ``get_file_info_selector`` — calling it raises AttributeError
        # inside the C++ discovery callback and every cached dataset open
        # fails (misreported downstream as "table does not exist"). Both
        # shapes return a list of FileInfo; support either generation.
        if hasattr(self._base, "get_file_info_selector"):
            return self._base.get_file_info_selector(selector)
        return self._base.get_file_info(selector)

    def normalize_path(self, path: str) -> str:
        return self._base.normalize_path(str(path))

    # -- everything else: read-only posture, delegate or refuse ---------------

    def get_type_name(self) -> str:
        return f"blockcache({self._base.type_name})"

    def _read_only(self, *args, **kwargs):
        raise NotImplementedError("the block-cache filesystem is read-only")

    create_dir = _read_only
    delete_dir = _read_only
    delete_dir_contents = _read_only
    delete_root_dir_contents = _read_only
    delete_file = _read_only
    move = _read_only
    copy_file = _read_only
    open_output_stream = _read_only
    open_append_stream = _read_only


class _RawStream:
    """Pass-through for paths we do not cache (directories, unknown sizes)."""

    def __init__(self, stream):
        self._s = stream

    def read(self, n=-1):
        return self._s.read(n)

    def seek(self, offset, whence=os.SEEK_SET):
        return self._s.seek(offset, whence)

    def tell(self):
        return self._s.tell()

    def readable(self):
        return True

    def seekable(self):
        return True

    def close(self):
        self._s.close()

    @property
    def closed(self):
        return self._s.closed

    def size(self):
        return self._s.size()


class _CachedStream:
    """A seekable file-like over the base stream, backed by disk blocks.

    Block ``i`` covers ``[i*block_size, (i+1)*block_size)`` of the file and
    is cached at ``<dir>/<sha(path)>-<size>/block-<i>``. Reads assemble
    whole blocks; the base stream stays sequentially positioned so a cold
    read of N contiguous blocks is N sequential reads (no re-seeks).
    """

    def __init__(self, opener, path: str, size: int, cfg: dict, on_bytes, scope: str = ""):
        self._opener = opener  # base stream is opened LAZILY on first miss —
        self._s = None  # a fully-warm read never touches the network
        self._path = path
        self._size = size
        self._cfg = cfg
        self._on_bytes = on_bytes
        self._pos = 0
        self._closed = False
        # Optional snapshot scope: keys are scoped (never aliased) across
        # scopes, while the empty scope keeps the historical derivation so
        # existing cache directories stay warm across upgrades.
        if scope:
            key = hashlib.sha256(f"{scope}\x00{path}".encode()).hexdigest()[:24]
        else:
            key = hashlib.sha256(path.encode()).hexdigest()[:24]
        self._dir = Path(cfg["dir"]) / f"{key}-{size}"
        self._bs = cfg["block_size"]

    def _base_stream(self):
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if self._s is None:
            self._s = self._opener()
        return self._s

    # -- block plumbing -------------------------------------------------------

    def _block_path(self, i: int) -> Path:
        return self._dir / f"block-{i}"

    def _fetch_block(self, i: int) -> bytes:
        """Read block ``i`` from the base stream (cache-aside, atomic publish)."""
        bp = self._block_path(i)
        if bp.exists():  # warm: another reader (or an earlier stream) fetched it
            try:
                return bp.read_bytes()
            except OSError:
                pass  # raced a cache reset: fall through and refetch
        start = i * self._bs
        length = min(self._bs, self._size - start)
        stream = self._base_stream()
        stream.seek(start)
        data = stream.read(length)
        self._on_bytes(len(data))
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._dir), prefix=f"block-{i}.", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, bp)
        except Exception:
            logger.debug(
                "block %s/%d not cached (disk issue?); serving from stream",
                self._path,
                i,
                exc_info=True,
            )
        return data

    # -- the file-like contract pyarrow needs ---------------------------------

    def read(self, n: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if n is None or n < 0:
            n = max(self._size - self._pos, 0)
        n = min(n, max(self._size - self._pos, 0))
        if n == 0:
            return b""
        out = bytearray()
        while len(out) < n:
            block_no, off = divmod(self._pos, self._bs)
            data = self._fetch_block(block_no)
            chunk = data[off : off + (n - len(out))]
            out += chunk
            self._pos += len(chunk)
            if len(chunk) == 0:  # defensive: no forward progress
                break
        return bytes(out)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            self._pos = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence!r}")
        self._pos = max(self._pos, 0)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return True

    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self._closed = True
        if self._s is not None:
            try:
                self._s.close()
            except Exception:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def maybe_block_cache(
    fs: pafs.FileSystem,
    purpose: str = "",
    scope: str = "",
    sizes: dict[str, int] | None = None,
) -> pafs.FileSystem:
    """Wrap ``fs`` in the disk block cache when enabled; never fails.

    Disabled by default (``SQLHANDLER_BLOCK_CACHE=1`` opts in). Pure-local
    filesystems are skipped by default — the OS page cache already does this
    job for local disk — but NFS mounts are ALSO LocalFileSystem to pyarrow
    while being network: ``SQLHANDLER_BLOCK_CACHE_INCLUDE_LOCAL=1`` opts
    them in. Any error constructing the wrapper logs a warning and returns
    the original filesystem, so the data path can never break from here.

    ``scope`` namespaces the cache keys (e.g. a Delta snapshot version, so
    two snapshots of the same table never share blocks); ``sizes`` supplies
    known file sizes (e.g. from the delta log) so cacheable paths skip a
    stat round-trip and unknown-size paths are the only ones left to the
    stat fallback.
    """
    cfg = _cfg()
    if not cfg["enabled"]:
        return fs
    if isinstance(fs, pafs.LocalFileSystem) and not cfg["include_local"]:
        return fs
    try:
        wrapped = pafs.PyFileSystem(_BlockCacheHandler(fs, cfg, scope=scope, sizes=sizes))
        logger.debug("block cache enabled for %s", purpose or type(fs).__name__)
        return wrapped
    except Exception:
        logger.warning(
            "could not enable block cache for %s; using the plain filesystem",
            purpose,
            exc_info=True,
        )
        return fs
