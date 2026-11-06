"""Who may see which skills — one small file, one decision.

The catalogue is no longer global: what a caller sees is computed from
identity -> role -> source paths. This module owns that mapping; it knows
nothing about HTTP, MCP or the UI.

Policy file (SKILLS_POLICY_FILE, default /etc/skills/policy/access.yaml):

    default:
      paths: [skills]
    roles:
      team:
        members: [bryan, alice]
        paths: [skills]
      demo:
        members: [bryan]
        paths: [skills, skills-demo]

Failure direction, deliberately:
  * file ABSENT          -> no policy at all -> no filtering (not configured)
  * file PRESENT + broken -> DENY EVERYTHING (a visibility control must never
                             fail open) + the error is surfaced in /admin/status

Identity matching is case-insensitive and also matches the local part of an
email, so "bryan" in members matches "bryan@corp" as an identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ENV_POLICY_FILE = "SKILLS_POLICY_FILE"
DEFAULT_POLICY_FILE = "/etc/skills/policy/access.yaml"


class PolicyError(ValueError):
    """The policy file exists but cannot be used."""


@dataclass(frozen=True)
class Role:
    name: str
    paths: tuple[str, ...]
    members: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    default_paths: tuple[str, ...] = ()
    roles: tuple[Role, ...] = ()
    #: Set when the file was present but unusable — the caller may surface it.
    error: str | None = None

    @staticmethod
    def deny_all(error: str) -> "Policy":
        return Policy(default_paths=(), roles=(), error=error)

    def _keys(self, identity: str) -> set[str]:
        value = identity.strip().lower()
        keys = {value}
        if "@" in value:
            keys.add(value.split("@", 1)[0])
        return keys

    def role_for(self, identity: str | None) -> Role | None:
        if not identity:
            return None
        keys = self._keys(identity)
        for role in self.roles:
            if keys & set(role.members):
                return role
        return None

    def paths_for(self, identity: str | None) -> tuple[str, ...]:
        role = self.role_for(identity)
        return role.paths if role else self.default_paths


def policy_file() -> Path:
    return Path(os.getenv(ENV_POLICY_FILE, "").strip() or DEFAULT_POLICY_FILE)


def parse_policy(text: str) -> Policy:
    """Parse the YAML body. Raises PolicyError on anything unusable."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid YAML: {str(exc).splitlines()[0]}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a mapping with 'default' and/or 'roles'")

    default = raw.get("default") or {}
    if not isinstance(default, dict):
        raise PolicyError("'default' must be a mapping")
    default_paths = _paths(default.get("paths"), "default")

    roles: list[Role] = []
    raw_roles = raw.get("roles") or {}
    if not isinstance(raw_roles, dict):
        raise PolicyError("'roles' must be a mapping of name -> {members, paths}")
    for name, body in raw_roles.items():
        if not isinstance(body, dict):
            raise PolicyError(f"role {name!r} must be a mapping")
        members = body.get("members") or []
        if not isinstance(members, list):
            raise PolicyError(f"role {name!r}: 'members' must be a list")
        roles.append(
            Role(
                name=str(name),
                paths=_paths(body.get("paths"), f"role {name!r}"),
                members=tuple(str(m).strip().lower() for m in members if str(m).strip()),
            )
        )
    return Policy(default_paths=default_paths, roles=tuple(roles))


def _paths(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PolicyError(f"{where}: 'paths' must be a list")
    return tuple(str(v).strip() for v in value if str(v).strip())


_cache: tuple[str, float, int, Policy | None] | None = None


def current_policy() -> Policy | None:
    """The active policy, re-read when the file changes. None = not configured."""
    global _cache
    path = policy_file()
    try:
        stat = path.stat()
    except OSError:
        _cache = None
        return None
    key = (str(path), stat.st_mtime, stat.st_size)
    if _cache is not None and _cache[:3] == key:
        return _cache[3]
    try:
        loaded = parse_policy(path.read_text(encoding="utf-8"))
    except (OSError, PolicyError) as exc:
        loaded = Policy.deny_all(f"{path}: {exc}")
    _cache = (*key, loaded)
    return loaded
