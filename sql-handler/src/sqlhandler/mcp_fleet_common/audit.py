"""The fleet's shared hash-chained JSONL audit writer.

Wave-6 G1 (decision D16) generalization of applygate's audit trail (Wave-3
C4 — the fleet's newest, strongest audit writer), so every governed server
can adopt the same tamper-evident format.

Entry schema (readers of the old 7-key format stay compatible — additive
fields only). Every entry carries:

* ``prev_sha256`` — sha256 of the PREVIOUS line's JSON text (no trailing
  newline); the very first line of an empty trail uses the 64-zero genesis.
  Tampering with, truncating, or reordering any line breaks every later
  link (:func:`verify_audit_chain` runs the check; the README documents the
  procedure).
* ``caller`` — OPTIONAL (this generalization's knob): non-secret caller
  identity resolved at the auth layer, e.g. ``{"key_fp": "sha256:<12hex>",
  "client": "10.1.2.3:51000"}`` — a stable FINGERPRINT of the matched key,
  never the key itself. Pass a ``caller_provider`` (a zero-arg callable) to
  attribute entries; omit it and no ``caller`` field is written (the
  workbench shape).

Everything is best-effort by design — an audit sink must never block or
crash the operation it records: failures print a loud stderr warning and the
tool result keeps reporting the truth of the underlying operation. Writes
are serialized by an instance lock so concurrent callers chain correctly.
The environment is consulted per append (the fleet re-read convention).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable
from typing import TypedDict

__all__ = [
    "AUDIT_GENESIS",
    "CALLER_CONTEXT",
    "Caller",
    "HashChainedAuditLog",
    "key_fingerprint",
    "verify_audit_chain",
]

AUDIT_GENESIS = "0" * 64


#: sha256 of *key* truncated to 12 hex — the stable, NON-SECRET caller
#: fingerprint (the raw key never enters an audit trail).
def key_fingerprint(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class Caller:
    """Resolved identity of one request's caller (K8S-MCP's fleet pattern).

    ``key_fp`` is :func:`key_fingerprint` of the MATCHED configured key (or
    None when anonymous/unmatched); ``client`` is the ASGI client host:port
    when known. NamedTuple so entries stay plain-JSON serializable.
    """

    def __init__(self, key_fp: str | None = None, client: str | None = None):
        self.key_fp = key_fp
        self.client = client

    def as_dict(self) -> dict:
        return {"key_fp": self.key_fp, "client": self.client}

    def __eq__(self, other) -> bool:  # pragma: no cover - trivial
        return isinstance(other, Caller) and self.key_fp == other.key_fp and self.client == other.client

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Caller(key_fp={self.key_fp!r}, client={self.client!r})"


#: The request-scoped caller slot — the auth/capture middleware sets it per
#: request; the audit writer reads it when no explicit provider is bound.
CALLER_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("mcp_fleet_common_caller", default=None)


def _caller_from_context():
    caller = CALLER_CONTEXT.get()
    if caller is None:
        return None
    if isinstance(caller, dict):
        return caller
    return caller.as_dict()


class HashChainedAuditLog:
    """Append-only JSONL trail with the ``prev_sha256`` hash chain.

    Path resolution precedence: the *path* given per ``append`` call → the
    constructor's *path* → the *env_var* when set → *default_path*. The env
    is consulted per append, so tests (and operators) can retarget the trail
    without reimporting.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        env_var: str | None = None,
        default_path: str | None = None,
        caller_provider: Callable[[], dict | None] | None = _caller_from_context,
        stderr=None,
    ) -> None:
        self.path = path
        self.env_var = env_var
        self.default_path = default_path
        self.caller_provider = caller_provider
        # None (the default) binds the CURRENT sys.stderr at call time — a
        # default of `stderr=sys.stderr` would freeze the stream at import
        # time and dodge pytest's capture (and any runtime redirection).
        self._stderr = stderr if stderr is not None else sys.stderr
        self._lock = threading.Lock()

    # -- chain plumbing ---------------------------------------------------------

    @staticmethod
    def _last_line_sha(path: str) -> str:
        """sha256 of the trail's last complete line (no trailing newline) —
        the value the next entry stores as prev_sha256. An empty/missing file
        (or an unreadable one) seeds the chain from the genesis constant."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size == 0:
                    return AUDIT_GENESIS
                window = min(size, 64 * 1024)  # audit lines are ~1 KiB; headroom ample
                fh.seek(size - window)
                data = fh.read(window)
            last = [ln for ln in data.split(b"\n") if ln.strip()]
            if not last:
                return AUDIT_GENESIS
            return hashlib.sha256(last[-1]).hexdigest()
        except OSError:
            return AUDIT_GENESIS

    # -- the writer ---------------------------------------------------------------

    def append(self, entry: dict, *, path: str | None = None) -> None:
        """Append one entry with its chain link (+ caller when one resolves).

        The caller's dict is copied; ``prev_sha256`` is added here, and
        ``caller`` only when the bound provider resolves an identity (None
        provider or anonymous resolution → the field is omitted entirely).
        Never raises: OS errors warn loudly on stderr instead (best-effort by
        design — the audit sink must not block or fail the recorded action).
        """
        resolved = path or self.path or (os.environ.get(self.env_var) if self.env_var else None) or self.default_path
        try:
            if resolved is None:
                raise OSError("no audit path configured (path/env_var/default_path)")

            record = dict(entry)
            parent = os.path.dirname(resolved)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._lock:
                record["prev_sha256"] = self._last_line_sha(resolved)
                if self.caller_provider is not None:
                    try:
                        caller = self.caller_provider()
                    except Exception:
                        caller = None
                    if caller is not None:
                        record["caller"] = caller
                line = json.dumps(record, sort_keys=True)
                with open(resolved, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            # Best-effort by design — but never quiet.
            print(
                f"[audit] WARNING: could not append audit entry to {resolved}: {exc}",
                file=self._stderr,
            )

    # -- the verifier ---------------------------------------------------------------

    def verify(self, path: str | None = None) -> _ChainReport:
        """Verify the hash chain over a whole trail file (see module doc)."""
        resolved = path or self.path
        if resolved is None and self.env_var:
            resolved = os.environ.get(self.env_var) or None
        if resolved is None:
            resolved = self.default_path
        if resolved is None:
            raise ValueError("no audit path to verify (path/env_var/default_path)")
        return verify_audit_chain(resolved)


class _ChainReport(TypedDict):
    """Report shape of :func:`verify_audit_chain` (the applygate verifier's, kept identical)."""

    file: str
    exists: bool
    entries: int
    legacy_entries: int
    ok: bool
    first_bad_line: int | None
    error: str


def verify_audit_chain(path: str) -> _ChainReport:
    """Verify the audit hash chain over a whole trail file.

    Each non-empty line must carry ``prev_sha256 == sha256(previous line)``;
    pre-hardening entries (no ``prev_sha256`` field) are treated as chain
    roots, so old trails verify from the first chained entry onward. A
    blank/injected line, a modified line, or a reordered trail is reported
    with the first offending line number.

    Report shape (the applygate verifier's, kept identical):
    ``{file, exists, entries, legacy_entries, ok, first_bad_line, error}``.
    """
    result: _ChainReport = {
        "file": path,
        "exists": True,
        "entries": 0,
        "legacy_entries": 0,
        "ok": True,
        "first_bad_line": None,
        "error": "",
    }
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        result["exists"] = False
        return result
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"could not read audit file: {exc}"
        return result
    prev = AUDIT_GENESIS
    for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = f"line {lineno} is blank — the chain has a gap (injected or corrupted line)"
            return result
        try:
            entry = json.loads(line)
        except ValueError:
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = f"line {lineno} is not valid JSON — the trail was modified"
            return result
        if not isinstance(entry, dict):
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = f"line {lineno} is not a JSON object"
            return result
        got = entry.get("prev_sha256")
        if got is None:
            # Pre-hardening entry: no chain link to check; it seeds the chain.
            result["legacy_entries"] += 1
        elif got != prev:
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = (
                f"line {lineno}: prev_sha256 does not match the sha256 of the previous line — "
                "the trail was tampered with, truncated, or reordered"
            )
            return result
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        result["entries"] += 1
    return result
