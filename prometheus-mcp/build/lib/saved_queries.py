"""Saved-query store for the Prometheus MCP server (Wave-5 F4 — additive).

A small name→{query, params} store behind four MCP tools
(``query_save`` / ``query_list`` / ``query_delete`` / ``query_saved``).
It is deliberately boring: a JSON file written atomically (write-to-temp
in the SAME directory, fsync, ``os.replace``) or, when
``PROMETHEUS_SAVED_QUERIES_PATH`` is unset, a plain in-memory dict that
lives for the process session — the default UX is unchanged (nothing
persists, nothing new on disk).

Contract:

* ``PROMETHEUS_SAVED_QUERIES_PATH`` — unset/empty (default) → in-memory
  only for the session (the tool results say so); set → durable JSON at
  that path. Re-read per call (the fleet env pattern), so operators and
  tests can flip it without reimporting.
* Names are sanitized before they become keys: whitespace collapsed,
  anything outside ``[A-Za-z0-9._- ]`` replaced with ``_``, length capped.
  Saving under an existing name overwrites it (an update, not a dup).
* Writes are atomic: a partial write can never clobber the previous
  file — the temp file is removed on any failure and the rename is the
  commit point.
* ``params`` (save-time defaults and run-time overrides) are restricted
  to the keys the existing query paths already accept —
  ``mode``/``time``/``start``/``end``/``step`` — so ``query_saved`` can
  route through the EXISTING instant/range paths unchanged (clamps, caps
  and validation all apply; nothing here re-implements a query).
* The store is per-process (each replica has its own); the file is
  per-replica state, not a shared multi-writer database.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time

SAVED_QUERIES_ENV = "PROMETHEUS_SAVED_QUERIES_PATH"

# Upper bounds (honest caps instead of unbounded growth): 100 saved
# queries, names up to 64 chars. Saving one more past the cap fails with
# a clear error rather than silently evicting.
MAX_SAVED_QUERIES = 100
MAX_NAME_LENGTH = 64

# The params keys query_saved accepts — exactly the arguments the existing
# instant/range paths take (mode selects the path; the rest pass through).
PARAM_KEYS = frozenset({"mode", "time", "start", "end", "step"})
PARAM_MODES = frozenset({"instant", "range"})

# Sanitizing: collapse whitespace, then replace anything outside the safe
# set (letters, digits, dot, underscore, hyphen, single space) with "_".
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._\- ]")


def saved_queries_path(environ: dict[str, str] | None = None) -> str | None:
    """Configured store path, or None for the in-memory-only session store."""
    env = os.environ if environ is None else environ
    raw = (env.get(SAVED_QUERIES_ENV) or "").strip()
    return raw or None


def sanitize_name(name: str) -> str:
    """Normalize a saved-query name (raises ValueError when nothing survives).

    "  my  cool/query!!  " → "my cool_query__" — deterministic, so a later
    ``query_delete("my cool / query !!")`` addresses the same entry.
    """
    text = " ".join(str(name or "").split())
    cleaned = _UNSAFE_NAME_CHARS.sub("_", text)[:MAX_NAME_LENGTH].strip()
    if not cleaned:
        raise ValueError(
            "query name is required (got "
            f"{str(name)!r}; allowed characters: letters, digits, '._- ' — "
            "others are replaced with '_')"
        )
    return cleaned


def normalize_params(params) -> dict[str, str]:
    """Validate params into {str: str} with only the known keys."""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object of query-path arguments")  # noqa: TRY004 — 400-style tool semantics (fleet precedent: webui._json_body)
    unknown = sorted(set(params) - PARAM_KEYS)
    if unknown:
        raise ValueError("params keys must be " + "/".join(sorted(PARAM_KEYS)) + f" (unknown: {', '.join(unknown)})")
    out: dict[str, str] = {}
    for key in sorted(set(params) & PARAM_KEYS):
        value = params[key]
        if value is None:
            continue
        if isinstance(value, bool):
            raise ValueError(f"params.{key} must be a string (got a boolean)")  # noqa: TRY004 — same 400-style tool semantics
        text = str(value).strip()
        if not text:
            continue
        if key == "mode" and text not in PARAM_MODES:
            raise ValueError("params.mode must be 'instant' or 'range'")
        out[key] = text
    return out


class SavedQueryStore:
    """name → {name, raw_name, query, params, saved_at}, optionally file-backed.

    The sanitized name is the KEY; the raw name the caller used is kept as
    an alias so ``query_saved``/``query_delete`` accept either spelling
    (sanitize is lossy: 'my test!' stores as 'my test_' — both must work).
    """

    def __init__(self, path: str | None = None):
        self.path = path
        self._entries: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}  # raw name -> sanitized key
        self._seq = 0  # monotonic insertion counter (clock-independent ordering)
        if path:
            self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        """Load the JSON file; missing/corrupt → empty store (next save
        rewrites it whole — the file is a cache of the store, never the
        other way around)."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            data = data.get("queries", [])
        if not isinstance(data, list):
            return
        for raw in data:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            query = raw.get("query")
            if not isinstance(name, str) or not isinstance(query, str) or not name or not query.strip():
                continue
            try:
                params = normalize_params(raw.get("params"))
            except ValueError:
                params = {}
            self._entries[name] = {
                "name": name,
                "raw_name": raw.get("raw_name") if isinstance(raw.get("raw_name"), str) else name,
                "query": query.strip(),
                "params": params,
                "saved_at": raw.get("saved_at") if isinstance(raw.get("saved_at"), int) else int(time.time()),
                "seq": raw.get("seq") if isinstance(raw.get("seq"), int) else 0,
            }
        self._seq = max((e["seq"] for e in self._entries.values()), default=0)
        self._rebuild_aliases()

    def _rebuild_aliases(self) -> None:
        self._aliases = {
            e["raw_name"]: e["name"] for e in self._entries.values() if e["raw_name"] and e["raw_name"] != e["name"]
        }

    def _flush(self) -> None:
        """Write the whole store atomically: temp file in the SAME directory
        (same filesystem → the rename is atomic), fsync, os.replace. Any
        failure removes the temp file and leaves the previous file intact."""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".saved-queries-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    {"version": 1, "queries": list(self._entries.values())},
                    fh,
                    indent=2,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)  # the atomic commit point
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ CRUD
    def save(self, name: str, query: str, params: dict | None = None) -> dict:
        """Insert/overwrite one entry (returns the stored entry)."""
        raw = " ".join(str(name or "").split())
        clean = sanitize_name(raw)
        query_text = str(query or "").strip()
        if not query_text:
            raise ValueError("query is required to save a query")
        norm = normalize_params(params)
        if clean not in self._entries and len(self._entries) >= MAX_SAVED_QUERIES:
            raise ValueError(f"saved-query store is full ({MAX_SAVED_QUERIES} entries) — delete one first")
        entry = {
            "name": clean,
            "raw_name": raw if raw != clean else clean,
            "query": query_text,
            "params": norm,
            "saved_at": int(time.time()),
            "seq": self._seq + 1,
        }
        self._seq += 1
        previous = self._entries.get(clean)
        self._entries[clean] = entry
        if entry["raw_name"] != clean:
            self._aliases[entry["raw_name"]] = clean
        if self.path:
            try:
                self._flush()
            except BaseException:
                # roll back: a save that could not be persisted did not happen
                if previous is None:
                    self._entries.pop(clean, None)
                else:
                    self._entries[clean] = previous
                raise
        return entry

    def get(self, name: str) -> dict | None:
        """Look up by raw or sanitized name; None when absent."""
        for candidate in self._name_candidates(name):
            entry = self._entries.get(candidate)
            if entry is not None:
                return entry
        return None

    def delete(self, name: str) -> bool:
        """Remove one entry; True when it existed (the file is rewritten)."""
        for candidate in self._name_candidates(name):
            if candidate in self._entries:
                removed = self._entries.pop(candidate)
                if self.path:
                    try:
                        self._flush()
                    except BaseException:
                        self._entries[candidate] = removed  # roll back
                        raise
                return True
        return False

    def items(self) -> list[dict]:
        """All entries, newest first (insertion sequence — clock-independent,
        deterministic for the list tool)."""
        return sorted(self._entries.values(), key=lambda e: (-e["seq"], e["name"]))

    def __len__(self) -> int:
        return len(self._entries)

    def _name_candidates(self, name: str) -> list[str]:
        """Lookup order: exact name → its sanitized form → the alias the
        entry was saved under (sanitize is lossy; the alias closes the gap)."""
        text = str(name or "")
        candidates = [text]
        try:
            clean = sanitize_name(text)
        except ValueError:
            clean = None
        if clean:
            candidates.append(clean)
        alias = self._aliases.get(text)
        if alias:
            candidates.append(alias)
        seen, unique = set(), []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                unique.append(c)
        return unique


