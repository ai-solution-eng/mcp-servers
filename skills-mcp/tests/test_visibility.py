"""Visibility: the filter that both the MCP tools and the UI go through."""

import asyncio
import json
from pathlib import Path

import pytest

from skills_mcp import policy as policy_mod
from skills_mcp import server, ui, visibility
from skills_mcp.catalog import Catalog

POLICY = """
default:
  paths: [skills]
roles:
  demo:
    members: [bryan]
    paths: [skills, skills-demo]
"""


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
    monkeypatch.setenv("SKILLS_SOURCE_PATHS", "skills,skills-demo")
    monkeypatch.delenv("SKILLS_SOURCE_DIR", raising=False)
    write_skill(tmp_path, "skills", "alpha", body="See beta for details.")
    write_skill(tmp_path, "skills", "beta")
    write_skill(tmp_path, "skills-demo", "triage-categories", body="Demo only.")
    server.set_state(server.load_state(tmp_path, server.Source(origin="test")))
    yield tmp_path
    server.set_state(server.State(catalog=Catalog(), source=server.Source(origin="test")))


def set_policy(tmp_path: Path, monkeypatch, text: str = POLICY) -> None:
    path = tmp_path / "access.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SKILLS_POLICY_FILE", str(path))


def as_caller(name: str | None) -> None:
    from mcp_auth import CALLER_CONTEXT, Caller

    CALLER_CONTEXT.set(Caller(key_fp="sha256:abc", client=None, name=name))


def use_identity(name: str | None) -> None:
    as_caller(name)


@pytest.fixture(autouse=True)
def _reset_caller():
    from mcp_auth import CALLER_CONTEXT

    CALLER_CONTEXT.set(None)
    yield
    CALLER_CONTEXT.set(None)


# ------------------------------------------------------------------ no policy


def test_no_policy_means_everything(tree) -> None:
    assert [s.name for s in visibility.visible_skills(None)] == ["alpha", "beta", "triage-categories"]
    assert visibility.role_label(None) is None


# ------------------------------------------------------------------ filtering


def test_default_role_sees_only_the_base_tier(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)
    assert [s.name for s in visibility.visible_skills("mallory")] == ["alpha", "beta"]
    assert visibility.role_label("mallory") == "default"


def test_named_role_sees_extra_paths(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)
    names = [s.name for s in visibility.visible_skills("bryan")]
    assert names == ["alpha", "beta", "triage-categories"]
    assert visibility.role_label("bryan") == "demo"


def test_source_path_is_carried_by_each_skill(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)
    by_name = {s.name: s for s in visibility.visible_skills("bryan")}
    assert by_name["alpha"].source_path == "skills"
    assert by_name["triage-categories"].source_path == "skills-demo"


def test_hidden_skill_is_not_retrievable(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)
    assert visibility.get_visible("triage-categories", "mallory") is None
    assert visibility.get_visible("triage-categories", "bryan") is not None


# ------------------------------------------------------------------ MCP tools


def test_list_skills_count_depends_on_identity(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)

    as_caller("mallory")
    base = json.loads(run(server.list_skills()))
    as_caller("bryan")
    demo = json.loads(run(server.list_skills()))

    assert base["count"] == 2 and base["role"] == "default"
    assert demo["count"] == 3 and demo["role"] == "demo"


def test_get_skill_refuses_hidden_skills(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)
    as_caller("mallory")

    payload = json.loads(run(server.get_skill("triage-categories")))
    assert "error" in payload
    assert "triage-categories" not in payload["valid_names"]


def test_get_skill_file_refuses_hidden_skills(tree, monkeypatch) -> None:
    set_policy(tree, monkeypatch)
    as_caller("mallory")
    payload = json.loads(run(server.get_skill_file("triage-categories", "SKILL.md")))
    assert "error" in payload


def test_related_only_lists_visible_skills(tree, monkeypatch) -> None:
    # alpha mentions "beta" in its body; both are visible -> listed
    set_policy(tree, monkeypatch)
    as_caller("mallory")
    assert json.loads(run(server.get_skill("alpha")))["related"] == ["beta"]


# ------------------------------------------------------------------ UI


def test_ui_sidebar_only_shows_visible_skills(tree, monkeypatch) -> None:
    from starlette.requests import Request

    set_policy(tree, monkeypatch)

    def request_with_user(user: str | None):
        headers = [(b"x-forwarded-user", user.encode())] if user else []
        return Request(
            {"type": "http", "method": "GET", "path": "/", "headers": headers,
             "query_string": b"", "path_params": {}}
        )

    visible_page = run(ui.skill_detail(request_with_user("bryan")))
    hidden_page = run(ui.skill_detail(request_with_user("mallory")))

    assert "triage-categories" in visible_page.body.decode()
    assert "triage-categories" not in hidden_page.body.decode()
    assert "role: demo" in visible_page.body.decode()
