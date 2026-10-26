"""Packaging regression tests — the Wave-6 deploy-blocker class (mcp_auth crash).

Root cause being pinned: the server image is built with a NON-editable
``uv pip install --system .`` and runs the installed ``logsearch-mcp``
console script (= ``server:main``) from site-packages. Any top-level module
that ``server.py`` imports but that the wheel does not ship makes the
container crash-loop at startup with ModuleNotFoundError. TWO modules hit
exactly this: ``mcp_auth`` (auth wave) and ``mcp_metrics`` (Wave-3 C3 —
which was ALSO missing from the Dockerfile COPY line, found and fixed in
the same pass). The live images predate those waves, which is why the
crash was never seen until a rebuild.

Three layers must agree, and each has a test:

1. ``pyproject.toml`` declares every runtime-imported local module under
   ``[tool.setuptools] py-modules`` (plus an AST cross-check of
   server.py/webui.py imports so the list cannot silently drift again).
2. The Dockerfile COPYs every one of those files into the build context
   (and the existing .dockerignore excludes none of them).
3. (skipif no local build backend) a wheel built from the tree with
   ``pip wheel --no-deps --no-build-isolation`` actually CONTAINS them all.

Hermetic and offline: no network (build isolation off, --no-deps), no
import of server.py, and the wheel build runs on a tmp COPY of the build
inputs so the source tree stays clean (no build/ residue in git status).

Known environment split (fleet unit-test convention): this suite normally
runs under ``SQLhandler/.venv312`` (py3.12, no setuptools) where the wheel
test SKIPs; the packaging proof itself is exercised out-of-suite and under
the conda ML14 interpreter where setuptools IS present.
"""

import ast
import importlib.util
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
PYPROJECT = APP_DIR / "pyproject.toml"
DOCKERFILE = APP_DIR / "Dockerfile"

# What server.py imports at runtime (verified 2026-09-13): mcp_auth (auth
# wave) + mcp_metrics (Wave-3 C3, default-off /metrics).
REQUIRED_MODULES = ["server", "webui", "mcp_auth", "mcp_metrics"]
REQUIRED_PACKAGES: list[str] = []

# Build-context files/dirs the Dockerfile must COPY (pyproject + every
# declared module; MANIFEST.in already COPY'd for source installs).
REQUIRED_COPY_ENTRIES = [
    "pyproject.toml",
    "server.py",
    "webui.py",
    "mcp_auth.py",
    "mcp_metrics.py",
]


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _setuptools_config() -> dict:
    return _pyproject().get("tool", {}).get("setuptools", {})


def _copy_sources() -> set[str]:
    """All source paths named on COPY lines in the Dockerfile (trailing
    slashes normalized so directory COPYs compare cleanly)."""
    sources: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY ") and not stripped.startswith("COPY --"):
            parts = stripped.split()
            # COPY <src...> <dest> — last token is the destination.
            sources.update(p.rstrip("/") for p in parts[1:-1])
    return sources


def _local_top_level_imports(py_file: Path) -> set[str]:
    """Top-level module names imported by a source file (AST, no import)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    # Keep only names that resolve to a local file/package in the app dir.
    return {n for n in names if (APP_DIR / f"{n}.py").is_file() or (APP_DIR / n / "__init__.py").is_file()}


def _build_backend_available() -> bool:
    """The wheel-content test needs setuptools (the declared backend) + pip."""
    return importlib.util.find_spec("setuptools") is not None and (importlib.util.find_spec("pip") is not None)


# ---------------------------------------------------------------- layer 1


def test_pyproject_declares_all_required_modules() -> None:
    declared = set(_setuptools_config().get("py-modules", []))
    missing = [m for m in REQUIRED_MODULES if m not in declared]
    assert not missing, (
        f"pyproject [tool.setuptools] py-modules is missing {missing}; the "
        "non-editable image install would crash-loop at startup "
        "(ModuleNotFoundError) because server.py imports them"
    )


def test_pyproject_modules_cover_all_local_imports() -> None:
    """AST drift-guard: every local top-level import of server.py/webui.py
    must be declared. New local import without a py-modules update = the
    next image build crash-loops."""
    declared = set(_setuptools_config().get("py-modules", [])) | set(_setuptools_config().get("packages", []))
    imported: set[str] = set()
    for src in ("server.py", "webui.py"):
        path = APP_DIR / src
        if path.is_file():
            imported |= _local_top_level_imports(path)
    undeclared = imported - declared
    assert not undeclared, (
        f"server.py/webui.py import local modules {sorted(undeclared)} that "
        f"pyproject does not declare (declared: {sorted(declared)})"
    )


def test_console_script_entry_point_pins_server_main() -> None:
    """The image CMD runs the installed console script = server:main — the
    whole crash class exists because THAT import must resolve from
    site-packages. Pin the mapping so it cannot be renamed silently."""
    scripts = _pyproject().get("project", {}).get("scripts", {})
    assert scripts.get("logsearch-mcp") == "server:main"


# ---------------------------------------------------------------- layer 2


def test_dockerfile_copies_all_build_context_files() -> None:
    sources = _copy_sources()
    missing = [e for e in REQUIRED_COPY_ENTRIES if e not in sources]
    assert not missing, (
        f"Dockerfile COPY lines do not bring {missing} into the build "
        "context; `uv pip install --system .` builds the wheel from /app, "
        "so anything not COPY'd cannot land in the wheel"
    )


def test_dockerignore_does_not_exclude_required_entries() -> None:
    """The existing .dockerignore may exclude envs/caches/tests, but never
    a module the wheel needs (mcp_auth.py / mcp_metrics.py)."""
    ignore = APP_DIR / ".dockerignore"
    if not ignore.is_file():
        return
    patterns = {
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    banned_exact = {"mcp_auth.py", "mcp_metrics.py", "*.py"}
    hit = patterns & banned_exact
    assert not hit, f".dockerignore excludes build-required entries: {hit}"


# ---------------------------------------------------------------- layer 3


@pytest.mark.skipif(
    not _build_backend_available(),
    reason="no local build backend (setuptools) in this interpreter — "
    "wheel-content proof exercised under the conda ML14 python instead",
)
def test_wheel_contains_all_required_modules(tmp_path: Path) -> None:
    """Deterministic offline proof: build the wheel from a tmp COPY of
    EXACTLY the build context the Dockerfile COPY lines would assemble and
    assert every required module is inside. Derived from the Dockerfile
    (not a hand-maintained list) so the two layers cannot disagree — a
    declared py-module missing from the build context is SILENTLY OMITTED
    by setuptools, which is precisely how this crash class ships unnoticed."""
    src = tmp_path / "build-src"
    src.mkdir()
    for entry in sorted(_copy_sources()):
        if any(c in entry for c in "*$"):
            continue  # defensive: no globs/build-args in these Dockerfiles
        s = APP_DIR / entry
        assert s.exists(), f"Dockerfile COPY source {entry} does not exist"
        if s.is_dir():
            shutil.copytree(s, src / entry, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(s, src / entry)
    out = tmp_path / "wheel"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            str(src),
            "-w",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"pip wheel failed:\n{proc.stdout}\n{proc.stderr}"
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    for module in REQUIRED_MODULES:
        assert f"{module}.py" in names, (
            f"wheel {wheels[0].name} does not contain {module}.py — the "
            "image would crash-loop at startup (ModuleNotFoundError)"
        )
