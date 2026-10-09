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
  "bytes": n, "tables": [...]}``. The sidecar's full ``key`` guards against a
  hash-prefix collision AND against a foreign file occupying the name; the
  parquet is trusted only when its sidecar matches the exact key asked for.
  ``tables`` — written when the caller passes the referenced table
  identifiers to :meth:`store` — is what the write-tier eviction
  (:meth:`drop_for_table`) matches on; sidecars without it are "unknown"
  and get the conservative legacy treatment on a drop.
* **Atomicity** — both files publish via temp file + ``os.replace``, so
  concurrent replicas materializing the same key simultaneously are always
  reading a complete file (last writer wins; both results are valid — the
  virtual-cache precedent).
* **Degradation** — every failure (missing sidecar, corrupt JSON, unreadable
  parquet, unwritable dir) degrades to a miss / a skipped write. The cache
  is an accelerator, never a dependency; nothing here can fail a query.
* **Async write-out** (HA review 2026-09, the sglang HiCache write-through
  analogy): the computing replica hands the result to a bounded queue and a
  single daemon worker serializes zstd parquet + sidecar OFF the query path
  (the sglang L3 write-out, applied to result caching). The query returns
  when its L1 (memory) copy is placed; other replicas pick the result up
  from disk at a small non-RAM read cost once the worker publishes it.
  Fallback to the historical synchronous publish when disabled
  (SQLHANDLER_L2_WRITE_ASYNC=0) or when the queue is full (backpressure:
  drop the write rather than grow the queue — a dropped write is an
  ordinary cache miss, a grown queue is replica memory).
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
import queue
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .fastrender import dumps as _fast_json_dumps

logger = logging.getLogger("sqlhandler.l2cache")

# Defaults: the floor exists because PVC IO round-trips small results slower
# than recomputing them (the implementation review's verified gotcha); the
# ceiling matches the virtual materialization cache (2 GiB) so one runaway
# result cannot fill the shared volume.
DEFAULT_MIN_BYTES = 256 * 1024
DEFAULT_MAX_BYTES = 2 * 1024**3
DEFAULT_TTL = 3600.0
SWEEP_INTERVAL_FRACTION = 0.5  # daemon sweeps at ttl/2 (min 30s)

# Async write-out: bounded pending-store queue (ENTRIES, not bytes — every
# queued entry is already capped by SQLHANDLER_L2_MAX_BYTES, so a full queue
# of the largest allowed results is ~40GiB of pending parquet worst-case;
# a full queue DROPS the write — an ordinary cache miss — instead of growing).
DEFAULT_ASYNC_QUEUE_DEPTH = 32


