"""Load, validate and index skill folders.

A SKILL is a directory directly under a configured source path that contains a
``SKILL.md`` with YAML frontmatter::

    ---
    name: cluster-triage
    description: Use when something is wrong in the cluster ...
    ---
    <markdown body>

Nothing else counts. No recursion, no ``*.bak*`` files, no ``__pycache__``:
a backup file carrying the same `name:` as its live sibling would otherwise
serve LAST WEEK's instructions under today's name, silently.

Two rules this module exists to enforce:

1. **Strict contract, loud failures.** A skill that does not parse is SKIPPED
   and recorded as a warning — never half-loaded, never a crash. The warnings
   surface in /admin/status so a broken file is visible, not invisible.
2. **Cheap catalogue.** Loading a skill reads its SKILL.md once and keeps the
   body in memory (the whole corpus is ~200 KB). `files` is names only — file
   CONTENT is read on demand by `get_skill_file`, never here.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import yaml

SKILL_FILE = "SKILL.md"

# lower-kebab-case, the same shape as the folder name. Anchored so "a--b",
# "a-", "-a" and "a_b" are all rejected.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Clients truncate descriptions around 500 chars — and every author puts the
# "do NOT use this for X" hint at the END, so a long description loses exactly
# the routing information that makes it useful. Warn well before the cliff.
DESCRIPTION_WARN_CHARS = 400

# Ignored inside a skill directory: editor/backup droppings and bytecode.
IGNORED_DIRS = frozenset({"__pycache__"})
_IGNORED_FILE_RE = re.compile(r"(\.bak|\.pyc$)")

# ``---`` line, frontmatter, closing ``---`` line, at the very start of the file.
# Tolerates CRLF and a UTF-8 BOM.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


class SkillError(ValueError):
    """A skill directory that cannot be loaded. Always caught by the loader."""


@dataclass(frozen=True)
class Skill:
    """One loaded skill. ``body`` is the markdown with frontmatter stripped."""

    name: str
    description: str
    body: str
    directory: Path
    #: Files inside the skill directory (POSIX relative paths), SKILL.md excluded.
    files: tuple[str, ...] = ()
    #: Non-fatal problems found while loading THIS skill (e.g. long description).
    warnings: tuple[str, ...] = ()

    def catalogue_row(self) -> dict:
        """The cheap row ``list_skills`` returns — name, description, has_files.
        Never the body: the catalogue is loaded on every agent turn."""
        return {
            "name": self.name,
            "description": self.description,
            "has_files": bool(self.files),
        }


@dataclass
class Catalog:
    """The loaded corpus: skills by name + everything that went wrong."""

    skills: dict[str, Skill] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Directories under a source path that had no SKILL.md (assets, .git, ...).
    ignored: int = 0

    def names(self) -> list[str]:
        return sorted(self.skills)

    def __len__(self) -> int:
        return len(self.skills)

    def __contains__(self, name: object) -> bool:
        return name in self.skills

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``SKILL.md`` text into (frontmatter mapping, body).

    Raises SkillError when the file has no frontmatter, the YAML is invalid, or
    the YAML is not a mapping. The body is everything after the closing ``---``,
    trimmed.
    """
    text = text.lstrip("\ufeff")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillError(
            "no YAML frontmatter (the file must start with a '---' line and "
            "close it with another '---')"
        )
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        # First line only: PyYAML's message spans several lines with a caret
        # diagram, which is noise in a status payload.
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise SkillError(f"invalid YAML frontmatter: {detail}") from exc
    if not isinstance(meta, dict):
        raise SkillError("frontmatter is not a YAML mapping (expected 'name:' and 'description:')")
    return meta, text[match.end():].strip()


def is_ignored_file(name: str) -> bool:
    """True for backup/bytecode files that must never be served or listed."""
    return bool(_IGNORED_FILE_RE.search(name))


def _skill_files(directory: Path) -> tuple[str, ...]:
    """Every file inside the skill directory except SKILL.md, backups, and
    bytecode — POSIX relative paths, sorted (deterministic output)."""
    found: list[str] = []
    for entry in sorted(directory.rglob("*")):
        rel = entry.relative_to(directory)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        if not entry.is_file():
            continue
        if entry.name == SKILL_FILE:
            continue
        if is_ignored_file(entry.name):
            continue
        found.append(rel.as_posix())
    return tuple(found)


