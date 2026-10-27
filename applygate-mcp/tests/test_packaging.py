"""Packaging regression tests — the Wave-6 deploy-blocker class (mcp_auth crash).

Root cause being pinned: the server image is built with a NON-editable
``uv pip install --system .`` and runs the installed ``applygate-mcp``
console script (= ``server:main``) from site-packages. ``server.py`` does
``import mcp_auth`` (auth wave) but the wheel shipped only
``py-modules = ["server", "webui"]`` — a rebuilt image crash-loops at
startup with ModuleNotFoundError. The live images predate the auth wave,
which is why it was never seen until a rebuild.

Three layers must agree, and each has a test:

1. ``pyproject.toml`` declares the src-layout package (``packages`` +
   ``package-dir``) and every runtime-imported TOP-LEVEL module under
   ``[tool.setuptools] py-modules`` (with an AST cross-check of the package
   modules' imports so neither list can silently drift again).
2. The Dockerfile COPYs every one of those files into the build context
   (and the existing .dockerignore excludes none of them).
3. (skipif no local build backend) a wheel built from the tree with
   ``pip wheel --no-deps --no-build-isolation`` actually CONTAINS them all.

Hermetic and offline: no network (build isolation off, --no-deps), no
import of the app modules, and the wheel build runs on a tmp COPY of the build
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

# What the installed wheel must ship (fleet src layout): the PACKAGE under
# src/applygate_mcp/ holding every app module, plus the TOP-LEVEL mcp_auth
# py-module that the package's server does `import mcp_auth` for (verified
# 2026-09-13: mcp_auth is the only local top-level import). applygate has NO
# mcp_metrics module — its /metrics surface is inline in server (Wave-3 C4),
# unlike workbench/logsearch/searxng.
REQUIRED_MODULES = ["mcp_auth"]  # -> [tool.setuptools] py-modules (repo-root files)
REQUIRED_PACKAGES = ["applygate_mcp"]  # -> [tool.setuptools] packages (src/<pkg>/)
REQUIRED_PACKAGE_MODULES = ["server", "webui"]  # app modules inside the package

# Build-context files/dirs the Dockerfile must COPY (pyproject + the src tree
# + every root-level py-module).
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
    # Keep only names that resolve to a local file/package in the app dir, the
    # src tree, or a package dir. Sibling modules INSIDE src/<pkg>/ are imported
    # relatively (node.level > 0) and never reach here; an absolute `import
    # webui` written inside the package still resolves to the package file, so
    # the drift guard keeps its teeth after the src-layout move.
    roots = [APP_DIR, *(APP_DIR / "src" / p for p in REQUIRED_PACKAGES)]
    return {
        n for n in names if any((root / f"{n}.py").is_file() or (root / n / "__init__.py").is_file() for root in roots)
    }


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
        "(ModuleNotFoundError) because the package's server imports them"
    )


def test_pyproject_declares_the_src_package() -> None:
    """Layer 1 for the fleet src layout: the package must be mapped via
    package-dir/packages AND every app module must actually live inside it.
    setuptools SILENTLY OMITS a module that is not inside a declared package,
    so a module left behind at the repo root ships nothing — the same
    crash-loop class this file exists for."""
    config = _setuptools_config()
    missing = [p for p in REQUIRED_PACKAGES if p not in set(config.get("packages", []))]
    assert not missing, (
        f"pyproject [tool.setuptools] packages is missing {missing}; the wheel "
        "would carry no app code and the image would crash-loop at startup"
    )
    package_dir = config.get("package-dir", {})
    for pkg in REQUIRED_PACKAGES:
        assert package_dir.get(pkg) == f"src/{pkg}", (
            f"[tool.setuptools] package-dir must map {pkg} -> src/{pkg} (got {package_dir.get(pkg)!r})"
        )
        for module in REQUIRED_PACKAGE_MODULES:
            path = APP_DIR / "src" / pkg / f"{module}.py"
            assert path.is_file(), f"missing app module {path} — the src-layout move left it behind"


def test_pyproject_modules_cover_all_local_imports() -> None:
    """AST drift-guard: every local top-level import of the package modules
    must be declared. New local import without a py-modules update = the
    next image build crash-loops."""
    declared = set(_setuptools_config().get("py-modules", [])) | set(_setuptools_config().get("packages", []))
    imported: set[str] = set()
    for pkg in REQUIRED_PACKAGES:
        for module in REQUIRED_PACKAGE_MODULES:
            path = APP_DIR / "src" / pkg / f"{module}.py"
            if path.is_file():
                imported |= _local_top_level_imports(path)
    undeclared = imported - declared
    assert not undeclared, (
        f"the package modules import local modules {sorted(undeclared)} that "
        f"pyproject does not declare (declared: {sorted(declared)})"
    )


def test_console_script_entry_point_pins_server_main() -> None:
    """The image CMD runs the installed console script = applygate_mcp.server:main
    — the whole crash class exists because THAT import must resolve from
    site-packages. Pin the mapping so it cannot be renamed silently."""
    scripts = _pyproject().get("project", {}).get("scripts", {})
    assert scripts.get("applygate-mcp") == "applygate_mcp.server:main"


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
    a module the wheel needs (mcp_auth.py)."""
    ignore = APP_DIR / ".dockerignore"
    if not ignore.is_file():
        return
    patterns = {
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    banned_exact = {"mcp_auth.py", "*.py"}
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
    for pkg in REQUIRED_PACKAGES:
        for module in REQUIRED_PACKAGE_MODULES:
            assert f"{pkg}/{module}.py" in names, (
                f"wheel {wheels[0].name} does not contain {pkg}/{module}.py — "
                "the image would crash-loop at startup (ModuleNotFoundError)"
            )
