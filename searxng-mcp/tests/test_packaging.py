"""Packaging regression tests — the Wave-6 deploy-blocker class (mcp_auth crash).

Root cause being pinned: the server image is built with a NON-editable
``uv pip install --system ".[browser]"`` and runs the installed
``searxng-mcp`` console script (= ``searxng_mcp.server:main``) from
site-packages. Any module/package that ``server.py`` imports but that the
wheel does not ship makes the container crash-loop at startup with
ModuleNotFoundError.
TWO modules hit exactly this: ``mcp_auth`` (auth wave) and ``mcp_metrics``
(Wave-3 C3 — which was ALSO missing from the Dockerfile COPY line, found
and fixed in the same pass). The live images predate those waves, which is
why the crash was never seen until a rebuild.

Three layers must agree, and each has a test:

1. ``pyproject.toml`` declares the src-layout ``searxng_mcp`` PACKAGE
   (via ``package-dir`` + ``packages``) plus the top-level ``mcp_auth``
   py-module — the app's own modules ship INSIDE the package, mcp_auth stays
   a root-level py-module (plus an AST cross-check of the package's server
   module imports so the list cannot silently drift again).
2. The Dockerfile COPYs the build context those declarations need —
   ``src/`` (the whole package) and ``mcp_auth.py`` — and the existing
   .dockerignore excludes none of them (it excludes the local ``searxng/``
   directory, which no runtime module imports).
3. (skipif no local build backend) a wheel built from the tree with
   ``pip wheel --no-deps --no-build-isolation`` actually CONTAINS them all.

Hermetic and offline: no network (build isolation off, --no-deps), no
import of server.py, and the wheel build runs on a tmp COPY of the build
inputs so the source tree stays clean (no build/ residue in git status).

Environment note (fleet convention): this suite runs under the conda
ML14 python (``python3 -m pytest tests/``), which HAS setuptools — so the
wheel-content test runs in-suite here (it adds ~2-4 s, still hermetic).
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

# What server.py imports at runtime (verified 2026-09-13): the app's own
# modules (now the searxng_mcp package under src/) + mcp_auth (auth wave) +
# mcp_metrics (Wave-3 C3, default-off /metrics). mcp_types is provided by the
# mcp>=2 dependency, not local.
PACKAGE = "searxng_mcp"
REQUIRED_PACKAGE_MODULES = [
    "server",
    "searxng_client",
    "fetcher",
    "browser_client",
    "url_policy",
    "mcp_metrics",
]
# mcp_auth is the fleet-hardlink module at the REPO ROOT: it stays a
# top-level py-module (imported as `import mcp_auth`, never relative) and
# must ship at the wheel root, not inside the package.
REQUIRED_TOP_LEVEL_MODULES = ["mcp_auth"]
REQUIRED_PACKAGES: list[str] = [PACKAGE]

# Build-context files/dirs the Dockerfile must COPY (pyproject + the src
# package tree + the root-level py-module — url_policy.py was itself missing
# from the COPY line until the Wave-6 packaging proof caught it: Wave-1 added
# the module but never touched this Dockerfile, so the src/ tree is now
# copied as a whole rather than file by file).
REQUIRED_COPY_ENTRIES = [
    "pyproject.toml",
    "src",
    "mcp_auth.py",
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
    config = _setuptools_config()
    declared_packages = set(config.get("packages", []))
    declared_modules = set(config.get("py-modules", []))
    missing_pkgs = [p for p in REQUIRED_PACKAGES if p not in declared_packages]
    assert not missing_pkgs, (
        f"pyproject [tool.setuptools] packages is missing {missing_pkgs}; the "
        "non-editable image install would crash-loop at startup "
        "(ModuleNotFoundError) because server.py imports them"
    )
    missing_mods = [m for m in REQUIRED_TOP_LEVEL_MODULES if m not in declared_modules]
    assert not missing_mods, (
        f"pyproject [tool.setuptools] py-modules is missing {missing_mods}; the "
        "non-editable image install would crash-loop at startup "
        "(ModuleNotFoundError) because server.py imports them"
    )
    # setuptools ships a declared package as a WHOLE, so every runtime module
    # must also be present in the package tree — a file missing from src/ is
    # silently absent from the wheel exactly like an undeclared py-module.
    pkg_dir = APP_DIR / "src" / PACKAGE
    missing_files = [m for m in REQUIRED_PACKAGE_MODULES if not (pkg_dir / f"{m}.py").is_file()]
    assert not missing_files, (
        f"src/{PACKAGE}/ is missing {missing_files}; the package ships as a "
        "tree, so a module that is not there never reaches the wheel"
    )


def test_pyproject_modules_cover_all_local_imports() -> None:
    """AST drift-guard: every local top-level import of the package's
    server module must be declared. New local import without a
    py-modules/packages update = the next image build crash-loops.
    (Non-local names like mcp_types, provided by the mcp dependency, are
    excluded by the local-file filter; sibling modules inside the package
    are imported RELATIVELY and are covered by the tree check above.)"""
    declared = set(_setuptools_config().get("py-modules", [])) | set(_setuptools_config().get("packages", []))
    imported: set[str] = set()
    path = APP_DIR / "src" / PACKAGE / "server.py"
    if path.is_file():
        imported |= _local_top_level_imports(path)
    undeclared = imported - declared
    assert not undeclared, (
        f"server.py imports local modules {sorted(undeclared)} that "
        f"pyproject does not declare (declared: {sorted(declared)})"
    )


def test_console_script_entry_point_pins_server_main() -> None:
    """The image CMD runs the installed console script = server:main — the
    whole crash class exists because THAT import must resolve from
    site-packages. Pin the mapping so it cannot be renamed silently."""
    scripts = _pyproject().get("project", {}).get("scripts", {})
    assert scripts.get("searxng-mcp") == "searxng_mcp.server:main"


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
    """The existing .dockerignore may exclude envs/caches/tests/the local
    searxng/ dir (imported by nothing at runtime), but never a module the
    wheel needs (mcp_auth.py / mcp_metrics.py / the five own modules)."""
    ignore = APP_DIR / ".dockerignore"
    if not ignore.is_file():
        return
    patterns = {
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    banned_exact = set(REQUIRED_COPY_ENTRIES) - {"pyproject.toml"} | {"*.py"}
    hit = patterns & banned_exact
    assert not hit, f".dockerignore excludes build-required entries: {hit}"


# ---------------------------------------------------------------- layer 3


@pytest.mark.skipif(
    not _build_backend_available(),
    reason="no local build backend (setuptools) in this interpreter — wheel-content proof exercised elsewhere",
)
def test_wheel_contains_all_required_modules(tmp_path: Path) -> None:
    """Deterministic offline proof: build the wheel from a tmp COPY of
    EXACTLY the build context the Dockerfile COPY lines would assemble and
    assert every required module is inside. Derived from the Dockerfile
    (not a hand-maintained list) so the two layers cannot disagree — a
    declared module missing from the build context is SILENTLY OMITTED by
    setuptools (this exact hole shipped for url_policy.py: Wave-1 added the
    module, the Dockerfile COPY line was never updated, and a wheel built
    without the file just leaves it out). The app ships as the src/<pkg>/
    PACKAGE tree, so the whole src/ COPY is what the wheel is built from."""
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
    for module in REQUIRED_PACKAGE_MODULES:
        assert f"{PACKAGE}/{module}.py" in names, (
            f"wheel {wheels[0].name} does not contain {PACKAGE}/{module}.py — "
            "the image would crash-loop at startup (ModuleNotFoundError)"
        )
    for module in REQUIRED_TOP_LEVEL_MODULES:
        assert f"{module}.py" in names, (
            f"wheel {wheels[0].name} does not contain the top-level {module}.py — "
            "the image would crash-loop at startup (ModuleNotFoundError)"
        )