def write_async_enabled() -> bool:
    """Async L2 write-out on/off (SQLHANDLER_L2_WRITE_ASYNC, default on)."""
    return os.environ.get("SQLHANDLER_L2_WRITE_ASYNC", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


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


def _normalize_table_ident(ident: str) -> str:
    """Normalized form of a table identifier for drop-time matching.

    Strips any ``scheme://host`` prefix (so a sidecar's recorded path form
    and a drop's canonical filesystem form meet), strips surrounding and
    trailing slashes, and casefolds — names in this repo's providers are
    compared case-insensitively. Empty input normalizes to "" (filtered by
    the caller).
    """
    text = str(ident).strip()
    if "://" in text:
        # urlsplit separates netloc from path: abfs://fs/shop/sales -> shop/sales
        parsed = urlsplit(text)
        text = parsed.path
    return text.strip("/").rstrip("/").casefold()


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
        # (key, table, referenced-table ids); ids None = the historical
        # no-tables call shape (see enqueue_write / _write_loop).
        self._write_queue: queue.Queue[tuple[str, object, list[str] | None]] | None = None
        self._write_worker: threading.Thread | None = None
        self._write_dropped = 0  # backpressure drops (an ordinary miss each)

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
            payload = {
                "enabled": True,
                "dir": self._dir,
                "ttl": self._ttl,
                "hits": self.hits,
                "writes": self.writes,
            }
            if self._write_queue is not None:
                payload["write_mode"] = "async"
                payload["write_queue_depth"] = self._write_queue.qsize()
                payload["write_dropped"] = self._write_dropped
            else:
                payload["write_mode"] = "sync"
            return payload

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
        entry for the OLD content stays "fresh". The stored key is the
        final sha256 hex with the parts hashed away, so path-matching
        stored keys is impossible; instead entries recorded their
        referenced-table identifiers at STORE time (the sidecar's
        ``tables`` list — the engine passes the ``_referenced_tables``
        forms through the store API), and THIS call matches the written
        table's identifiers against those lists.

        Matching is normalized: a sidecar ``tables`` entry or a drop
        identifier is compared case-insensitively after stripping any
        ``scheme://host`` prefix and surrounding slashes, so path- and
        name-form identifiers meet. Only entries whose ``tables`` list
        contains a dropped identifier are removed — entries for OTHER
        tables (and legacy sidecars with no ``tables`` key) SURVIVE a
        selective drop.

        Fallback (the conservative legacy behavior, kept for sidecars
        without ``tables``): when a drop matched nothing AND at least one
        unknown-tables entry exists, those unknown entries are wiped — an
        unknown entry may be the written table's, and serving stale
        results is worse than recomputing. Known non-matching entries
        survive even the fallback (their recorded tables provably exclude
        the dropped table — wiping them would re-create the over-eviction
        this method exists to fix). The fallback is logged (WARNING with
        the wiped count) and self-heals, since post-wipe entries carry
        ``tables`` again.

        Returns the dropped count. Never raises.
        """
        dropped = 0
        try:
            # Quiesce the async write-out FIRST (cross-review fix): an
            # entry still sitting in the queue is invisible to the on-disk
            # scan below, and the worker would publish it AFTER this drop —
            # resurrecting a pre-write result (stale rows until TTL). Drain
            # is bounded (worker keeps serving; queue-full stores fall back
            # to the synchronous path so no deadlock).
            try:
                self.flush_async_stores(timeout=5.0)
            except Exception:
                logger.debug("L2 drop_for_table flush_async_stores failed", exc_info=True)
            wanted = {
                _normalize_table_ident(table_path),
                _normalize_table_ident(table_name or ""),
            } - {""}
            sidecars = list(Path(self._dir).glob("*.json"))
            unknown: list[Path] = []
            victims: list[str] = []
            for sidecar in sidecars:
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    continue  # corrupt sidecar — the sweep/lazy paths own it
                tables = meta.get("tables")
                if not isinstance(tables, list):
                    unknown.append(sidecar)
                    continue
                sidecar_idents = {_normalize_table_ident(str(t)) for t in tables} - {""}
                if wanted & sidecar_idents:
                    key = str(meta.get("key", ""))
                    if key:
                        victims.append(key)
            if victims:
                for key in victims:
                    self._remove(key)
                    dropped += 1
            elif unknown:
                # Nothing matched selectively, but entries whose referenced
                # tables are unknown exist — one of them may be the written
                # table's. Wipe the UNKNOWN entries (conservative: serving a
                # stale result is worse than recomputing) — known
                # non-matching entries survive, they provably don't reference
                # the dropped table. Say so: a silent wipe of a shared cache
                # is exactly the failure this method used to be.
                logger.warning(
                    "L2 drop_for_table(%s): no sidecar recorded a matching table; "
                    "wiping %d unknown-tables (legacy) entries as the fallback",
                    table_path,
                    len(unknown),
                )
                for sidecar in unknown:
                    try:
                        meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    self._remove(str(meta.get("key", "")))
                    dropped += 1
        except Exception:
            logger.debug("L2 drop_for_table failed for %s", table_path, exc_info=True)
        return dropped

    # -------------------------------------------------- async write-out
    def async_store(self, key: str, table, tables: list[str] | None = None) -> bool:
        """Hand a result to the background publisher (the write-through).

        Returns True when queued, False when the write falls back to the
        synchronous path (async disabled or queue full — the caller then
        calls :meth:`store` itself, preserving the historical behavior).
        The reference is queued, not copied: the table is already paid for
        in the computing replica's L1, and L1 eviction dropping the last
        reference only means the worker loses a race it can lose anyway
        (a replica crash mid-publish) — the artifact write itself holds
        its own reference while serializing. ``tables`` (referenced-table
        identifiers for the write-tier eviction) rides in the queue item
        and reaches the sidecar via :meth:`store`.
        """
        if self._write_queue is None:
            return False
        try:
            self._write_queue.put_nowait((key, table, tables))
            return True
        except queue.Full:
            with self._lock:
                self._write_dropped += 1
            return False

    def _ensure_write_worker(self) -> None:
        """Start the single write-out worker (idempotent, daemon)."""
        if self._write_worker is not None and self._write_worker.is_alive():
            return
        self._write_queue = queue.Queue(maxsize=DEFAULT_ASYNC_QUEUE_DEPTH)
        self._write_worker = threading.Thread(target=self._write_loop, daemon=True, name="sqlhandler-l2-writeout")
        self._write_worker.start()

    def _write_loop(self) -> None:
        # The worker outlives the attribute's Optional lifetime: the queue is
        # created before this thread starts and never swapped back to None
        # (only the sweeper's shutdown clears workers, not queues) — the
        # assert states that invariant for the checker.
        write_queue = self._write_queue
        assert write_queue is not None
        while True:
            key, table, tables = write_queue.get()
            try:
                if tables is None:
                    self.store(key, table)  # historical call shape (no tables known)
                else:
                    self.store(key, table, tables=tables)
            except Exception:  # store() never raises, but never trust a loop to die
                logger.debug("L2 async write-out failed for key %s…", key[:16], exc_info=True)
            finally:
                write_queue.task_done()

    def flush_async_stores(self, timeout: float = 30.0) -> bool:
        """Block until every queued write-out is published (tests/shutdown).

        True when the queue drained within the timeout. A no-op (True) when
        async mode never started. (Polled rather than Queue.join(timeout=)
        — that parameter is 3.13+ and the engine supports 3.11.)
        """
        if self._write_queue is None:
            return True
        deadline = time.monotonic() + timeout
        while self._write_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._write_queue.unfinished_tasks

    # ------------------------------------------------------------- store
    def store(self, key: str, table, tables: list[str] | None = None) -> None:
        """Publish one result as parquet + sidecar (best-effort, never raises).

        Byte caps are the ENGINE's decision (it knows the env-tuned min/max
        and its own L1 already saw the size); by the time a table arrives
        here it is within bounds.

        ``tables`` — the referenced-table identifiers the engine computed
        for this result's SQL (``TableInfo.path`` / ``name`` forms) — is
        recorded in the sidecar when given so a write-tier
        :meth:`drop_for_table` can match entries SELECTIVELY; entries
        stored without it are "unknown" and get the conservative legacy
        treatment on a drop. When ``tables`` is None (e.g. the engine's
        L1-warm republish of an L2 hit, which has no SQL in hand) an
        existing sidecar's ``tables`` list is carried forward — same key
        means the same SQL identity (the key embeds it), so the carried
        list is exactly the entry's own. Old sidecars (pre-``tables``)
        read identically: the key is the default and unknown-tables
        entries are simply left alone by a selective drop.
        """
        import pyarrow.parquet as pq

        if tables is None:
            try:
                carried = self._read_sidecar(key).get("tables")
                if isinstance(carried, list) and carried:
                    tables = [str(t) for t in carried]
            except Exception:
                pass  # no readable existing sidecar — store without tables
        path = self._artifact(key)
        tmp_path: str | None = None
        try:
            Path(self._dir).mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
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
            if tables:
                # Dedup + sorted for stable sidecars; only truthy entries.
                meta["tables"] = sorted({str(t) for t in tables if t})
            fd, tmp_meta = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".meta.tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_fast_json_dumps(meta))
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


def metadata_shared_enabled() -> bool:
    """Share profile/column_stats outputs through the L2 dir (default on).

    `SQLHANDLER_L2_METADATA=0` opts out. Active only when the L2 layer
    itself is configured (a shared dir); a per-replica-only deployment
    (no dir) is unaffected either way.
    """
    return os.environ.get("SQLHANDLER_L2_METADATA", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


class L2JsonCache:
    """Shared read-through tier for small JSON results (profile, column_stats).

    The result cache's L2 moves Arrow tables (zstd parquet — the async
    write-out exists because those can be hundreds of MB). Profile and
    column_stats outputs are a few KB of JSON whose recompute is a
    multi-second sampled scan, so the floor-vs-recompute economics invert:
    sharing a small artifact beats recomputing it on every replica. Same
    discipline as the table tier, JSON-flavored:

    * artifacts live in a `meta/` SUBDIRECTORY of the L2 dir — a separate
      namespace so a metadata entry can never collide with a result
      sidecar, and the table-tier sweeper never sees them;
    * publish is a synchronous atomic temp+rename (sub-millisecond for a
      few KB — no queue, no worker);
    * TTL lives in the entry (`created`), checked lazily on get; a hit
      PAST the ttl deletes the file and misses;
    * every failure degrades to a miss / skipped write. Never raises.

    Fidelity: values serialize as their JSON text (a DATE min/max becomes
    its string form) — exactly how the MCP/UI layers render these outputs
    anyway, so consumers see identical text; direct python equality on a
    round-tripped dict holds for JSON-native scalars.
    """

    def __init__(self, dir_path: str, ttl: float):
        self._dir = Path(dir_path) / "meta"
        self._ttl = ttl
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @property
    def dir(self) -> str:
        return str(self._dir)

    @property
    def ttl(self) -> float:
        return self._ttl

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "dir": str(self._dir),
                "ttl": self._ttl,
                "hits": self.hits,
                "misses": self.misses,
                "writes": self.writes,
            }

    def _path(self, key: str) -> Path:
        return self._dir / f"{key[:16]}.json"

    def get(self, key: str) -> dict | None:
        """The cached dict for `key` when fresh and readable, else None."""
        result = self._get(key)
        with self._lock:
            if result is None:
                self.misses += 1
        return result

    def _get(self, key: str) -> dict | None:
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except Exception:
            return None
        if entry.get("key") != key:  # prefix-collision guard, same as the table tier
            return None
        if self._ttl > 0 and time.time() - float(entry.get("created", 0)) >= self._ttl:
            self.remove(key)
            return None
        with self._lock:
            self.hits += 1
        payload = entry.get("payload")
        return payload if isinstance(payload, dict) else None

    def put(self, key: str, payload: dict, tables: list[str] | None = None) -> None:
        """Publish one dict (best-effort, never raises).

        ``tables`` — referenced-table identifiers recorded in the sidecar so
        the write tier's ``drop_for_table`` can evict selectively (same
        contract as the table tier's store); the profile/column_stats tier
        shares the CTAS-version-regression hole the result tier fixed.
        """
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # no shared dir — the tier is inert, never a failure
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), prefix=f"{key[:16]}.", suffix=".tmp")
        try:
            entry = {"key": key, "created": time.time(), "payload": payload}
            if tables:
                entry["tables"] = sorted({str(t) for t in tables})
            os.write(fd, _fast_json_dumps(entry, default=str).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path(key))
        with self._lock:
            self.writes += 1

    def drop_for_table(self, table_path: str, table_name: str | None = None) -> int:
        """Evict entries whose recorded ``tables`` include the written table.

        Mirrors the table tier's selective semantics (normalized matching;
        unknown/legacy sidecars are left alone — they age out via TTL, and
        serving a possibly-stale PROFILE is far less dangerous than serving
        stale QUERY RESULTS, so no conservative wipe here). Never raises;
        returns the dropped count.
        """
        dropped = 0
        try:
            wanted = {
                _normalize_table_ident(table_path),
                _normalize_table_ident(table_name or ""),
            } - {""}
            if not wanted:
                return 0
            for sidecar in list(self._dir.glob("*.json")):
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    continue
                tables = meta.get("tables")
                if not isinstance(tables, list):
                    continue
                if any(_normalize_table_ident(str(t)) in wanted for t in tables):
                    self.remove(str(meta.get("key", "")))
                    dropped += 1
        except Exception:
            logger.debug("L2 meta drop_for_table failed for %s", table_path, exc_info=True)
        return dropped

    def remove(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass


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
