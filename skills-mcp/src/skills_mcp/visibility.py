"""The ONE place that decides what a caller may see.

Both surfaces — the MCP tools and the browser UI — call these functions, so the
page can never show a skill the agent would not get (or vice versa).

Layering: catalog -> state -> policy -> visibility -> {server, ui}.
"""

from __future__ import annotations

from . import policy as policy_mod
from . import state as state_mod
from .catalog import Skill


def visible_skills(identity: str | None) -> list[Skill]:
    """Every skill this identity may see, in catalogue order.

    No policy configured -> everything (backwards compatible).
    Policy configured    -> only the source paths its role allows.
    """
    state = state_mod.current_state()
    catalog = state.catalog
    active = policy_mod.current_policy()
    names = catalog.names()
    if active is None:
        return [catalog.skills[name] for name in names]
    allowed = set(active.paths_for(identity))
    return [catalog.skills[name] for name in names if catalog.skills[name].source_path in allowed]


def visible_names(identity: str | None) -> list[str]:
    return [skill.name for skill in visible_skills(identity)]


def get_visible(name: str, identity: str | None) -> Skill | None:
    """One skill, but only if this identity may see it."""
    for skill in visible_skills(identity):
        if skill.name == name:
            return skill
    return None


def role_label(identity: str | None) -> str | None:
    """The role name for display ("team", "demo", "default"), or None when no
    policy is configured."""
    active = policy_mod.current_policy()
    if active is None:
        return None
    role = active.role_for(identity)
    return role.name if role else "default"


def policy_error() -> str | None:
    active = policy_mod.current_policy()
    return active.error if active is not None else None
