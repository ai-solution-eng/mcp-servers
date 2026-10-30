"""Unit tests for the skill loader — the contract, one rule per test.

Every test builds its own tiny tree in tmp_path: no dependence on the real
skills repo, so the suite is fast and hermetic.
"""

from pathlib import Path

from skills_mcp.catalog import (
    DESCRIPTION_WARN_CHARS,
    Catalog,
    SkillError,
    load_catalog,
    load_skill,
    split_frontmatter,
)


def make_skill(
    root: Path,
    folder: str,
    *,
    name: str | None = None,
    description: str = "Use when testing the loader.",
    body: str = "Do the thing.",
    source: str = "skills",
    extra_files: dict[str, str] | None = None,
) -> Path:
    directory = root / source / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name if name is not None else folder}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in (extra_files or {}).items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


# ------------------------------------------------------------------ loading


def test_loads_a_valid_skill(tmp_path: Path) -> None:
    make_skill(tmp_path, "cluster-triage", description="Use when the cluster is broken.", body="Step 1.\nStep 2.")
    catalog = load_catalog(tmp_path)
    assert catalog.names() == ["cluster-triage"]
    skill = catalog.get("cluster-triage")
    assert skill is not None
    assert skill.description == "Use when the cluster is broken."
    assert skill.body == "Step 1.\nStep 2."
    assert skill.files == ()


def test_body_excludes_frontmatter(tmp_path: Path) -> None:
    make_skill(tmp_path, "alpha", body="# Heading\n\nBody text.")
    skill = load_skill(tmp_path / "skills" / "alpha")
    assert "---" not in skill.body
    assert "name:" not in skill.body
    assert skill.body.startswith("# Heading")


def test_multiline_description_folds_to_single_line(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "alpha"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Use when the\n  thing is broken\n---\nbody\n",
        encoding="utf-8",
    )
    skill = load_skill(directory)
    assert skill.description == "Use when the thing is broken"


def test_files_are_listed_and_exclude_skill_md(tmp_path: Path) -> None:
    make_skill(
        tmp_path,
        "with-files",
        extra_files={"scripts/run.py": "print(1)", "assets/logo.png": "png"},
    )
    skill = load_skill(tmp_path / "skills" / "with-files")
    assert skill.files == ("assets/logo.png", "scripts/run.py")
    assert "SKILL.md" not in skill.files


def test_files_exclude_backups_and_bytecode(tmp_path: Path) -> None:
    make_skill(
        tmp_path,
        "messy",
        extra_files={
            "SKILL.md.bak-2026-10-01": "old",
            "scripts/__pycache__/run.cpython-312.pyc": "pyc",
            "scripts/run.py": "print(1)",
        },
    )
    skill = load_skill(tmp_path / "skills" / "messy")
    assert skill.files == ("scripts/run.py",)


def test_no_recursion_into_subdirectories(tmp_path: Path) -> None:
    make_skill(tmp_path, "alpha")
    nested = tmp_path / "skills" / "alpha" / "nested"
    nested.mkdir()
    (nested / "SKILL.md").write_text("---\nname: nested\ndescription: nope\n---\n", encoding="utf-8")
    catalog = load_catalog(tmp_path)
    assert catalog.names() == ["alpha"]


# ------------------------------------------------------------------ rejection


def test_name_must_match_folder(tmp_path: Path) -> None:
    make_skill(tmp_path, "folder-name", name="different-name")
    catalog = load_catalog(tmp_path)
    assert catalog.names() == []
    assert any("does not match folder" in warning for warning in catalog.warnings)


def test_invalid_name_shape_is_rejected(tmp_path: Path) -> None:
    make_skill(tmp_path, "Bad_Name", name="Bad_Name")
    catalog = load_catalog(tmp_path)
    assert catalog.names() == []
    assert any("lower-kebab-case" in warning for warning in catalog.warnings)


def test_missing_frontmatter_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "plain"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("# Just markdown, no frontmatter\n", encoding="utf-8")
    catalog = load_catalog(tmp_path)
    assert catalog.names() == []
    assert any("no YAML frontmatter" in warning for warning in catalog.warnings)


def test_invalid_yaml_is_skipped(tmp_path: Path) -> None:
    """The real-world case: an unquoted description containing ': ' (lessons-loop)."""
    directory = tmp_path / "skills" / "broken"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: broken\ndescription: Domain-agnostic: applies to anything\n---\nbody\n",
        encoding="utf-8",
    )
    catalog = load_catalog(tmp_path)
    assert catalog.names() == []
    assert any("invalid YAML frontmatter" in warning for warning in catalog.warnings)


def test_missing_description_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "alpha"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: alpha\n---\nbody\n", encoding="utf-8")
    catalog = load_catalog(tmp_path)
    assert catalog.names() == []
    assert any("no 'description'" in warning for warning in catalog.warnings)


# ------------------------------------------------------------------ warnings


def test_long_description_warns_but_loads(tmp_path: Path) -> None:
    make_skill(tmp_path, "wordy", description="x" * (DESCRIPTION_WARN_CHARS + 1))
    catalog = load_catalog(tmp_path)
    assert catalog.names() == ["wordy"]
    assert any("description is" in warning for warning in catalog.warnings)


def test_missing_source_path_warns_but_does_not_blank_the_catalogue(tmp_path: Path) -> None:
    make_skill(tmp_path, "alpha")
    catalog = load_catalog(tmp_path, ["skills", "skills-demo"])
    assert catalog.names() == ["alpha"]
    assert any("skills-demo: source path not found" in warning for warning in catalog.warnings)


def test_duplicate_name_across_source_paths_keeps_the_first(tmp_path: Path) -> None:
    make_skill(tmp_path, "alpha", description="from team", source="skills")
    make_skill(tmp_path, "alpha", description="from demo", source="skills-demo")
    catalog = load_catalog(tmp_path, ["skills", "skills-demo"])
    assert catalog.names() == ["alpha"]
    assert catalog.get("alpha").description == "from team"
    assert any("duplicate skill name" in warning for warning in catalog.warnings)


def test_directory_without_skill_md_is_counted_as_ignored(tmp_path: Path) -> None:
    make_skill(tmp_path, "alpha")
    (tmp_path / "skills" / "assets").mkdir()
    catalog = load_catalog(tmp_path)
    assert catalog.ignored == 1


def test_empty_catalog_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    catalog = load_catalog(tmp_path)
    assert isinstance(catalog, Catalog)
    assert len(catalog) == 0
    assert catalog.names() == []


# ------------------------------------------------------------------ primitives


def test_split_frontmatter_returns_mapping_and_body() -> None:
    meta, body = split_frontmatter("---\nname: alpha\ndescription: d\n---\n\nBody\n")
    assert meta == {"name": "alpha", "description": "d"}
    assert body == "Body"


def test_split_frontmatter_rejects_non_mapping() -> None:
    try:
        split_frontmatter("---\n- just\n- a list\n---\nbody\n")
    except SkillError as exc:
        assert "not a YAML mapping" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SkillError")
