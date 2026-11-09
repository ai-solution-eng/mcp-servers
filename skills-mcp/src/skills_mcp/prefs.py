"""Per-identity state on the PVC — the only mutable thing this server owns.

One JSON file per identity under SKILLS_STATE_DIR (default /data/state):

    {
      "disabled": ["memory-curation"],          # global skills this person turned off
      "keys": [                                  # credentials this person minted
        {"id": "k_9f3a1c", "label": "laptop-dsh",
         "sha256": "…", "prefix": "sk_live_9f3a",
         "created_at": "2026-11-09T18:00:00Z",
         "expires_at": null, "revoked_at": null}
      ]
    }

Why files: a minted key appears at runtime; env is frozen at container start.
Why hashes: we only ever verify ("does sha256(presented) match?"), never recall,
so a leaked file yields nothing usable. The plaintext key is shown ONCE, at mint.

A key is USABLE when it is neither revoked nor expired. Revoke is permanent
(the record stays for the audit trail); expiry is a timestamp. Both are checked
on every request, so no restart is needed.

Writes are atomic (temp + os.replace) and serialised by a per-path lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ENV_STATE_DIR = "SKILLS_STATE_DIR"
DEFAULT_STATE_DIR = "/data/state"

_SAFE = re.compile(r"[^a-z0-9._@-]+")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def state_dir() -> Path:
    return Path(os.getenv(ENV_STATE_DIR, "").strip() or DEFAULT_STATE_DIR)


def safe_identity(identity: str) -> str:
    """A filename-safe identity. Refuses anything that could escape the dir."""
    value = (identity or "").strip().lower()
    cleaned = _SAFE.sub("_", value).strip("._")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    if not cleaned:
        raise ValueError("identity must contain at least one usable character")
    return cleaned


def _path_for(identity: str) -> Path:
    return state_dir() / (safe_identity(identity) + ".json")


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ storage


def _migrate(doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """v0.2.x stored a single {"token": {...}}; v1.0 stores {"keys": [...]}.

    Migrated lazily and written back once, so an existing token keeps working
    across the upgrade without anyone re-minting.
    """
    token = doc.get("token")
    if not isinstance(token, dict) or not token.get("sha256"):
        return doc, False
    migrated = {
        "id": "k_legacy",
        "label": "default",
        "sha256": token.get("sha256"),
        "prefix": token.get("prefix") or "",
        "created_at": token.get("created_at") or _now(),
        "expires_at": None,
        "revoked_at": None,
    }
    keys = doc.get("keys")
    doc["keys"] = ([migrated] + list(keys)) if isinstance(keys, list) else [migrated]
    doc.pop("token", None)
    return doc, True


def load(identity: str) -> dict[str, Any]:
    """This identity's state (migrated). Never raises on bad JSON."""
    try:
        raw = json.loads(_path_for(identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    doc, changed = _migrate(raw)
    if changed:
        try:
            save(identity, doc)
        except OSError:
            pass
    return doc


def save(identity: str, doc: dict[str, Any]) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    target = _path_for(identity)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    with _lock_for(str(target)):
        temporary.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)


# ------------------------------------------------------------------ skills


def disabled(identity: str | None) -> set[str]:
    if not identity:
        return set()
    names = load(identity).get("disabled") or []
    return {str(name) for name in names if str(name).strip()}


def set_disabled(identity: str, name: str, is_disabled: bool) -> list[str]:
    """Turn one global skill off/on for this identity. Returns the new list."""
    doc = load(identity)
    current = {str(n) for n in (doc.get("disabled") or [])}
    if is_disabled:
        current.add(name)
    else:
        current.discard(name)
    doc["disabled"] = sorted(current)
    save(identity, doc)
    return doc["disabled"]


# ------------------------------------------------------------------ keys


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """What the API/UI may see: never the hash, never the plaintext."""
    return {
        "id": record.get("id"),
        "label": record.get("label") or "",
        "prefix": record.get("prefix") or "",
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "revoked_at": record.get("revoked_at"),
    }


def is_active(record: dict[str, Any], now: datetime | None = None) -> bool:
    """Usable = not revoked AND not expired."""
    if record.get("revoked_at"):
        return False
    expires = record.get("expires_at")
    if expires:
        try:
            deadline = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError:
            return False
        return deadline > (now or _now_dt())
    return True


def keys(identity: str | None) -> list[dict[str, Any]]:
    """Public view of every key this identity ever minted (active or not)."""
    if not identity:
        return []
    records = load(identity).get("keys") or []
    return [_public(r) for r in records if isinstance(r, dict)]


def add_key(
    identity: str, key: str, *, label: str = "", expires_at: str | None = None
) -> dict[str, Any]:
    """Store the HASH of a freshly minted key; return its public view."""
    record = {
        "id": "k_" + secrets.token_hex(4),
        "label": (label or "").strip()[:60],
        "sha256": hashlib.sha256(key.encode()).hexdigest(),
        "prefix": key[:14],
        "created_at": _now(),
        "expires_at": expires_at or None,
        "revoked_at": None,
    }
    doc = load(identity)
    doc["keys"] = list(doc.get("keys") or []) + [record]
    save(identity, doc)
    return _public(record)


def revoke_key(identity: str, key_id: str) -> bool:
    """Permanently revoke one key. The record stays for the audit trail."""
    doc = load(identity)
    changed = False
    for record in doc.get("keys") or []:
        if isinstance(record, dict) and record.get("id") == key_id and not record.get("revoked_at"):
            record["revoked_at"] = _now()
            changed = True
    if changed:
        save(identity, doc)
    return changed


def rename_key(identity: str, key_id: str, label: str) -> bool:
    doc = load(identity)
    changed = False
    for record in doc.get("keys") or []:
        if isinstance(record, dict) and record.get("id") == key_id:
            record["label"] = (label or "").strip()[:60]
            changed = True
    if changed:
        save(identity, doc)
    return changed


def iter_identities() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every identity with a state file (used to resolve minted keys)."""
    directory = state_dir()
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return
    for entry in entries:
        try:
            doc = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            migrated, changed = _migrate(doc)
            if changed:
                try:
                    save(entry.stem, migrated)
                except OSError:
                    pass
            yield entry.stem, migrated
