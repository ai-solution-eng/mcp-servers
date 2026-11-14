"""Unit tests for the refresh machinery: seed, swap, fail-safe, prune.

No network: the tarball download is monkeypatched to copy a local .tgz built by
the test itself, and the GitHub commit lookup is stubbed.
"""

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from skills_mcp import refresh
from skills_mcp import state as state_mod


def skill_text(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n\n{body}\n"


def make_tarball(path: Path, skills: dict[str, str], *, top: str = "tools-main", source: str = "skills") -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, body in skills.items():
            data = skill_text(name, body).encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{source}/{name}/SKILL.md")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def make_bundled(root: Path, skills: dict[str, str]) -> Path:
    bundled = root / "bundled"
    for name, body in skills.items():
        directory = bundled / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(skill_text(name, body), encoding="utf-8")
    (bundled / ".ref").write_text("main", encoding="utf-8")
    return bundled


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLS_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SKILLS_BUNDLED_ROOT", str(tmp_path / "bundled"))
    monkeypatch.setenv("SKILLS_SOURCE_PATHS", "skills")
    monkeypatch.setenv("SKILLS_SOURCE_URL", "https://codeload.github.com/ai-solution-eng/tools/tar.gz")
    monkeypatch.setenv("SKILLS_SOURCE_REF", "main")
    monkeypatch.setenv("SKILLS_REFRESH_INTERVAL_S", "14400")
    state_mod.reset_state()
    yield
    state_mod.reset_state()


def serve(monkeypatch, tarball: Path) -> None:
    """Point the downloader at a local tarball."""
    monkeypatch.setattr(refresh, "_download", lambda url, dest: shutil.copy(tarball, dest))


# seeding


def test_seeds_working_copy_from_the_bundled_snapshot(env, tmp_path: Path) -> None:
    bundled = make_bundled(tmp_path, {"alpha": "bundled body"})
    served = refresh.ensure_working_copy()

    assert served is not None
    assert served.source.origin == "bundled"
    assert served.source.ref == "main"
    assert served.catalog.get("alpha").body == "bundled body"

    link = tmp_path / "data" / "skills-current"
    assert link.is_symlink()
    assert link.resolve() == bundled.resolve()


def test_no_source_at_all_returns_none(env) -> None:
    assert refresh.ensure_working_copy() is None


# refresh


def test_refresh_swaps_in_the_new_text(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "OLD instructions"})
    assert refresh.ensure_working_copy().catalog.get("alpha").body == "OLD instructions"

    tarball = make_tarball(tmp_path / "new.tgz", {"alpha": "NEW instructions"})
    serve(monkeypatch, tarball)
    monkeypatch.setattr(refresh, "_resolve_commit", lambda url, ref: "a1b2c3d4e5f6")

    payload = refresh.refresh_from_source()

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["commit"] == "a1b2c3d4e5f6"

    served = state_mod.current_state()
    assert served.source.origin == "github"
    assert served.source.commit == "a1b2c3d4e5f6"
    assert served.catalog.get("alpha").body == "NEW instructions"

    link = tmp_path / "data" / "skills-current"
    assert link.is_symlink()
    assert link.resolve().name == "a1b2c3d4e5f6"
    assert (link.resolve() / "skills" / "alpha" / "SKILL.md").is_file()


def test_failed_fetch_keeps_the_old_catalogue(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "GOOD instructions"})
    refresh.ensure_working_copy()

    # First a real success so we have something to protect.
    tarball = make_tarball(tmp_path / "good.tgz", {"alpha": "GOOD instructions"})
    serve(monkeypatch, tarball)
    monkeypatch.setattr(refresh, "_resolve_commit", lambda url, ref: "good1234")
    refresh.refresh_from_source()
    before = state_mod.current_state()

    # Now the fetch fails (the acceptance test: point at a 404).
    def boom(url, dest):
        raise refresh.RefreshError("fetch failed: HTTP 404 from " + url)

    monkeypatch.setattr(refresh, "_download", boom)
    with pytest.raises(refresh.RefreshError):
        refresh.refresh_from_source()

    after = state_mod.current_state()
    assert after.source.commit == before.source.commit
    assert after.catalog.get("alpha").body == "GOOD instructions"
    assert refresh.last_error() is not None
    assert "404" in refresh.last_error()


