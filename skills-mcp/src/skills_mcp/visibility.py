"""The ONE place that decides what a caller may see.

    visible = every global skill (git)
              - the ones this person disabled
              + their private skills (which shadow a global of the same name)

Both surfaces — the MCP tools and the browser UI — call these functions, so the
page can never show a skill the agent would not get.
"""

from __future__ import annotations

from . import prefs
from . import private as private_mod
from . import state as state_mod
from .catalog import Skill


def visible_skills(identity: str | None) -> list[Skill]:
    catalog = state_mod.current_state().catalog
    hidden = prefs.disabled(identity)
    merged: dict[str, Skill] = {
        name: catalog.skills[name] for name in catalog.names() if name not in hidden
    }
    private_skills, _warnings = private_mod.load(identity)
    for skill in private_skills:
        merged[skill.name] = skill          # private shadows global, for its owner
    return [merged[name] for name in sorted(merged)]


def visible_names(identity: str | None) -> list[str]:
    return [skill.name for skill in visible_skills(identity)]


def get_visible(name: str, identity: str | None) -> Skill | None:
    for skill in visible_skills(identity):
        if skill.name == name:
            return skill
    return None


def private_skills(identity: str | None) -> list[Skill]:
    skills, _warnings = private_mod.load(identity)
    return list(skills)


def private_warnings(identity: str | None) -> list[str]:
    _skills, warnings = private_mod.load(identity)
    return list(warnings)
