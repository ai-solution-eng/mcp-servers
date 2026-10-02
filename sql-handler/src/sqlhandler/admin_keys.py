"""Admin keys store + admin designation (Lead-pinned interface, 2026-09-30).

The frontend-admin feature's seam between the three implementation owners:

* ``identity-keys`` owns this module's BODIES (the JSON keys file on the
  RWX store, mtime-cached like the policy store) and the ``admins`` field
  in policy.py.
* ``admin-api`` owns the server.py routes + MCP twins that CALL this
  module — imports only the functions below, never the file format.
* The store COMPLEMENTS, never replaces, the bootstrap Secret keys
  (``SQLHANDLER_API_KEYS`` / ``MCP_API_KEYS``): the middleware matches
  against the UNION. Secret keys are reported with ``source: "secret"``
  and are NOT removable through this store (their lifecycle is the
  Secret's — kubectl; the frontend shows them read-only).

Fail-closed defaults until the owners land: no keys file configured →
empty store; ``is_admin`` → False for everyone (bootstrap is a one-time
``admins:`` entry in the policy document, documented in DEPLOYMENT.md).

Pinned shapes (do not change — other files compile against them):

* ``KeyEntry``: {"fp": "sha256:<12hex>", "key_sha256": <full sha256 hex
  of the raw key — REQUIRED, never the raw key itself>, "label": str,
  "created_at": ISO-8601 str, "created_by": str (minting admin's subject
  or fp), "source": "file"}
* keys file: {"keys": [KeyEntry...]} — fp-unique; duplicate mint →
  :class:`AdminKeysError`
* atomic writes ONLY: temp file in the same directory + ``os.replace``
  (a crash never truncates the store).

``key_sha256`` exists because fingerprints are one-way: the middleware
must authenticate a PRESENTED raw key against the store, and it can only
do that by hashing the presentation and comparing (constant-time) against
this full digest. The raw key is STILL never stored, logged, or returned
by anything except the one-time mint response.

Designation is policy-side: ``is_admin`` reads the CURRENT policy via
``policy_store().get()`` — the ``admins`` list of subjects and/or key
fingerprints (hot-reloaded with the policy file; both fp spellings
accepted by the loader, normalized to the bare fp).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone

logger = logging.getLogger("sqlhandler.admin_keys")

__all__ = [
    "ADMIN_KEYS_FILE_ENV",
    "AdminKeysError",
    "KeyEntry",
    "add_key",
    "is_admin",
    "keys_file_path",
    "list_keys",
    "match_presentation",
    "remove_key",
]

#: The keys-file path (empty/unset = store disabled — Secret keys only).
ADMIN_KEYS_FILE_ENV = "SQLHANDLER_ADMIN_KEYS_FILE"

_KEYS_FILE_NAME = "admin-keys.json"


class AdminKeysError(Exception):
    """Raised on invalid store operations (dup fp, store unconfigured)."""


def keys_file_path(environ: dict[str, str] | None = None) -> str | None:
    """The configured keys-file path, or None when the store is disabled."""
    env = os.environ if environ is None else environ
    path = env.get(ADMIN_KEYS_FILE_ENV, "").strip()
    return path or None


# ---------------------------------------------------------------------------
# the on-disk store (JSON, fp-unique, mtime-cached, atomic writes)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
#: Serializes the read-modify-write compound ops (add/remove); _lock guards
#: only the cache swap. One reentrant family would deadlock; keep them separate.
_rmw_lock = threading.Lock()
_cache: list[dict] | None = None  # None = nothing loaded yet
_cache_path: str | None = None  # the path the cache was loaded from
_stat: tuple[int, int] | None = None  # (mtime_ns, size) of that path


def _stat_sig(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load(environ) -> list[dict]:
    """The current store contents (mtime-cached; NEVER raises on bad content).

    Fail-closed like the policy store: a vanished file keeps the last valid
    contents (a mount hiccup must not strand minted keys mid-flight); a
    present-but-broken file keeps the previous contents too and logs — an
    unreadable store must never look EMPTY (an empty store would surface
    as "no keys minted", inviting a silent re-mint over live grants).

    A CHANGED configured path is a store switch, not a vanish: the cache
    is keyed on the path, so repointing the env starts fresh (the old
    path's entries must never bleed into the new store).
    """
    global _cache, _cache_path, _stat
    path = keys_file_path(environ)
    if path is None:
        return []  # store disabled: fail-closed empty, nothing cached
    sig = _stat_sig(path)
    if sig is None:
        # The file vanished: keep the last valid contents (fail-closed) —
        # but only when it is the SAME store that vanished AND we have
        # actually loaded it before (never a DIFFERENT path's entries).
        # A file that never existed on a FRESH path is just an empty store
        # (quietly: this is the configured-but-not-yet-minted state).
        with _lock:
            known = _cache is not None and _cache_path == path and _stat is not None
            kept = [dict(e) if isinstance(e, dict) else e for e in _cache] if known else []
        if known:
            logger.warning(
                "admin keys file %s vanished after being loaded; keeping the previous contents (fail-closed)", path
            )
        else:
            logger.debug("admin keys file %s not present; store reads empty", path)
        return kept
    with _lock:
        if _cache is not None and _cache_path == path and _stat == sig:
            return [dict(e) if isinstance(e, dict) else e for e in _cache]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        with _lock:
            if _cache is not None and _cache_path == path:
                logger.warning("admin keys file %s unreadable (%s); keeping the previous contents", path, exc)
                return [dict(e) if isinstance(e, dict) else e for e in _cache]
        logger.error("admin keys file %s unreadable: %s — store reports EMPTY (fail-closed, loud)", path, exc)
        return []
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        keys = []
        logger.warning("admin keys file %s has no 'keys' list; treating as empty", path)
    with _lock:
        _cache = keys
        _cache_path = path
        _stat = sig
    return [dict(e) if isinstance(e, dict) else e for e in keys]


def _write(entries: list[dict], environ) -> None:
    """Atomically persist the store (temp file in the SAME directory +
    ``os.replace`` — a crash never truncates the store)."""
    path = keys_file_path(environ)
    if path is None:  # defensive; callers check first
        raise AdminKeysError("admin keys store not configured")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".admin-keys-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"keys": entries}, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise AdminKeysError(f"could not write the admin keys store {path}: {exc}") from exc
    # Our own write is the newest state — cache it under the new stat so a
    # same-second read cannot hit a stale mtime granularity window.
    global _cache, _cache_path, _stat
    with _lock:
        _cache = list(entries)
        _cache_path = path
        _stat = _stat_sig(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_keys(environ: dict[str, str] | None = None) -> list[dict]:
    """The minted keys (KeyEntry shapes), mtime-cached. [] when disabled."""
    return _load(environ)


def add_key(raw_key: str, *, label: str, created_by: str,
            environ: dict[str, str] | None = None) -> dict:
    """Persist a minted key's FINGERPRINT (never the raw key) + assignment
    metadata. Returns the KeyEntry. Raises AdminKeysError when the store
    is disabled, the fp already exists, or the file cannot be written."""
    path = keys_file_path(environ)
    if path is None:
        raise AdminKeysError("admin keys store not configured")
    if not raw_key or not isinstance(raw_key, str):
        raise AdminKeysError("refusing to store an empty key")
    from .mcp_fleet_common.audit import key_fingerprint

    fp = key_fingerprint(raw_key)
    # The whole read-modify-write is serialized: two concurrent mints must
    # both land (and a mint racing a revoke must not resurrect the revoked
    # entry), so the load-check-write runs under one lock.
    with _rmw_lock:
        entries = _load(environ)
        if any(e.get("fp") == fp for e in entries):
            raise AdminKeysError(f"key fingerprint {fp} already exists in the store")
        entry = {
            "fp": fp,
            "key_sha256": hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
            "label": str(label),
            "created_at": _now_iso(),
            "created_by": str(created_by),
            "source": "file",
        }
        entries.append(entry)
        _write(entries, environ)
    logger.info("admin key minted: fp=%s label=%r by=%s (raw key never logged)", fp, label, created_by)
    return dict(entry)


def remove_key(fp: str, environ: dict[str, str] | None = None) -> bool:
    """Drop one key by fingerprint. True when removed, False when absent."""
    path = keys_file_path(environ)
    if path is None:
        return False
    with _rmw_lock:
        entries = _load(environ)
        kept = [e for e in entries if e.get("fp") != fp]
        if len(kept) == len(entries):
            return False
        _write(kept, environ)
    logger.info("admin key revoked: fp=%s", fp)
    return True


# ---------------------------------------------------------------------------
# presentation matching (the middleware union's store half)
# ---------------------------------------------------------------------------


def match_presentation(raw: str, environ: dict[str, str] | None = None) -> dict | None:
    """Authenticate a PRESENTED raw key against the store: hash it (full
    sha256 hex — the ``key_sha256`` the mint stored) and constant-time
    compare against each entry (count-bounded, ``hmac.compare_digest``).

    Returns the matching KeyEntry or None. A cheap no-op when the store is
    disabled (``keys_file_path()`` None → None immediately, zero I/O) —
    the no-store deployment pays nothing for the union. The raw key is
    never logged or stored here.
    """
    path = keys_file_path(environ)
    if path is None or not raw:
        return None
    presented = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    for entry in _load(environ):
        stored = entry.get("key_sha256") if isinstance(entry, dict) else None
        if not stored or not isinstance(stored, str):
            continue  # a Secret-report or legacy entry without the digest
        if hmac.compare_digest(presented.encode("utf-8"), stored.encode("utf-8")):
            return dict(entry)
    return None


# ---------------------------------------------------------------------------
# the admin designation (policy-side; hot-reloads with the policy file)
# ---------------------------------------------------------------------------


def is_admin(caller) -> bool:
    """True when the resolved Caller is a designated admin.

    Designation lives in the policy document (``admins:`` list of subjects
    and/or ``sha256:<12hex>`` fingerprints, hot-reloaded with the file;
    the loader normalizes the ``key:``-prefixed spelling to the bare fp).
    Fail-closed: no policy, no admins list, or an anonymous caller → False.
    """
    try:
        from .policy import policy_store

        pol = policy_store().get()
    except Exception:
        return False
    if pol is None or not getattr(pol, "admins", ()):
        return False
    if getattr(caller, "is_anonymous", False) or caller is None:
        return False
    subject = getattr(caller, "subject", None)
    if subject and subject in pol.admins:
        return True
    fp = getattr(caller, "key_fp", None)
    return bool(fp) and fp in pol.admins
