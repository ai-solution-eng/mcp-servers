"""Visibility under model C: global - disabled + private (private shadows)."""

import asyncio
import json
from pathlib import Path

import pytest

from skills_mcp import prefs, server, visibility
from skills_mcp.catalog import Catalog


def run(coro):
    return asyncio.run(coro)


def write_skill(root: Path, source: str, folder: str, body: str = "Body.") -> None:
    directory = root / source / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {folder}\ndescription: Use when {folder}.\n---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_SOURCE_PATHS", "skills")
    monkeypatch.delenv("SKILLS_SOURCE_DIR", raising=False)
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    write_skill(tmp_path, "skills", "alpha", body="See beta for details.")
    write_skill(tmp_path, "skills", "beta")
    server.set_state(server.load_state(tmp_path, server.Source(origin="test")))
    yield tmp_path
    server.set_state(server.State(catalog=Catalog(), source=server.Source(origin="test")))


def add_private(tmp_path: Path, identity: str, folder: str) -> None:
    write_skill(tmp_path / "private", identity, folder)


# ------------------------------------------------------------------ global


def test_everyone_sees_every_global_skill(tree) -> None:
    assert [s.name for s in visibility.visible_skills(None)] == ["alpha", "beta"]
    assert [s.name for s in visibility.visible_skills("alice")] == ["alpha", "beta"]


def test_disabling_hides_it_from_that_person_only(tree) -> None:
    prefs.set_disabled("alice", "beta", True)
    assert [s.name for s in visibility.visible_skills("alice")] == ["alpha"]
    assert [s.name for s in visibility.visible_skills("bob")] == ["alpha", "beta"]
    assert visibility.get_visible("beta", "alice") is None
    assert visibility.get_visible("beta", "bob") is not None


def test_reenabling_brings_it_back(tree) -> None:
    prefs.set_disabled("alice", "github:beta", True)
    assert [s.name for s in visibility.visible_skills("alice")] == ["alpha"]
    prefs.set_disabled("alice", "github:beta", False)
    assert [s.name for s in visibility.visible_skills("alice")] == ["alpha", "beta"]


def test_hiding_one_tier_keeps_the_other(tree) -> None:
    """The whole point of ids: hide the team's copy, keep your own."""
    add_private(tree, "alice", "alpha")

    prefs.set_disabled("alice", "github:alpha", True)
    alices = [s for s in visibility.visible_skills("alice") if s.name == "alpha"]
    assert [s.source_path for s in alices] == ["private"]

    prefs.set_disabled("alice", "github:alpha", False)
    prefs.set_disabled("alice", "private:alice:alpha", True)
    alices = [s for s in visibility.visible_skills("alice") if s.name == "alpha"]
    assert [s.source_path for s in alices] == ["skills"]

    prefs.set_disabled("alice", "github:alpha", True)
    assert [s for s in visibility.visible_skills("alice") if s.name == "alpha"] == []


# ------------------------------------------------------------------ private


def test_private_skill_is_visible_only_to_its_owner(tree) -> None:
    add_private(tree, "alice", "my-checklist")
    assert "my-checklist" in [s.name for s in visibility.visible_skills("alice")]
    assert "my-checklist" not in [s.name for s in visibility.visible_skills("bob")]
    assert "my-checklist" not in [s.name for s in visibility.visible_skills(None)]


def test_a_private_copy_does_not_hide_the_global(tree) -> None:
    """No shadowing: both copies are visible to their owner, and a name lookup
    is reported as ambiguous so the caller can choose by description."""
    add_private(tree, "alice", "alpha")

    alices = [s for s in visibility.visible_skills("alice") if s.name == "alpha"]
    assert [s.source_path for s in alices] == ["skills", "private"]

    verdict, found = visibility.resolve("alpha", "alice")
    assert verdict == "ambiguous" and len(found) == 2
    assert {visibility.skill_id(s, "alice") for s in found} == {
        "github:alpha",
        "private:alice:alpha",
    }

    # bob never sees alice's copy
    assert [s.source_path for s in visibility.visible_skills("bob") if s.name == "alpha"] == ["skills"]
    assert visibility.resolve("alpha", "bob")[0] == "one"


def test_visible_names_deduplicates(tree) -> None:
    add_private(tree, "alice", "alpha")
    assert visibility.visible_names("alice").count("alpha") == 1


# ------------------------------------------------------------------ MCP tools


def test_list_skills_reflects_the_personal_view(tree) -> None:
    from mcp_auth import CALLER_CONTEXT, Caller

    prefs.set_disabled("alice", "beta", True)
    CALLER_CONTEXT.set(Caller(key_fp="sha256:abc", client=None, name="alice"))
    try:
        payload = json.loads(run(server.list_skills()))
        assert payload["count"] == 1
        assert payload["skills"][0]["name"] == "alpha"
    finally:
        CALLER_CONTEXT.set(None)


def test_get_skill_refuses_a_disabled_skill(tree) -> None:
    from mcp_auth import CALLER_CONTEXT, Caller

    prefs.set_disabled("alice", "beta", True)
    CALLER_CONTEXT.set(Caller(key_fp="sha256:abc", client=None, name="alice"))
    try:
        payload = json.loads(run(server.get_skill("beta")))
        assert "error" in payload
        assert payload["valid_names"] == ["alpha"]
    finally:
        CALLER_CONTEXT.set(None)


def test_private_skill_is_readable_through_the_tool(tree) -> None:
    from mcp_auth import CALLER_CONTEXT, Caller

    add_private(tree, "alice", "my-checklist")
    CALLER_CONTEXT.set(Caller(key_fp="sha256:abc", client=None, name="alice"))
    try:
        payload = json.loads(run(server.get_skill("my-checklist")))
        assert payload["name"] == "my-checklist"
        assert payload["body"] == "Body."
    finally:
        CALLER_CONTEXT.set(None)
