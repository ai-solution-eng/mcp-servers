"""Drift guard: requirements-deps.txt must equal pyproject's dep set.

The manifest is the Dockerfile dep-layer's cache key (v3 — see the Dockerfile
comment block): if it diverges from pyproject.toml ([project.dependencies] +
the [iceberg] extra), the wheel layer installs something the local package
doesn't declare (or misses one it does) — the import-gate layer catches the
miss case at build time, but the EXTRA case (unused wheels shipped) has no
other guard. Regenerate with ./bump_version.sh <version>.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_dep_set() -> set[str]:
    pp = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = list(pp["project"]["dependencies"])
    deps += pp["project"].get("optional-dependencies", {}).get("iceberg", [])
    return {d.strip() for d in deps if d.strip()}


def _manifest_dep_set() -> set[str]:
    lines = (ROOT / "requirements-deps.txt").read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def test_manifest_matches_pyproject():
    assert _manifest_dep_set() == _pyproject_dep_set()


def test_manifest_has_no_package_version():
    """The manifest must NOT carry the package's own version line — a version
    bump rewrites pyproject, and a version in the manifest would bust the
    wheel layer on every release (the v2 behavior, live-seen)."""
    text = (ROOT / "requirements-deps.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        assert not re.match(r"^sqlhandler[=<>!~ ]", line.strip(), re.IGNORECASE), line


def test_dockerfile_installs_from_manifest():
    """The dep layer must install from the FROZEN lock (v4) — a floor-range
    install would let a rebuild float dependency versions (the DuckDB the
    filesystem-lockdown SETs depend on included)."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements-deps.lock.txt ./" in dockerfile
    assert "-r requirements-deps.lock.txt" in dockerfile


def _lock_pins() -> dict[str, str]:
    """name -> pinned version from requirements-deps.lock.txt (== lines only)."""
    pins: dict[str, str] = {}
    for line in (ROOT / "requirements-deps.lock.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "# via")):
            continue
        if stripped.startswith(("#", "-")) or "==" not in stripped:
            continue
        name, _, version = stripped.partition("==")
        name = name.split(";")[0].strip()
        version = version.split(";")[0].strip()
        if name and version:
            pins[name.lower().replace("_", "-")] = version
    return pins


def test_lock_is_frozen_to_pyproject_deps():
    """Every pyproject floor dep must appear in the lock with a == pin, and
    its pinned version must satisfy the floor (a regenerated-but-stale lock
    or a dep added without re-locking fails here, at test time, not as a
    silently-floated runtime version months later)."""
    import packaging.specifiers  # dev-dep-free: setuptools/pip ship it; fall back below

    pins = _lock_pins()
    for dep in sorted(_pyproject_dep_set()):
        req = dep.split(";")[0].strip()
        name = re.split(r"[<>=!~\[ ]", req, 1)[0].strip()
        key = name.lower().replace("_", "-")
        assert key in pins, f"{name} missing from requirements-deps.lock.txt — regenerate with uv export"
        if "[" in name:
            continue
        floor_match = re.search(r"(>=|==|~=)\s*([0-9][^,;\s]*)", req)
        if floor_match:
            spec = f"{floor_match.group(1)}{floor_match.group(2)}"
            assert packaging.specifiers.SpecifierSet(spec).contains(pins[key]), (
                f"lock pins {name}=={pins[key]} which violates pyproject floor {spec}"
            )
