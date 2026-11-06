"""Attribution: the caller reaches the tools, and stays absent when unknown.

The wiring under test is CallerCapture (ASGI) -> mcp_auth.CALLER_CONTEXT ->
the tool. scripts/probe_caller_context.py proves the ContextVar survives the
SDK's task boundary; these tests pin the response shape and the defaults.
"""

import asyncio
import json
from pathlib import Path

import pytest
from mcp_auth import CALLER_CONTEXT, Caller

from skills_mcp import server
from skills_mcp.catalog import Catalog


def run(coro):
    return asyncio.run(coro)


def write_skill(root: Path, folder: str, body: str = "Do the thing.") -> None:
    directory = root / "skills" / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {folder}\ndescription: Use when testing.\n---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("SKILLS_SOURCE_PATHS", "skills")
    monkeypatch.delenv("SKILLS_SOURCE_DIR", raising=False)
    monkeypatch.delenv("SKILLS_MCP_CLIENTS", raising=False)
    CALLER_CONTEXT.set(None)
    yield
    CALLER_CONTEXT.set(None)
    server.set_state(server.State(catalog=Catalog(), source=server.Source(origin="test")))


def use(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")
    server.set_state(server.load_state(tmp_path, server.Source(origin="test")))


def as_caller(name: str | None, key_fp: str = "sha256:abc123def456") -> None:
    CALLER_CONTEXT.set(Caller(key_fp=key_fp, client="10.0.0.9:5000", name=name))


# ------------------------------------------------------------------ presence


def test_named_caller_appears_in_list_skills(tmp_path: Path) -> None:
    use(tmp_path)
    as_caller("alice")

    payload = json.loads(run(server.list_skills()))
    assert payload["caller"]["name"] == "alice"
    assert payload["caller"]["key_fp"] == "sha256:abc123def456"


def test_internal_client_ip_is_not_exposed(tmp_path: Path) -> None:
    use(tmp_path)
    as_caller("alice")

    payload = json.loads(run(server.list_skills()))
    assert "client" not in payload["caller"]


def test_named_caller_appears_in_get_skill_and_file(tmp_path: Path) -> None:
    use(tmp_path)
    (tmp_path / "skills" / "alpha" / "run.py").write_text("print(1)", encoding="utf-8")
    as_caller("alice")

    assert json.loads(run(server.get_skill("alpha")))["caller"]["name"] == "alice"
    assert json.loads(run(server.get_skill_file("alpha", "run.py")))["caller"]["name"] == "alice"


# ------------------------------------------------------------------ absence


def test_anonymous_caller_has_no_caller_field(tmp_path: Path) -> None:
    use(tmp_path)
    CALLER_CONTEXT.set(None)

    payload = json.loads(run(server.list_skills()))
    assert "caller" not in payload


def test_matched_key_without_registry_name_is_anonymous(tmp_path: Path) -> None:
    """A valid key with no SKILLS_MCP_CLIENTS entry: attributable by fingerprint,
    never by name."""
    use(tmp_path)
    as_caller(None)

    payload = json.loads(run(server.list_skills()))
    assert "name" not in payload["caller"]
    assert payload["caller"]["key_fp"] == "sha256:abc123def456"


# ------------------------------------------------------------------ wiring


def test_app_is_wrapped_with_caller_capture() -> None:
    """Capture must be the OUTERMOST wrapper so rejected requests are still
    attributable — this test fails if someone reorders the wrappers."""
    assert isinstance(server._build_http_app(), server.CallerCapture)