def load_skill(directory: Path) -> Skill:
    """Load ONE skill directory. Raises SkillError when it is not a valid skill."""
    skill_md = directory / SKILL_FILE
    if not skill_md.is_file():
        raise SkillError(f"no {SKILL_FILE}")
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"cannot read {SKILL_FILE}: {exc}") from exc

    meta, body = split_frontmatter(text)

    raw_name = meta.get("name")
    raw_description = meta.get("description")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise SkillError("frontmatter has no 'name'")
    if not isinstance(raw_description, str) or not raw_description.strip():
        raise SkillError("frontmatter has no 'description'")

    name = raw_name.strip()
    # YAML folds multi-line scalars to single spaces already; collapse any
    # stray runs so the catalogue is byte-stable across formatting.
    description = " ".join(raw_description.split())

    if not NAME_RE.match(name):
        raise SkillError(f"invalid name {name!r} (must be lower-kebab-case: a-z, 0-9, single hyphens)")
    if name != directory.name:
        raise SkillError(f"name {name!r} does not match folder {directory.name!r}")

    warnings: list[str] = []
    if len(description) > DESCRIPTION_WARN_CHARS:
        warnings.append(
            f"description is {len(description)} chars (> {DESCRIPTION_WARN_CHARS}): clients "
            "truncate near 500 and the 'do NOT use this for X' hint at the end is what gets cut"
        )

    return Skill(
        name=name,
        description=description,
        body=body,
        directory=directory,
        files=_skill_files(directory),
        warnings=tuple(warnings),
    )


def load_catalog(root: Path | str, source_paths: Sequence[str] = ("skills",)) -> Catalog:
    """Load every skill under ``root/<source path>/<skill dir>/SKILL.md``.

    ``root`` is the checkout (or the extracted tarball, or the working copy);
    ``source_paths`` are the folders inside it to read — the demo/team switch.
    A source path that does not exist is a WARNING, not an error: one bad path
    must not blank the catalogue.
    """
    root = Path(root)
    catalog = Catalog()

    for source in source_paths:
        base = root / source
        if not base.is_dir():
            catalog.warnings.append(f"{source}: source path not found under {root}")
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / SKILL_FILE).is_file():
                catalog.ignored += 1
                continue
            try:
                skill = load_skill(child)
            except SkillError as exc:
                catalog.warnings.append(f"{source}/{child.name}: {exc} (skipped)")
                continue
            if skill.name in catalog.skills:
                first = catalog.skills[skill.name].directory
                catalog.warnings.append(
                    f"{source}/{child.name}: duplicate skill name {skill.name!r} "
                    f"(already loaded from {first}) — skipped"
                )
                continue
            catalog.skills[skill.name] = skill
            catalog.warnings.extend(f"{source}/{child.name}: {warning}" for warning in skill.warnings)

    return catalog


def main(argv: Sequence[str] | None = None) -> int:
    """Dev CLI: ``python -m skills_mcp.catalog <root> [--paths skills,skills-demo]``."""
    parser = argparse.ArgumentParser(
        prog="python -m skills_mcp.catalog",
        description="Print the skill catalogue for a checkout (dev tool, no server needed).",
    )
    parser.add_argument("root", nargs="?", default=".", help="directory containing the source folders")
    parser.add_argument("--paths", default="skills", help="comma-separated source paths (default: skills)")
    args = parser.parse_args(argv)

    sources = [part.strip() for part in args.paths.split(",") if part.strip()]
    catalog = load_catalog(Path(args.root), sources)

    print(f"{len(catalog)} skills from {Path(args.root)} [{', '.join(sources)}]")
    width = max((len(name) for name in catalog.names()), default=0)
    for name in catalog.names():
        skill = catalog.skills[name]
        print(f"  {name:<{width}}  {len(skill.description):>4}ch  files={len(skill.files)}")
    if catalog.ignored:
        print(f"  ({catalog.ignored} directories without {SKILL_FILE} ignored)")
    if catalog.warnings:
        print(f"\n{len(catalog.warnings)} warning(s):")
        for warning in catalog.warnings:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
