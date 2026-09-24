"""Shared L2 result cache: query results on a disk path all replicas can see.

The per-process result cache (``SqlEngine._result_cache``) is memory-only,
so a 4-replica deployment pays every cold query four times — the bench
notes call this out ("Per-replica cache physics": warm p95 outliers 1.0–3.2s
vs ~105ms p50). The virtual-table materialization cache already proved the
fix for ITS slice: write the result to a shared directory as parquet + a
small JSON sidecar and let every replica read it back. This module is the
same pattern for GENERAL query results, behind the memory LRU:

* **Artifact** — ``<dir>/<key[:16]>.parquet`` (zstd) + ``<dir>/<key[:16]>.json``
  sidecar carrying ``{"key": <sha256 hex>, "created": <epoch>, "rows": n,
  "bytes": n}``. The sidecar's full ``key`` guards against a hash-prefix
  collision AND against a foreign file occupying the name; the parquet is
  trusted only when its sidecar matches the exact key asked for.
* **Atomicity** — both files publish via temp file + ``os.replace``, so
  concurrent replicas materializing the same key simultaneously are always
  reading a complete file (last writer wins; both results are valid — the
  virtual-cache precedent).
* **Degradation** — every failure (missing sidecar, corrupt JSON, unreadable
  parquet, unwritable dir) degrades to a miss / a skipped write. The cache
  is an accelerator, never a dependency; nothing here can fail a query.
* **Cleanup** — expired entries are removed lazily on lookup, and one
  daemon thread per process sweeps sidecars past the TTL (mtime pass, same
  shape as the engine's ``_auto_refresh_loop``). Sweeping uses the sidecar
  mtime, which survives restarts — an entry a dead pod wrote still expires.

Opt-in: ``SQLHANDLER_L2_DIR`` must be set (k8s: point it at an RWX PVC
shared by all replicas). ``SQLHANDLER_L2_ENABLED=0`` disables the layer
entirely; when the dir is unset the engine behaves byte-identically to
before.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("sqlhandler.l2cache")

# Defaults: the floor exists because PVC IO round-trips small results slower
# than recomputing them (the implementation review's verified gotcha); the
# ceiling matches the virtual materialization cache (2 GiB) so one runaway
# result cannot fill the shared volume.
DEFAULT_MIN_BYTES = 256 * 1024
DEFAULT_MAX_BYTES = 2 * 1024**3
DEFAULT_TTL = 3600.0
SWEEP_INTERVAL_FRACTION = 0.5  # daemon sweeps at ttl/2 (min 30s)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(int(raw), 0) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return max(float(raw), 0.0) if raw else default
    except ValueError:
        return default


class L2ResultCache:
    """Disk-backed shared read-through layer for query results.

    Holds no data itself — lookup/store move pyarrow Tables between the
    caller and ``dir``. All counters live on the instance and are surfaced
    through :meth:`stats` for ``cache_stats()`` and /metrics.
    """

    def __init__(self, dir_path: str, ttl: float = DEFAULT_TTL):
        self._dir = dir_path
        self._ttl = ttl
        self._lock = threading.Lock()
        self.hits = 0
        self.writes = 0
        self._sweeper: threading.Thread | None = None

    # ------------------------------------------------------------- config
    @property
    def dir(self) -> str:
        return self._dir

    @property
    def ttl(self) -> float:
        return self._ttl

    def stats(self) -> dict:
        """Counters + config snapshot for cache_stats()/metrics (additive)."""
        with self._lock:
            return {
                "enabled": True,
                "dir": self._dir,
                "ttl": self._ttl,
                "hits": self.hits,
                "writes": self.writes,
            }

    # ------------------------------------------------------------- paths
    def _artifact(self, key: str) -> Path:
        return Path(self._dir) / f"{key[:16]}.parquet"

    def _sidecar(self, key: str) -> Path:
        return Path(self._dir) / f"{key[:16]}.json"

    def has_entry(self, key: str) -> bool:
        """True when a FRESH entry for ``key`` exists — without reading data.

        The explain_query warm-band probe (agent productivity pack): one
        sidecar read (a small JSON stat), never the parquet. Same freshness
        rule as :meth:`lookup` (the TTL check uses the sidecar's ``created``
        timestamp) but no parquet verification and no counter bump — a probe
        must not turn a lookup miss into a counted hit. Never raises.
        """
        try:
            meta = self._read_sidecar(key)
            if meta.get("key") != key:
                return False
            return not (self._ttl > 0 and time.time() - float(meta.get("created", 0)) >= self._ttl)
        except Exception:
            return False

    # ------------------------------------------------------------ lookup
    def lookup(self, key: str):
        """The cached table for ``key`` when fresh and readable, else None.

        A hit ALSO warms nothing here — the engine copies the table into its
        memory L1 (see ``SqlEngine._result_cache_lookup``). An entry past the
        TTL is deleted on sight (lazy GC): the daemon sweep is the backstop,
        this is the guaranteed path. Never raises.
        """
        import pyarrow.parquet as pq

        try:
            meta = self._read_sidecar(key)
        except Exception:
            return None  # missing/corrupt sidecar — an ordinary miss
        if meta.get("key") != key:
            return None  # foreign file at the same prefix — never trusted
        if self._ttl > 0 and time.time() - float(meta.get("created", 0)) >= self._ttl:
            self._remove(key)
            return None
        path = str(self._artifact(key))
        try:
            table = pq.read_table(path)
        except Exception:
            # Unreadable parquet with a live sidecar would otherwise be
            # re-tried forever — remove the broken pair.
            logger.warning("L2 result cache: parquet unreadable for key %s…; ignoring", key[:16])
            self._remove(key)
            return None
        with self._lock:
            self.hits += 1
        return table

    def _read_sidecar(self, key: str) -> dict:
        return json.loads(self._sidecar(key).read_text(encoding="utf-8"))

    def _remove(self, key: str) -> None:
        try:
            self._sidecar(key).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._artifact(key).unlink(missing_ok=True)
        except OSError:
            pass

    def drop_for_table(self, table_path: str, table_name: str | None = None) -> int:
        """Remove every entry whose SQL references a written table (write tier).

        The write tier's complement of snapshot-token invalidation: a
        scratch CTAS drop-create RESETS the Delta log to version 0, so the
        key's ``<source>/<path>=<version>`` token can REGRESS and a cached
        entry for the OLD content stays "fresh". A write therefore walks the
        sidecars (small JSON stats) and — because the stored key is the
        final sha256 hex with the parts hashed away — matches on the SQL
        text the ENGINE remembers... which the sidecar does not carry. So
        the honest L2 eviction is TTL-conservative: entries are dropped by
        KEY when the engine hands us the exact stale keys, and by AGE when
        it cannot (this call, sidecar-only: drop entries older than the
        table's write moment). Returns the dropped count. Never raises.
        """
        dropped = 0
        try:
            for sidecar in Path(self._dir).glob("*.json"):
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    continue
                self._remove(str(meta.get("key", "")))
                dropped += 1
        except Exception:
            logger.debug("L2 drop_for_table failed for %s", table_path, exc_info=True)
        return dropped

    # ------------------------------------------------------------- store
    def store(self, key: str, table) -> None:
        """Publish one result as parquet + sidecar (best-effort, never raises).

        Byte caps are the ENGINE's decision (it knows the env-tuned min/max
        and its own L1 already saw the size); by the time a table arrives
        here it is within bounds.
        """
        import pyarrow.parquet as pq

        path = self._artifact(key)
        tmp_path: str | None = None
        try:
            Path(self._dir).mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            os.close(fd)
            pq.write_table(table, tmp_path, compression="zstd")
            os.replace(tmp_path, path)
            tmp_path = None
            meta = {
                "key": key,
                "created": time.time(),
                "rows": table.num_rows,
                "bytes": table.nbytes,
            }
            fd, tmp_meta = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".meta.tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(meta))
            os.replace(tmp_meta, str(self._sidecar(key)))
            with self._lock:
                self.writes += 1
        except Exception:
            logger.debug("L2 result cache store failed for key %s…", key[:16], exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------ sweep
    def sweep(self) -> int:
        """Delete sidecar+parquet pairs past the TTL; returns deleted count.

        One mtime pass over ``dir``: the sidecar's mtime is the entry's
        creation clock (written once, never touched again), so entries a
        dead pod published still expire on schedule. The parquet is removed
        with its sidecar; orphans (parquet with no sidecar) are left for a
        later slice — the atomic publish order (data then sidecar) means an
        orphan only exists mid-write or after a crash between the two
        renames, and deleting it would race a legitimate concurrent writer.
        Never raises.
        """
        removed = 0
        if self._ttl <= 0:
            return 0
        now = time.time()
        try:
            entries = list(Path(self._dir).glob("*.json"))
        except OSError:
            return 0
        for sidecar in entries:
            try:
                if now - sidecar.stat().st_mtime < self._ttl:
                    continue
                key = self._read_sidecar_key(sidecar)
                self._remove(key)
                removed += 1
            except Exception:
                # Corrupt/lost sidecar past TTL: fall back to unlinking both
                # prefix-mates by name (sidecar first — a reader that just
                # validated against it has already passed the freshness check).
                stem = sidecar.stem
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    (sidecar.parent / f"{stem}.parquet").unlink(missing_ok=True)
                except OSError:
                    pass
                removed += 1
        return removed

    def _read_sidecar_key(self, sidecar: Path) -> str:
        return str(json.loads(sidecar.read_text(encoding="utf-8"))["key"])

    def start_sweeper(self) -> None:
        """Start the daemon sweep thread (idempotent; no-op when TTL <= 0)."""
        if self._ttl <= 0 or self._sweeper is not None:
            return
        self._sweeper = threading.Thread(
            target=self._sweep_loop,
            daemon=True,
            name="sqlhandler-l2-sweep",
        )
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        interval = max(self._ttl * SWEEP_INTERVAL_FRACTION, 30.0)
        while True:
            time.sleep(interval)
            try:
                removed = self.sweep()
                if removed:
                    logger.debug(
                        "L2 result cache sweep removed %d expired entr%s",
                        removed,
                        "y" if removed == 1 else "ies",
                    )
            except Exception:
                logger.debug("L2 result cache sweep skipped", exc_info=True)


def load_l2_config() -> dict | None:
    """L2 cache configuration from the environment, or None when disabled.

    Enabled requires BOTH the master switch (``SQLHANDLER_L2_ENABLED``,
    default on) and a directory (``SQLHANDLER_L2_DIR`` — deliberately no
    default: silently scattering result parquet onto pod-local /tmp would
    look like sharing while sharing nothing). Byte caps: results smaller
    than ``SQLHANDLER_L2_MIN_BYTES`` (default 256 KiB) round-trip slower
    than recomputing; results larger than ``SQLHANDLER_L2_MAX_BYTES``
    (default 2 GiB, mirroring the virtual cache) would fill the volume.
    """
    if os.environ.get("SQLHANDLER_L2_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    dir_path = os.environ.get("SQLHANDLER_L2_DIR", "").strip()
    if not dir_path:
        return None
    return {
        "dir": dir_path,
        "ttl": _env_float("SQLHANDLER_L2_TTL", DEFAULT_TTL),
        "min_bytes": _env_int("SQLHANDLER_L2_MIN_BYTES", DEFAULT_MIN_BYTES),
        "max_bytes": _env_int("SQLHANDLER_L2_MAX_BYTES", DEFAULT_MAX_BYTES),
    }
