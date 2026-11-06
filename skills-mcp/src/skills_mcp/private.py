"""Private skills — a person's own tier, on the PVC.

Layout:  SKILLS_PRIVATE_DIR (default /data/private) / <identity> / <skill> / SKILL.md

Same contract as the git tier (name == folder, YAML frontmatter, no backups),
because it goes through the SAME loader. The only difference is where it lives
and who may see it. Private skills shadow a global skill of the same name for
their owner only — useful for trying a variant without touching the repo.
"""

from __future__ import annotations

import os
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
