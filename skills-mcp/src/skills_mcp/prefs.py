"""Per-identity state on the PVC — the only mutable thing this server owns.

One JSON file per identity under SKILLS_STATE_DIR (default /data/state):

    {
      "disabled": ["memory-curation"],            # global skills this person turned off
      "token": {"sha256": "…", "prefix": "sk_live_9f3a", "created_at": "…"}
    }

Why a file and not env: a minted token appears at runtime, env is frozen at
container start. Why hashed: we only ever verify ("does sha256(presented)
match?"), never recall, so a leaked file yields nothing usable.

Writes are atomic (temp + os.replace) and serialised by a per-path lock, so a
crash can never leave a half-written JSON file behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


def load(identity: str) -> dict[str, Any]:
    """This identity's state, or an empty document. Never raises on bad JSON."""
    try:
        raw = json.loads(_path_for(identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


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


# ------------------------------------------------------------------ tokens


def token_record(identity: str | None) -> dict[str, Any] | None:
    if not identity:
        return None
    token = load(identity).get("token")
    return token if isinstance(token, dict) else None


def set_token(identity: str, key: str) -> dict[str, Any]:
    """Store the HASH of a freshly minted key; return the public view."""
    record = {
        "sha256": hashlib.sha256(key.encode()).hexdigest(),
        "prefix": key[:14],
        "created_at": _now(),
    }
    doc = load(identity)
    doc["token"] = record
    save(identity, doc)
    return record


def clear_token(identity: str) -> bool:
    doc = load(identity)
    if "token" not in doc:
        return False
    doc.pop("token")
    save(identity, doc)
    return True


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
            yield entry.stem, doc
