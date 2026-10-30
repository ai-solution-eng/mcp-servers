"""Served state: what the tools read, and where it came from.

Everything the tools consult lives in ONE frozen State object. A refresh builds
a new State and assigns it once (set_state) — no reader can observe a
half-updated catalogue, and no lock is needed.

Boot order (first valid source wins):
  1. SKILLS_SOURCE_DIR      local dev: a checkout of the tools repo
  2. the working copy       <SKILLS_ROOT>/skills-current (a symlink a refresh
                            swapped in) — survives a restart when /data is a PVC
  3. the bundled snapshot   <SKILLS_BUNDLED_ROOT>, baked into the image
  4. nothing                an empty catalogue plus a warning (never a crash)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog
from . import catalog as catalog_mod

ENV_SOURCE_PATHS = "SKILLS_SOURCE_PATHS"
ENV_SOURCE_DIR = "SKILLS_SOURCE_DIR"
ENV_BUNDLED_ROOT = "SKILLS_BUNDLED_ROOT"

DEFAULT_SOURCE_PATHS = "skills"
DEFAULT_BUNDLED_ROOT = "/opt/skills-bundled"


@dataclass(frozen=True)
class Source:
    """Where the served catalogue came from — echoed in every tool response so
    a caller can tell a bundled snapshot from a fresh GitHub pull."""

    origin: str  # "local" | "bundled" | "github" | "none"
    ref: str | None = None
    commit: str | None = None
    refreshed_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "origin": self.origin,
            "ref": self.ref,
            "commit": self.commit,
            "refreshed_at": self.refreshed_at,
        }


@dataclass(frozen=True)
class State:
    catalog: Catalog
    source: Source
    directory: Path | None = None


_state: State | None = None


def source_paths() -> list[str]:
    """The folders inside a source tree to read (skills / skills,skills-demo)."""
    raw = os.getenv(ENV_SOURCE_PATHS, DEFAULT_SOURCE_PATHS)
    return [part.strip() for part in raw.split(",") if part.strip()]


def bundled_root() -> Path:
    return Path(os.getenv(ENV_BUNDLED_ROOT, DEFAULT_BUNDLED_ROOT))


def load_state(directory: Path | str, source: Source) -> State:
    """Load a catalogue from one directory (a checkout, a release, a bundle)."""
    directory = Path(directory)
    return State(
        catalog=catalog_mod.load_catalog(directory, source_paths()),
        source=source,
        directory=directory,
    )


def current_state() -> State:
    """The catalogue the tools serve (bootstrapped on first use)."""
    global _state
    if _state is None:
        _state = initial_state()
    return _state


def set_state(state: State) -> None:
    """Replace the served catalogue. One assignment = atomic for every reader."""
    global _state
    _state = state


def reset_state() -> None:
    """Forget the served state so the next read re-bootstraps (tests)."""
    global _state
    _state = None


def initial_state() -> State:
    """Resolve the boot source (see the module docstring for the order)."""
    local = os.getenv(ENV_SOURCE_DIR, "").strip()
    if local:
        return load_state(Path(local), Source(origin="local"))

    # Local import: refresh imports state, so importing it at module load would
    # be circular. By boot time both modules are fully initialised.
    from . import refresh

    working = refresh.ensure_working_copy()
    if working is not None:
        return working

    return State(
        catalog=Catalog(
            warnings=[
                "no skills source configured: set SKILLS_SOURCE_DIR (local dev), "
                "bake a snapshot at " + str(bundled_root()) + ", or set SKILLS_SOURCE_URL"
            ]
        ),
        source=Source(origin="none"),
    )
