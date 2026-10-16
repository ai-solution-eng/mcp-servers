"""Saved parameterized queries — name → SQL + bind-params store (additive, Wave 5).

A small JSON-file store under the existing data/config path (default next to
the disk-warm cache / catalog store; ``SQLHANDLER_SAVED_QUERIES_PATH``
overrides) mapping a caller-chosen name to a SQL template plus optional bind
parameters, exposed as MCP tools (``query_save`` / ``query_list`` /
``query_delete`` / ``query_saved``) and REST routes under
``/api/saved-queries/*``.

Injection safety: parameters are **bind parameters** — the stored SQL keeps
its ``$name`` / ``?`` placeholders and the values travel to DuckDB through
the same ``con.sql(sql, params=...)`` bind path ``run_sql`` uses. Saved SQL is
NEVER string-interpolated, at save time or at run time.

Mutation gating (the audit's semantic-catalog poisoning warning, applied
proactively to the new store): a saved query is a template other agents will
run, so WRITES (``query_save`` / ``query_delete``, and the REST
POST/DELETE routes) require a configured credential whenever one exists:

* ``SQLHANDLER_API_TOKEN`` or MCP keys (``MCP_API_KEYS`` /
  ``SQLHANDLER_API_KEYS``) configured → an unauthenticated write is refused
  (REST: 401; MCP over HTTP: the presented credential is verified per call —
  defense in depth on top of the /mcp middleware, which covers the
  token-only deployments where /mcp itself stays open).
* NO credential configured → the known **single-user-local mode**: writes are
  allowed and the posture is logged loudly at startup.

Reads (``query_list`` / ``query_saved``) follow the same posture as /mcp
itself — no additional gate.

The store is a JSON file (not the semantic catalog): operator-inspectable,
atomic-replaced on every write, and independent of the engine's caches.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .engine import _validate_params
from .sqlguard import assert_mcp_readonly, extract_statement_spans, mcp_readonly_enabled

try:  # cross-process file locking (POSIX); elsewhere the store is thread-safe only
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("sqlhandler.saved")

_SAVED_PATH_ENV = "SQLHANDLER_SAVED_QUERIES_PATH"
_STORE_VERSION = 1

#: Cross-replica serialization: every read-modify-write cycle holds this flock
#: (see :func:`_process_lock`). The timeout keeps a wedged lock file from
#: turning query_save into an outage -- availability over strict serialization
#: for a human-rate store.
_LOCK_TIMEOUT_SECONDS = 10.0


@contextlib.contextmanager
def _process_lock(path: Path, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Serialize one read-modify-write cycle ACROSS replicas (advisory flock).

    The store may live on an RWX PVC shared by every replica of a scaled-out
    deployment; the class's thread lock cannot see another process, so two
    replicas saving/deleting concurrently were last-writer-wins and one edit
    was silently lost. An flock on a sidecar lock file (same directory as the
    store) closes that without changing the file format. Degrades: no fcntl ->
    thread-only; lock busy past the timeout -> proceed unlocked with a warning
    (never an outage).
    """
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield  # an unwritable dir is the store's problem, not the lock's
        return
    lock_path = path.with_name(path.name + ".lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "saved-query store lock %s stayed busy past %.0fs; "
                        "proceeding unlocked (concurrent edits may conflict)",
                        lock_path,
                        timeout,
                    )
                    break
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

# A saved query is a JSON-file entry, not a filesystem path: forbid path
# separators, control characters and surrounding whitespace, cap the length.
# ("/" is forbidden too so every name stays a single REST path segment.)
_NAME_RE = re.compile(r"[^/\\\x00-\x1f]{1,128}")
_SQL_MAX_CHARS = 100_000
_DESCRIPTION_MAX_CHARS = 2_000

#: Hard bound on stored entries. The store is a single JSON file rewritten in
#: full on every save, so an unbounded map is a memory + disk-growth channel
#: (audit 2026-10-16 P2). Overridable for tests/operators.
_MAX_ENTRIES_ENV = "SQLHANDLER_SAVED_QUERIES_MAX"
_MAX_ENTRIES_DEFAULT = 500


