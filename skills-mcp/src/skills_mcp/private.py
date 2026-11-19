"""Private skills — a person's own tier, on the PVC.

Layout:  SKILLS_PRIVATE_DIR (default /data/private) / <identity> / <skill> / SKILL.md

Same contract as the git tier (name == folder, YAML frontmatter, no backups),
because it goes through the SAME loader. The only difference is where it lives
and who may see it.

Names are unique WITHIN this tier (one folder per name). A private skill MAY
reuse a global skill's name: the two sit side by side, both are listed, and the
caller picks by description then fetches by id. Nothing is hidden.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import catalog as catalog_mod
from .catalog import Skill

ENV_PRIVATE_DIR = "SKILLS_PRIVATE_DIR"
DEFAULT_PRIVATE_DIR = "/data/private"


def private_dir() -> Path:
    return Path(os.getenv(ENV_PRIVATE_DIR, "").strip() or DEFAULT_PRIVATE_DIR)


def root_for(identity: str) -> Path:
    from .prefs import safe_identity

    return private_dir() / safe_identity(identity)


_cache: dict[str, tuple[float, tuple[Skill, ...], tuple[str, ...]]] = {}


def load(identity: str | None) -> tuple[tuple[Skill, ...], tuple[str, ...]]:
    """(skills, warnings) for one identity. Empty for anonymous callers."""
    if not identity:
        return (), ()
    directory = root_for(identity)
    try:
        stamp = max((p.stat().st_mtime for p in directory.glob("*/SKILL.md")), default=0.0)
    except OSError:
        return (), ()
    cached = _cache.get(identity)
    if cached is not None and cached[0] == stamp:
        return cached[1], cached[2]

    skills: dict[str, Skill] = {}
    warnings: list[str] = []
    try:
        children = sorted(directory.iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / catalog_mod.SKILL_FILE).is_file():
            continue
        try:
            skill = catalog_mod.load_skill(child, source_path="private")
        except catalog_mod.SkillError as exc:
            warnings.append(f"private/{identity}/{child.name}: {exc} (skipped)")
            continue
        if skill.name in skills:
            warnings.append(f"private/{identity}/{child.name}: duplicate {skill.name!r} (skipped)")
            continue
        skills[skill.name] = skill

    ordered = tuple(skills[name] for name in sorted(skills))
    _cache[identity] = (stamp, ordered, tuple(warnings))
    return ordered, tuple(warnings)


# ------------------------------------------------------------------ writing

#: Folder-safe, lowercase: what the loader requires (name == folder).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_BODY = 64 * 1024


class PrivateError(ValueError):
    """A rejected write. The message is shown to the user verbatim."""


def skill_path(identity: str, name: str) -> Path:
    return root_for(identity) / name / catalog_mod.SKILL_FILE


def invalidate(identity: str) -> None:
    """Drop the cached view so the next read sees the write."""
    _cache.pop(identity, None)


def read(identity: str, name: str) -> dict[str, str] | None:
    """The editor's view of one private skill: {name, description, body}."""
    path = skill_path(identity, name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    front, body = _split(text)
    return {"name": name, "description": front.get("description", ""), "body": body}


def _split(text: str) -> tuple[dict[str, str], str]:
    """(frontmatter, body) — a tiny reader so the editor round-trips cleanly."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    front: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, _, value = line.partition(":")
        if key.strip() and value.strip():
            front[key.strip()] = value.strip().strip('"').strip("'")
    return front, parts[2].lstrip("\n")


def validate(name: str, description: str, body: str, forbidden: set[str]) -> tuple[str, str, str]:
    """Normalise + check. Raises PrivateError with a user-facing message."""
    name = (name or "").strip().lower()
    description = " ".join((description or "").split())
    body = (body or "").strip()
    if not NAME_RE.match(name):
        raise PrivateError("name must be lowercase letters, digits and dashes (max 63), e.g. my-runbook")
    if name in forbidden:
        raise PrivateError(f"the name {name!r} is already taken in this tier — pick another")
    if not description:
        raise PrivateError("description is required (one line: when should an agent use this?)")
    if len(description) > 400:
        raise PrivateError(f"description is {len(description)} chars; keep it under 400")
    if not body:
        raise PrivateError("body is required")
    if len(body.encode()) > MAX_BODY:
        raise PrivateError(f"body is larger than {MAX_BODY // 1024} KB")
    return name, description, body


def save(identity: str, name: str, description: str, body: str, *, forbidden: set[str]) -> str:
    """Create or overwrite one private skill. Returns the normalised name."""
    name, description, body = validate(name, description, body, forbidden)
    path = skill_path(identity, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    # Quote both scalars: an unquoted `description: 12` becomes a YAML int and
    # would be read back as "no description".
    temporary.write_text(
        f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    invalidate(identity)
    return name


def delete(identity: str, name: str) -> bool:
    """Remove a private skill (its folder, if it is now empty)."""
    directory = root_for(identity) / name
    path = directory / catalog_mod.SKILL_FILE
    try:
        path.unlink()
    except OSError:
        return False
    try:
        directory.rmdir()
    except OSError:
        pass
    invalidate(identity)
    return True
