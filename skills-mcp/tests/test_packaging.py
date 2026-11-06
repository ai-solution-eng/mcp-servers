"""Packaging regression tests: the "wheel built fine, pod crash-loops" class.

The image installs NON-editable (``uv pip install --system .``) and runs the
installed ``skills-mcp`` console script from site-packages. Any module the
server imports that the wheel does NOT ship makes the container crash-loop at
startup with ModuleNotFoundError, while the build stays green. Three layers
must agree, and each has a test:

1. pyproject.toml declares the app as the ``skills_mcp`` PACKAGE
   (package-dir -> src/skills_mcp) with the fleet-hardlinked ``mcp_auth`` as
   the only top-level py-module, plus an AST cross-check of the package's
   imports so that list cannot drift silently.
2. The Dockerfile COPYs every one of those files into the build context (and
   .dockerignore excludes none of them).
3. (skipped when there is no local build backend) a wheel built from the tree
   actually CONTAINS them all.

Hermetic and offline: no network, no import of server.py, and the wheel build
runs on a tmp COPY of the build inputs so the source tree stays clean.
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

PACKAGE = "skills_mcp"
PACKAGE_SRC = APP_DIR / "src" / PACKAGE

# Modules inside the package that must ship. Add new ones here as the server
# grows; the AST test catches the omission too.
REQUIRED_PACKAGE_MODULES = ["server", "catalog", "state", "refresh"]
# Top-level modules at the repo root that must ship (the fleet hardlink).
REQUIRED_TOP_LEVEL_MODULES = ["mcp_auth"]
REQUIRED_PACKAGES = [PACKAGE]
# Build-context entries the Dockerfile must COPY for the wheel to assemble.
REQUIRED_COPY_ENTRIES = ["pyproject.toml", "src", "mcp_auth.py"]


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
            # COPY <src...> <dest>: the last token is the destination.
            sources.update(p.rstrip("/") for p in parts[1:-1])
    return sources


def _local_imports(py_file: Path) -> set[str]:
    """Top-level module names imported by a source file (AST, no import).

    Relative imports (``from . import catalog``) resolve INSIDE the package,
    so they count as the package itself; absolute imports of names that resolve
    to a local file (mcp_auth at the repo root) stay top-level entries.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the src-layout package
                names.add(PACKAGE)
            elif node.module:
                names.add(node.module.split(".")[0])
    return {
        n
        for n in names
        if (APP_DIR / f"{n}.py").is_file()
        or (APP_DIR / "src" / n / "__init__.py").is_file()
    }


def _build_backend_available() -> bool:
    """The wheel-content test needs setuptools (the declared backend) + pip."""
    return importlib.util.find_spec("setuptools") is not None and (
        importlib.util.find_spec("pip") is not None
    )


# layer 1


def test_pyproject_declares_all_required_modules() -> None:
    declared_packages = set(_setuptools_config().get("packages", []))
    missing_pkgs = [p for p in REQUIRED_PACKAGES if p not in declared_packages]
    declared_modules = set(_setuptools_config().get("py-modules", []))
    missing_mods = [m for m in REQUIRED_TOP_LEVEL_MODULES if m not in declared_modules]
    assert not missing_pkgs and not missing_mods, (
        f"pyproject [tool.setuptools] is missing packages {missing_pkgs} / "
        f"py-modules {missing_mods}; the non-editable image install would "
        "crash-loop at startup (ModuleNotFoundError)"
    )


def test_pyproject_modules_cover_all_local_imports() -> None:
    """AST drift-guard: every local import in the package must be declared.
    Relative imports wire the src-layout package together, so the package must
    be declared, and the repo-root fleet module must stay a py-module. A new
    local import without a pyproject update means the next image build
    crash-loops."""
    declared = set(_setuptools_config().get("py-modules", []))
    packages = set(_setuptools_config().get("packages", []))
    imported: set[str] = set()
    for src in PACKAGE_SRC.glob("*.py"):
        imported |= _local_imports(src)
    undeclared_packages = imported & {PACKAGE} - packages
    undeclared_modules = imported - {PACKAGE} - declared
    assert not undeclared_packages and not undeclared_modules, (
        f"the package imports local modules "
        f"{sorted(undeclared_packages | undeclared_modules)} that pyproject "
        f"does not declare (packages: {sorted(packages)}, "
        f"py-modules: {sorted(declared)})"
    )


def test_console_script_entry_point_pins_server_main() -> None:
    """The image CMD runs the installed console script = server:main, the
    whole crash class exists because that import must resolve from
    site-packages. Pin the mapping so it cannot be renamed silently."""
    scripts = _pyproject().get("project", {}).get("scripts", {})
    assert scripts.get("skills-mcp") == f"{PACKAGE}.server:main"


# layer 2


def test_dockerfile_copies_all_build_context_files() -> None:
    sources = _copy_sources()
    missing = [e for e in REQUIRED_COPY_ENTRIES if e not in sources]
    assert not missing, (
        f"Dockerfile COPY lines do not bring {missing} into the build "
        "context; `uv pip install --system .` builds the wheel from /app, "
        "so anything not COPY'd cannot land in the wheel"
    )


def test_dockerignore_does_not_exclude_required_entries() -> None:
    """The .dockerignore may exclude envs/caches/tests, but never a build
    input the wheel needs (the src/ package tree, mcp_auth.py)."""
    ignore = APP_DIR / ".dockerignore"
    if not ignore.is_file():
        return
    patterns = {
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    banned_exact = {"mcp_auth.py", "src", "*.py"}
    hit = patterns & banned_exact
    assert not hit, f".dockerignore excludes build-required entries: {hit}"


# layer 3


@pytest.mark.skipif(
    not _build_backend_available(),
    reason="no local build backend (setuptools) in this interpreter, so the "
    "wheel-content proof runs in the image build's sanity gate instead",
)
def test_wheel_contains_all_required_modules(tmp_path: Path) -> None:
    """Deterministic offline proof: build the wheel from a tmp COPY of EXACTLY
    the build context the Dockerfile COPY lines would assemble, and assert
    every required module is inside. Derived from the Dockerfile (not a
    hand-maintained list) so the two layers cannot disagree: a declared
    py-module missing from the build context is silently omitted by setuptools,
    which is precisely how this crash class ships unnoticed."""
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
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         str(src), "-w", str(out)],
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
            f"wheel {wheels[0].name} does not contain {PACKAGE}/{module}.py "
            ": the image would crash-loop at startup (ModuleNotFoundError)"
        )
    for module in REQUIRED_TOP_LEVEL_MODULES:
        assert f"{module}.py" in names, (
            f"wheel {wheels[0].name} does not contain {module}.py: the image "
            "would crash-loop at startup (ModuleNotFoundError)"
        )