def max_entries() -> int:
    """Entry cap (SQLHANDLER_SAVED_QUERIES_MAX, default 500, min 1)."""
    raw = os.environ.get(_MAX_ENTRIES_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _MAX_ENTRIES_DEFAULT
    return value if value > 0 else _MAX_ENTRIES_DEFAULT


def saved_queries_path() -> str:
    """Where the saved-query store lives.

    ``SQLHANDLER_SAVED_QUERIES_PATH`` wins; the default sits next to the
    disk-warm cache (``SQLHANDLER_CACHE_DIR`` — a writable emptyDir in every
    supported deployment) or the platform temp dir, mirroring the
    semantic-catalog store's precedence.
    """
    raw = os.environ.get(_SAVED_PATH_ENV, "").strip()
    if raw:
        return raw
    cache_dir = os.environ.get("SQLHANDLER_CACHE_DIR", "").strip()
    base = cache_dir or tempfile.gettempdir()
    return str(Path(base) / "sqlhandler-saved-queries.json")


def _owner_for(caller) -> str | None:
    """The owner scope to record/filter on, or None when scoping is off.

    Owner scoping is active only when policy ENFORCEMENT is on AND a caller
    was supplied -- enforcement off keeps the shared store byte-identical (no
    ``owner`` field, every caller sees everything), which is what the
    pre-attribution tests pin.

    The scope comes from :func:`policy.owner_key`: subject when attributed,
    else the key fingerprint, else ``anonymous``. NEVER the raw key.
    """
    if caller is None:
        return None
    from . import policy as policy_mod

    if not policy_mod.policy_enabled():
        return None
    return policy_mod.owner_key(caller)


def _owner_of(entry: object) -> str | None:
    """The recorded owner of a stored entry (None for legacy/unowned)."""
    return entry.get("owner") if isinstance(entry, dict) else None


def validate_query_name(name: object) -> str:
    """Validate + normalize a saved-query name (clear client errors)."""
    if not isinstance(name, str):
        raise ValueError("Saved-query name must be a string.")  # noqa: TRY004
    cleaned = name.strip()
    if not cleaned or not _NAME_RE.fullmatch(cleaned):
        raise ValueError(
            "Saved-query name must be 1-128 characters with no path separators "
            "or control characters."
        )
    return cleaned


# ---------------------------------------------------------------------------
# credential posture (shared by the MCP tools and the REST write routes)
# ---------------------------------------------------------------------------


def credential_sources() -> list[str]:
    """Which credential envs are configured right now (re-read per call)."""
    sources: list[str] = []
    if os.environ.get("SQLHANDLER_API_TOKEN", "").strip():
        sources.append("SQLHANDLER_API_TOKEN")
    for env in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        if any(k.strip() for k in os.environ.get(env, "").split(",")):
            sources.append(env)
    return sources


def auth_configured() -> bool:
    """True when any credential source is configured (writes become gated)."""
    return bool(credential_sources())


def _configured_credentials() -> list[str]:
    creds: list[str] = []
    token = os.environ.get("SQLHANDLER_API_TOKEN", "").strip()
    if token:
        creds.append(token)
    for env in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        for k in os.environ.get(env, "").split(","):
            k = k.strip()
            if k and k not in creds:
                creds.append(k)
    return creds


def presented_credential(request) -> str:
    """The caller's credential from an ASGI/Starlette request ('' when none).

    Same header conventions as the /mcp and /api middlewares: ``Authorization:
    Bearer <key>``, ``X-API-Key`` or ``X-API-Token``.
    """
    if request is None:
        return ""
    try:
        headers = request.headers
    except Exception:
        return ""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("x-api-key") or headers.get("x-api-token") or "").strip()


def credential_ok(provided: str) -> bool:
    """Constant-time check of a presented credential against the configured set."""
    creds = _configured_credentials()
    if not creds or not provided:
        return False
    return any(
        hmac.compare_digest(provided.encode("utf-8"), candidate.encode("utf-8")) for candidate in creds
    )


class NotAuthorized(Exception):
    """A saved-query write refused by the mutation gate (HTTP 401 on REST)."""


class UnknownSavedQuery(KeyError):
    """A save/delete/run addressed a name that is not in the store.

    KeyError-shaped (REST routes map it to 404) with a clean str() so the
    MCP tool text doesn't grow Python's repr quotes.
    """

    def __str__(self) -> str:
        return self.args[0] if self.args else ""


