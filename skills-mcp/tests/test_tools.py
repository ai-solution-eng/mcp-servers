"""Unit tests for the three MCP tools.

The tools are plain async functions (the SDK decorator returns the function),
so they are called directly with asyncio.run — no server, no HTTP, no MCP
handshake. State is injected per test via server.set_state().
"""

import asyncio
import base64
import inspect
import json
from pathlib import Path

import pytest

from skills_mcp import server
from skills_mcp.catalog import Catalog


def run(coro):
    return asyncio.run(coro)


def write_skill(
    root: Path,
    folder: str,
    *,
    description: str = "Use when testing.",
    body: str = "Do the thing.",
    files: dict[str, bytes | str] | None = None,
) -> Path:
    directory = root / "skills" / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {folder}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return directory


def use(tmp_path: Path) -> None:
    """Point the server at this test's tree."""
    server.set_state(server.load_state(tmp_path, server.Source(origin="test", ref="deadbeef")))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("SKILLS_SOURCE_PATHS", "skills")
    monkeypatch.delenv("SKILLS_SOURCE_DIR", raising=False)
    yield
    server.set_state(server.State(catalog=Catalog(), source=server.Source(origin="test")))


# ---------------------------------------------------------------- list_skills


def test_list_skills_returns_the_catalogue(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", description="First skill.")
    write_skill(tmp_path, "beta", description="Second skill.", files={"run.py": "print(1)"})
    use(tmp_path)

    payload = json.loads(run(server.list_skills()))
    assert payload["count"] == 2
    assert [row["name"] for row in payload["skills"]] == ["alpha", "beta"]
    assert payload["source"]["origin"] == "test"
    assert payload["source"]["ref"] == "deadbeef"


def test_list_skills_never_ships_bodies(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", body="SECRET INSTRUCTIONS")
    use(tmp_path)

    payload = json.loads(run(server.list_skills()))
    assert set(payload["skills"][0]) == {"name", "description", "has_files"}
    assert "SECRET INSTRUCTIONS" not in json.dumps(payload)


def test_list_skills_has_files_flag(tmp_path: Path) -> None:
    write_skill(tmp_path, "bare")
    write_skill(tmp_path, "loaded", files={"scripts/run.py": "print(1)"})
    use(tmp_path)

    rows = {row["name"]: row for row in json.loads(run(server.list_skills()))["skills"]}
    assert rows["bare"]["has_files"] is False
    assert rows["loaded"]["has_files"] is True


# ---------------------------------------------------------------- get_skill


def test_get_skill_returns_body_files_and_related(tmp_path: Path) -> None:
    write_skill(tmp_path, "beta", description="The beta skill.")
    write_skill(
        tmp_path,
        "alpha",
        description="The alpha skill.",
        body="Start with beta, then stop.",
        files={"scripts/run.py": "print(1)"},
    )
    use(tmp_path)

    payload = json.loads(run(server.get_skill("alpha")))
    assert payload["name"] == "alpha"
    assert payload["description"] == "The alpha skill."
    assert payload["body"] == "Start with beta, then stop."
    assert payload["files"] == ["scripts/run.py"]
    assert payload["related"] == ["beta"]
    assert payload["base_uri"] == "skill://alpha/"


def test_get_skill_related_omits_unresolved_names(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", body="See fleet-drift and ptc-team-runtime. Also beta.")
    write_skill(tmp_path, "beta")
    use(tmp_path)

    payload = json.loads(run(server.get_skill("alpha")))
    assert payload["related"] == ["beta"]


def test_get_skill_unknown_name_lists_valid_ones(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")
    use(tmp_path)

    payload = json.loads(run(server.get_skill("does-not-exist")))
    assert "error" in payload
    assert payload["valid_names"] == ["alpha"]


# ---------------------------------------------------------------- get_skill_file


def test_get_skill_file_returns_text(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", files={"scripts/run.py": "print('hi')\n"})
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", "scripts/run.py")))
    assert payload["encoding"] == "utf8"
    assert payload["content"] == "print('hi')\n"
    assert payload["bytes"] == len("print('hi')\n")


def test_get_skill_file_returns_base64_for_binary(tmp_path: Path) -> None:
    blob = b"\x89PNG\r\n\x1a\n\x00\xff"
    write_skill(tmp_path, "alpha", files={"assets/logo.png": blob})
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", "assets/logo.png")))
    assert payload["encoding"] == "base64"
    assert base64.b64decode(payload["content"]) == blob


@pytest.mark.parametrize(
    "bad_path",
    ["../secret.txt", "../../etc/passwd", "scripts/../../secret.txt", "/etc/passwd", "C:/windows/system.ini"],
)
def test_get_skill_file_refuses_escapes(tmp_path: Path, bad_path: str) -> None:
    write_skill(tmp_path, "alpha")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", bad_path)))
    assert "error" in payload
    assert "TOP SECRET" not in json.dumps(payload)


def test_get_skill_file_refuses_symlink_escape(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")
    outside = tmp_path / "outside.txt"
    outside.write_text("TOP SECRET", encoding="utf-8")
    (tmp_path / "skills" / "alpha" / "link.txt").symlink_to(outside)
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", "link.txt")))
    assert "error" in payload
    assert "TOP SECRET" not in json.dumps(payload)


def test_get_skill_file_missing_lists_available_files(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", files={"scripts/run.py": "print(1)"})
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", "scripts/nope.py")))
    assert "error" in payload
    assert payload["files"] == ["scripts/run.py"]


def test_get_skill_file_refuses_backups(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", files={"SKILL.md.bak-2026-10-01": "old instructions"})
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", "SKILL.md.bak-2026-10-01")))
    assert "error" in payload


def test_get_skill_file_refuses_oversized_files(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", files={"big.bin": b"x" * (server.MAX_FILE_BYTES + 1)})
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("alpha", "big.bin")))
    assert "error" in payload
    assert "cap" in payload["error"]


def test_get_skill_file_unknown_skill(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")
    use(tmp_path)

    payload = json.loads(run(server.get_skill_file("nope", "a.txt")))
    assert "error" in payload
    assert payload["valid_names"] == ["alpha"]


# ---------------------------------------------------------------- registration


def test_exactly_three_tools_are_exposed() -> None:
    tools = [server.list_skills, server.get_skill, server.get_skill_file]
    assert len(tools) == 3
    for tool in tools:
        assert inspect.iscoroutinefunction(tool), f"{tool.__name__} is not an async tool"


def test_tools_never_raise_on_an_empty_catalogue() -> None:
    server.set_state(server.State(catalog=Catalog(), source=server.Source(origin="none")))
    assert json.loads(run(server.list_skills()))["count"] == 0
    assert "error" in json.loads(run(server.get_skill("alpha")))
    assert "error" in json.loads(run(server.get_skill_file("alpha", "x")))
