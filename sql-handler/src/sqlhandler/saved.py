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

logger = logging.getLogger("sqlhandler.saved")

_SAVED_PATH_ENV = "SQLHANDLER_SAVED_QUERIES_PATH"
_STORE_VERSION = 1

# A saved query is a JSON-file entry, not a filesystem path: forbid path
# separators, control characters and surrounding whitespace, cap the length.
# ("/" is forbidden too so every name stays a single REST path segment.)
_NAME_RE = re.compile(r"[^/\\\x00-\x1f]{1,128}")
_SQL_MAX_CHARS = 100_000
_DESCRIPTION_MAX_CHARS = 2_000


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


def validate_query_name(name: object) -> str:
    """Validate + normalize a saved-query name (clear client errors)."""
    if not isinstance(name, str):
        raise ValueError("Saved-query name must be a string.")  # noqa: TRY004
    cleaned = name.strip()
    if not cleaned or not _NAME_RE.fullmatch(cleaned):
        raise ValueError("Saved-query name must be 1-128 characters with no path separators or control characters.")
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
    return any(hmac.compare_digest(provided.encode("utf-8"), candidate.encode("utf-8")) for candidate in creds)


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
    def _load(self) -> dict:
        """Read the store; a missing/corrupt file is an EMPTY store (never an error)."""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"version": _STORE_VERSION, "queries": {}}
        except (OSError, json.JSONDecodeError):
            logger.warning("saved-query store %s unreadable; starting from an empty store", self._path)
            return {"version": _STORE_VERSION, "queries": {}}
        if not isinstance(data, dict) or not isinstance(data.get("queries"), dict):
            logger.warning("saved-query store %s has an unexpected shape; ignoring it", self._path)
            return {"version": _STORE_VERSION, "queries": {}}
        return data

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
    def save(self, name: object, sql: str, params: object | None = None, description: object | None = None) -> dict:
        """Validate + upsert one saved query; returns the stored entry summary.

        The SQL must PARSE (DuckDB's own parser) and, while the MCP read-only
        mode is on (decision D2, default), must be SELECT-only — the same
        guard ``query_saved`` re-applies at run time, so a poisoned store
        still cannot make another agent execute DDL.
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
        with self._lock:
            data = self._load()
            queries = data["queries"]
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
            entry = {
                k: v for k, v in entry.items() if v is not None or k in ("sql", "params", "created_at", "updated_at")
            }
            queries[clean] = entry
            self._write(data)
        return self._entry_summary(clean, entry)

    def list(self) -> list[dict]:
        """Every saved query (bounded by the store itself — a curated set)."""
        with self._lock:
            data = self._load()
            return [self._entry_summary(n, e) for n, e in sorted(data["queries"].items())]

    def get(self, name: object) -> dict | None:
        clean = validate_query_name(name)
        with self._lock:
            data = self._load()
            entry = data["queries"].get(clean)
        return self._entry_summary(clean, entry) if entry else None

    def delete(self, name: object) -> bool:
        clean = validate_query_name(name)
        with self._lock:
            data = self._load()
            if clean not in data["queries"]:
                return False
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


def api_saved_save(body: dict, request=None) -> dict:
    """POST /api/saved-queries + MCP query_save."""
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    assert_write_allowed(request)
    return saved_query_store().save(
        body.get("name"),
        body.get("sql", ""),
        body.get("params"),
        body.get("description"),
    )


def api_saved_list() -> list[dict]:
    """GET /api/saved-queries + MCP query_list (read posture: ungated here)."""
    return saved_query_store().list()


def api_saved_delete(name: str, request=None) -> dict:
    """DELETE /api/saved-queries/{name} + MCP query_delete."""
    assert_write_allowed(request)
    deleted = saved_query_store().delete(name)
    if not deleted:
        raise UnknownSavedQuery(f"Unknown saved query: {name}")
    return {"deleted": name}


def api_saved_run(name: str, body: dict | None = None) -> tuple[str, object | None, dict]:
    """Resolve a saved query for running; returns (sql, merged_params, entry).

    Call-time params OVERRIDE stored params (dict merge — per-key override;
    a positional list replaces the stored list entirely). The D2 guard is
    RE-applied to the stored SQL at run time: a hand-edited store file
    cannot smuggle DDL past the read-only mode.
    """
    body = body or {}
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    entry = saved_query_store().get(name)
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