def assert_write_allowed(request) -> None:
    """Gate saved-query WRITES (save/delete) on a configured credential.

    * No credential env configured → single-user-local mode: allowed.
    * Credential env(s) configured → the request must PRESENT a valid one.
      An HTTP request that doesn't (or a stdio tool call, which cannot
      present HTTP credentials) is refused — fail closed against the
      catalog-poisoning channel.
    """
    sources = credential_sources()
    if not sources:
        return  # known single-user-local mode (startup log says so, loudly)
    provided = presented_credential(request)
    if credential_ok(provided):
        return
    raise NotAuthorized(
        "saved-query writes require a credential: "
        f"{', '.join(sources)} is configured — present it as Authorization: Bearer, "
        "X-API-Key or X-API-Token (streamable-http /mcp or /api/saved-queries). "
        "A stdio MCP call cannot carry credentials; clear the auth envs for "
        "local single-user mode."
    )


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class SavedQueryStore:
    """Name → {sql, params, description} JSON store (atomic, thread-safe)."""

    def __init__(self, path: str | None = None):
        self._path = path or saved_queries_path()
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path

    # ---- io
    def _empty(self) -> dict:
        return {"version": _STORE_VERSION, "queries": {}}

    def _quarantine(self, reason: str) -> str | None:
        """Move an unusable store aside instead of letting it be overwritten.

        A corrupt store used to load as EMPTY, so the very next ``save()``
        rewrote the file with one entry and DESTROYED every other saved query
        (audit 2026-10-16 P2). The bytes are preserved under
        ``<path>.corrupt-<epoch>`` so an operator can recover them by hand.
        Best-effort: a failed rename is logged, never raised (a read must not
        start failing because the quarantine could not be written).
        """
        stamp = int(time.time())
        dest = f"{self._path}.corrupt-{stamp}"
        try:
            os.replace(self._path, dest)
        except OSError:
            logger.warning(
                "saved-query store %s is unusable (%s) and could not be quarantined; "
                "leaving it in place (writes will refuse until it is fixed)",
                self._path,
                reason,
                exc_info=True,
            )
            return None
        logger.error(
            "saved-query store %s was unusable (%s); moved to %s — recover entries "
            "from that file, or delete it to start empty",
            self._path,
            reason,
            dest,
        )
        return dest

    def _load(self) -> dict:
        """Read the store.

        A missing file is an empty store. A file that exists but cannot be
        parsed is QUARANTINED (``<path>.corrupt-<epoch>``) and reported as
        empty, so the next write cannot silently destroy recoverable data.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return self._empty()
        except json.JSONDecodeError as exc:
            self._quarantine(f"invalid JSON: {exc}")
            return self._empty()
        except OSError as exc:
            # Unreadable (permissions, transient IO): do NOT quarantine (the
            # bytes may be perfectly fine) and do NOT treat it as empty for
            # WRITE purposes -- see _load_for_write.
            logger.warning("saved-query store %s could not be read (%s)", self._path, exc)
            return self._empty()
        if not isinstance(data, dict) or not isinstance(data.get("queries"), dict):
            self._quarantine("unexpected JSON shape")
            return self._empty()
        return data

    def _load_for_write(self) -> dict:
        """``_load`` for a MUTATING caller: refuse when the store exists but
        cannot be read, instead of replacing it with an empty one.

        ``_load`` answers "what can I show?" (empty on any problem). A write
        needs the stronger question "do I have the CURRENT contents?", because
        writing an empty base is data loss. An absent file is still fine (first
        save); an unreadable one is refused loudly.
        """
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    json.load(fh)
            except FileNotFoundError:
                pass  # raced a quarantine: an empty base is correct now
            except (json.JSONDecodeError, OSError) as exc:
                raise NotAuthorized(
                    f"The saved-query store {self._path} exists but cannot be read "
                    f"({exc}). Refusing to overwrite it — fix or remove the file "
                    "(a corrupt store is quarantined to <path>.corrupt-<epoch> on "
                    "the next read)."
                ) from exc
        return self._load()

    def _write(self, data: dict) -> None:
        """Atomic replace: a crashed write can never corrupt the store."""
        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".saved-queries-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _entry_summary(name: str, entry: dict) -> dict:
        out = {"name": name, "sql": entry.get("sql", "")}
        if entry.get("params") is not None:
            out["params"] = entry.get("params")
        if entry.get("description"):
            out["description"] = entry.get("description")
        for k in ("created_at", "updated_at"):
            if entry.get(k):
                out[k] = entry.get(k)
        return out

    # ---- operations
    def save(
        self,
        name: object,
        sql: str,
        params: object | None = None,
        description: object | None = None,
        caller=None,
    ) -> dict:
        """Validate + upsert one saved query; returns the stored entry summary.

        The SQL must PARSE (DuckDB's own parser) and, while the MCP read-only
        mode is on (decision D2, default), must be SELECT-only — the same
        guard ``query_saved`` re-applies at run time, so a poisoned store
        still cannot make another agent execute DDL.

        Identity spine: when policy enforcement is ON and a caller is known,
        the entry records its ``owner`` (subject slug or key fingerprint —
        never the raw key); reads/lists are then owner-scoped (another
        caller's SQL text can name tables a policy hides) and a cross-owner
        delete is refused like an unknown name. Enforcement off keeps the
        shared store byte-identical (no owner field).
        """
        clean = validate_query_name(name)
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("Provide the SQL to save.")
        if len(sql) > _SQL_MAX_CHARS:
            raise ValueError(f"SQL too long ({len(sql)} chars; cap {_SQL_MAX_CHARS}).")
        if mcp_readonly_enabled():
            sql = assert_mcp_readonly(sql)  # ValueError with the D2 error text
        else:
            extract_statement_spans(sql)  # parse check only
        _validate_params(params)  # scalars only — the bind contract
        if description is not None:
            if not isinstance(description, str):
                raise ValueError("description must be a string.")
            if len(description) > _DESCRIPTION_MAX_CHARS:
                raise ValueError(f"description too long (cap {_DESCRIPTION_MAX_CHARS} chars).")
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        owner = _owner_for(caller)
        # The process lock serializes the read-modify-write ACROSS replicas; the
        # thread lock covers same-process threads. Order is thread-then-process
        # everywhere so the two can never deadlock against each other.
        with self._lock, _process_lock(Path(self._path)):
            data = self._load_for_write()
            queries = data["queries"]
            if clean not in queries and len(queries) >= max_entries():
                # Bounded store: an unbounded name->SQL map is a memory and
                # disk-growth channel for any caller allowed to save (audit
                # 2026-10-16 P2). Refuse clearly; an OVERWRITE of an existing
                # name is always allowed, so the cap never blocks an edit.
                raise ValueError(
                    f"Saved-query store is full ({len(queries)} of {max_entries()} entries, "
                    "SQLHANDLER_SAVED_QUERIES_MAX). Delete a query you no longer need "
                    "(query_delete) or overwrite an existing name, then retry."
                )
            created = now
            if clean in queries and isinstance(queries[clean], dict):
                created = queries[clean].get("created_at") or now
            entry = {
                "sql": sql.strip(),
                "params": params if params is not None else None,
                "description": (description or "").strip() or None,
                "created_at": created,
                "updated_at": now,
            }
            if owner is not None:
                entry["owner"] = owner
            entry = {k: v for k, v in entry.items() if v is not None or k in ("sql", "params", "created_at", "updated_at")}
            queries[clean] = entry
            self._write(data)
        return self._entry_summary(clean, entry)

    def list(self, caller=None) -> list[dict]:
        """Every VISIBLE saved query (owner-scoped under enforcement).

        Enforcement ON + a caller -> only that caller's own entries: a saved
        SQL template NAMES tables, so even reading another caller's reveals
        policy-hidden names. Enforcement off / no caller -> everything,
        byte-identical to the shared store.
        """
        owner = _owner_for(caller)
        with self._lock, _process_lock(Path(self._path)):
            data = self._load()
            items = sorted(data["queries"].items())
        if owner is None:
            return [self._entry_summary(n, e) for n, e in items]
        # An entry with NO recorded owner predates attribution (or was written
        # with enforcement off): it stays visible to everyone, so flipping
        # enforcement on cannot make existing saved queries vanish.
        return [
            self._entry_summary(n, e)
            for n, e in items
            if not isinstance(e, dict) or _owner_of(e) in (None, owner)
        ]

    def get(self, name: object, caller=None) -> dict | None:
        """One entry, or None when it does not exist OR is another owner's.

        A cross-owner entry resolves as UNKNOWN (not as a permission error),
        so the store cannot be used to probe which names another caller has
        saved.
        """
        clean = validate_query_name(name)
        owner = _owner_for(caller)
        with self._lock, _process_lock(Path(self._path)):
            data = self._load()
            entry = data["queries"].get(clean)
        if entry is None:
            return None
        recorded = _owner_of(entry)
        if owner is not None and recorded is not None and recorded != owner:
            return None  # invisible: never reveal another caller's SQL text
        return self._entry_summary(clean, entry)

    def delete(self, name: object, caller=None) -> bool:
        """Delete one entry; a cross-owner delete is REFUSED (not silent).

        Deleting someone else's saved query is a destructive action on their
        data, so it raises :class:`NotAuthorized` rather than reporting a
        false "not found" -- the caller is authenticated and authorized for
        the surface, just not for that entry.
        """
        clean = validate_query_name(name)
        owner = _owner_for(caller)
        with self._lock, _process_lock(Path(self._path)):
            data = self._load()
            if clean not in data["queries"]:
                return False
            entry = data["queries"][clean]
            recorded = _owner_of(entry)
            if owner is not None and recorded is not None and recorded != owner:
                raise NotAuthorized("saved query belongs to another caller")
            del data["queries"][clean]
            self._write(data)
        return True


# ---------------------------------------------------------------------------
# process-wide store + api handlers (MCP tools and REST routes share them)
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_store: SavedQueryStore | None = None


def saved_query_store() -> SavedQueryStore:
    """The process-wide store (path resolved once per process from the env)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SavedQueryStore()
            if not auth_configured():
                logger.warning(
                    "No credential source is configured (SQLHANDLER_API_TOKEN / "
                    "MCP_API_KEYS / SQLHANDLER_API_KEYS) — saved-query WRITES are "
                    "OPEN (known single-user-local mode). Set a credential to gate "
                    "query_save/query_delete."
                )
            else:
                logger.info(
                    "saved-query writes are gated: credential source(s) %s configured "
                    "(unauthenticated save/delete is refused)",
                    ", ".join(credential_sources()),
                )
        return _store


def reset_saved_query_store() -> None:
    """Test hook: drop the process-wide store (a fresh one builds lazily)."""
    global _store
    with _store_lock:
        _store = None


def api_saved_save(body: dict, request=None, *, caller=None) -> dict:
    """POST /api/saved-queries + MCP query_save.

    ``caller`` is keyword-only on purpose: it is the attributed identity used
    for owner scoping, and it must never be confused with ``request`` (which
    only carries the presented credential).
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    assert_write_allowed(request)
    return saved_query_store().save(
        body.get("name"),
        body.get("sql", ""),
        body.get("params"),
        body.get("description"),
        caller=caller,
    )


def api_saved_list(caller=None) -> list[dict]:
    """GET /api/saved-queries + MCP query_list (owner-scoped under enforcement)."""
    return saved_query_store().list(caller=caller)


def api_saved_delete(name: str, request=None, *, caller=None) -> dict:
    """DELETE /api/saved-queries/{name} + MCP query_delete."""
    assert_write_allowed(request)
    deleted = saved_query_store().delete(name, caller=caller)
    if not deleted:
        raise UnknownSavedQuery(f"Unknown saved query: {name}")
    return {"deleted": name}


def api_saved_run(name: str, body: dict | None = None, *, caller=None) -> tuple[str, object | None, dict]:
    """Resolve a saved query for running; returns (sql, merged_params, entry).

    Call-time params OVERRIDE stored params (dict merge — per-key override;
    a positional list replaces the stored list entirely). The D2 guard is
    RE-applied to the stored SQL at run time: a hand-edited store file
    cannot smuggle DDL past the read-only mode.

    Identity spine: another caller's owner-scoped entry resolves as unknown
    (invisible) when policy enforcement is on.
    """
    body = body or {}
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    entry = saved_query_store().get(name, caller=caller)
    if entry is None:
        raise UnknownSavedQuery(f"Unknown saved query: {name}")
    sql = entry.get("sql", "")
    if mcp_readonly_enabled():
        sql = assert_mcp_readonly(sql)
    stored = entry.get("params")
    call = body.get("params")
    _validate_params(call)
    if isinstance(stored, dict) and isinstance(call, dict):
        merged: object = {**stored, **call}
    else:
        merged = call if call is not None else stored
    return sql, merged, entry