# ----------------------------------------------------- process-wide accessor
_MEMORY_STORE: SavedQueryStore | None = None
_FILE_STORE: tuple[str, SavedQueryStore] | None = None


def get_store(environ: dict[str, str] | None = None) -> SavedQueryStore:
    """The session store — in-memory when no path is configured, file-backed
    otherwise.

    The two live in separate cache slots: flipping the env to a path (and
    back) never discards the session's in-memory queries, and pointing the
    env at a DIFFERENT path reloads that file. Re-read per call (the fleet
    env pattern) so tests and operators can flip
    PROMETHEUS_SAVED_QUERIES_PATH without reimporting; use
    :func:`reset_store` to drop both cached instances entirely.
    """
    global _MEMORY_STORE, _FILE_STORE
    path = saved_queries_path(environ)
    if path is None:
        if _MEMORY_STORE is None:
            _MEMORY_STORE = SavedQueryStore(None)
        return _MEMORY_STORE
    if _FILE_STORE is None or _FILE_STORE[0] != path:
        _FILE_STORE = (path, SavedQueryStore(path))
    return _FILE_STORE[1]


def reset_store() -> None:
    """Forget the cached stores (test isolation / explicit re-open)."""
    global _MEMORY_STORE, _FILE_STORE
    _MEMORY_STORE = None
    _FILE_STORE = None
