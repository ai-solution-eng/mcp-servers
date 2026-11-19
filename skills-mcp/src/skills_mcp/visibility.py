"""The ONE place that decides what a caller may see.

    visible = every global skill (git)
              - the ones this person disabled (by ID — so one tier can be hidden
                while the other copy of the same name stays visible)
              + their private skills

A name may appear TWICE — the team's copy and yours — and that is deliberate:
nothing is hidden, the caller gets both and picks by description, then fetches
by id. There is no shadowing.

Both surfaces — the MCP tools and the browser UI — call these functions, so the
page can never show a skill the agent would not get.
"""

from __future__ import annotations

from . import prefs
from . import private as private_mod
from . import state as state_mod
from .catalog import Skill


def all_skills(identity: str | None) -> list[Skill]:
    """Every skill this person COULD see, hidden ones included."""
    catalog = state_mod.current_state().catalog
    globals_ = [catalog.skills[name] for name in catalog.names()]
    private_skills, _warnings = private_mod.load(identity)
    # Team copy first, then the caller's own: a human reading the list sees the
    # shared one first. Sorted by name, then tier.
    return sorted(globals_, key=lambda s: s.name) + sorted(private_skills, key=lambda s: s.name)


def visible_skills(identity: str | None) -> list[Skill]:
    hidden = prefs.disabled(identity)
    return [
        skill for skill in all_skills(identity) if skill_id(skill, identity) not in hidden
    ]


def skill_id(skill: Skill, identity: str | None = None) -> str:
    """A stable handle for one skill.

    Names are display labels and may collide ACROSS tiers (a global and your
    private copy); ids never do, because they carry the tier (and the owner).
    `github:<name>` / `private:<identity>:<name>`.
    """
    if skill.source_path == "private":
        return f"private:{identity or ''}:{skill.name}"
    return f"github:{skill.name}"


def resolve(
    key: str, identity: str | None, *, include_hidden: bool = False
) -> tuple[str, list[Skill]]:
    """Look a skill up by id OR by name.

    Returns ("one", [skill]) | ("ambiguous", [skill, ...]) | ("none", []).

    A name can legitimately match two skills — a global one and the caller's
    private one. That is NOT an error: the caller gets both and picks by
    description (the same way it picks from list_skills), then fetches by id.
    """
    wanted = (key or "").strip()
    visible = all_skills(identity) if include_hidden else visible_skills(identity)

    if wanted.startswith("private:"):
        parts = wanted.split(":", 2)
        if len(parts) != 3 or parts[1] != (identity or ""):
            return "none", []          # another person's private id is invisible
        name = parts[2]
        return "one", [s for s in visible if s.source_path == "private" and s.name == name]

    if wanted.startswith("github:"):
        name = wanted.split(":", 1)[1]
        return "one", [s for s in visible if s.source_path != "private" and s.name == name]

    matches = [s for s in visible if s.name == wanted]
    if len(matches) == 1:
        return "one", matches
    if len(matches) > 1:
        return "ambiguous", matches
    return "none", []


def visible_names(identity: str | None) -> list[str]:
    """Unique names (a duplicated name is listed once)."""
    return sorted({skill.name for skill in visible_skills(identity)})


def get_visible(name: str, identity: str | None) -> Skill | None:
    """The FIRST match by name (the team's copy). Ambiguity-aware callers use
    resolve() instead."""
    for skill in visible_skills(identity):
        if skill.name == name:
            return skill
    return None


def is_duplicated(name: str, identity: str | None) -> bool:
    return sum(1 for skill in visible_skills(identity) if skill.name == name) > 1


def private_skills(identity: str | None) -> list[Skill]:
    skills, _warnings = private_mod.load(identity)
    return list(skills)


def private_warnings(identity: str | None) -> list[str]:
    _skills, warnings = private_mod.load(identity)
    return list(warnings)