def test_zero_parsed_skills_aborts_the_swap(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "GOOD instructions"})
    refresh.ensure_working_copy()
    tarball = make_tarball(tmp_path / "good.tgz", {"alpha": "GOOD instructions"})
    serve(monkeypatch, tarball)
    refresh.refresh_from_source()
    before = state_mod.current_state()

    # A tarball whose SKILL.md files are all broken: nothing parses.
    broken = tmp_path / "broken.tgz"
    with tarfile.open(broken, "w:gz") as archive:
        data = b"---\nname: broken\ndescription: Domain-agnostic: nope\n---\n"
        info = tarfile.TarInfo("tools-main/skills/broken/SKILL.md")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    serve(monkeypatch, broken)

    with pytest.raises(refresh.RefreshError) as error:
        refresh.refresh_from_source()
    assert "0 skills parsed" in str(error.value)
    assert state_mod.current_state().source.commit == before.source.commit


def test_tarball_traversal_is_refused(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "GOOD instructions"})
    refresh.ensure_working_copy()

    evil = tmp_path / "evil.tgz"
    with tarfile.open(evil, "w:gz") as archive:
        data = skill_text("alpha", "evil").encode("utf-8")
        info = tarfile.TarInfo("tools-main/skills/../../escape/SKILL.md")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    serve(monkeypatch, evil)

    with pytest.raises(refresh.RefreshError):
        refresh.refresh_from_source()
    assert not (tmp_path / "escape").exists()


def test_prune_keeps_three_releases(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "v0"})
    refresh.ensure_working_copy()

    for index in range(1, 5):
        tarball = make_tarball(tmp_path / f"v{index}.tgz", {"alpha": f"v{index}"})
        serve(monkeypatch, tarball)
        monkeypatch.setattr(refresh, "_resolve_commit", lambda url, ref, i=index: f"sha{index:04d}")
        refresh.refresh_from_source()

    releases = [p for p in (tmp_path / "data" / "releases").iterdir() if p.is_dir()]
    assert len(releases) <= refresh.KEEP_RELEASES
    assert state_mod.current_state().catalog.get("alpha").body == "v4"


# boot


def test_boot_reads_the_state_file_after_a_refresh(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "bundled"})
    refresh.ensure_working_copy()
    tarball = make_tarball(tmp_path / "new.tgz", {"alpha": "from github"})
    serve(monkeypatch, tarball)
    monkeypatch.setattr(refresh, "_resolve_commit", lambda url, ref: "deadbeefcafe")
    refresh.refresh_from_source()

    # Simulate a pod restart: forget everything, re-bootstrap.
    state_mod.reset_state()
    served = refresh.ensure_working_copy()
    assert served is not None
    assert served.source.origin == "github"
    assert served.source.commit == "deadbeefcafe"
    assert served.catalog.get("alpha").body == "from github"


def test_broken_working_copy_falls_back_to_the_bundle(env, tmp_path: Path) -> None:
    make_bundled(tmp_path, {"alpha": "bundled body"})
    releases = tmp_path / "data" / "releases" / "deadbeef"
    releases.mkdir(parents=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "skills-current").symlink_to(releases)

    served = refresh.ensure_working_copy()
    assert served is not None
    assert served.source.origin == "bundled"
    assert served.catalog.get("alpha").body == "bundled body"


# config


def test_refresh_disabled_without_a_url(env, monkeypatch) -> None:
    monkeypatch.delenv("SKILLS_SOURCE_URL")
    assert refresh.refresh_enabled() is False
    with pytest.raises(refresh.RefreshError) as error:
        refresh.refresh_from_source()
    assert "disabled" in str(error.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 14400), ("0", 0), ("60", 60), ("nonsense", 14400), ("", 14400)],
)
def test_interval_parsing(env, monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("SKILLS_REFRESH_INTERVAL_S")
    else:
        monkeypatch.setenv("SKILLS_REFRESH_INTERVAL_S", raw)
    assert refresh.refresh_interval_s() == expected


def test_status_payload_describes_what_is_served(env, tmp_path: Path, monkeypatch) -> None:
    make_bundled(tmp_path, {"alpha": "bundled"})
    tarball = make_tarball(tmp_path / "new.tgz", {"alpha": "fresh"})
    serve(monkeypatch, tarball)
    monkeypatch.setattr(refresh, "_resolve_commit", lambda url, ref: "abc123def456")
    refresh.refresh_from_source()

    payload = refresh.status_payload()
    assert payload["origin"] == "github"
    assert payload["commit"] == "abc123def456"
    assert payload["count"] == 1
    assert payload["refresh"]["enabled"] is True
    assert payload["refresh"]["interval_s"] == 14400
    assert payload["warnings"] == []
