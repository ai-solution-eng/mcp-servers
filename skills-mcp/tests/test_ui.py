"""Viewer UI: the pages render, the guards hold, the switch works.

Requests are built by hand (no httpx in this venv), which is enough: the
handlers are plain async functions returning Response objects.
"""

import asyncio
import json
from pathlib import Path

import pytest
from starlette.requests import Request

from skills_mcp import server, ui
from skills_mcp.catalog import Catalog


def run(coro):
    return asyncio.run(coro)


def make_request(path: str = "/", headers: dict | None = None, path_params: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": raw,
            "query_string": b"",
            "path_params": path_params or {},
        }
    )


def write_skill(root: Path, folder: str, body: str = "Do the thing.", files: dict | None = None) -> None:
    directory = root / "skills" / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {folder}\ndescription: Use when testing {folder}.\n---\n\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_SOURCE_PATHS", "skills")
    monkeypatch.delenv("SKILLS_SOURCE_DIR", raising=False)
    monkeypatch.delenv("SKILLS_UI_ENABLED", raising=False)
    write_skill(tmp_path, "alpha", files={"run.py": "print(1)"})
    write_skill(tmp_path, "beta")
    server.set_state(server.load_state(tmp_path, server.Source(origin="test")))
    yield
    server.set_state(server.State(catalog=Catalog(), source=server.Source(origin="test")))


# ------------------------------------------------------------------ pages


def test_root_redirects_to_the_first_skill() -> None:
    response = run(ui.index(make_request("/")))
    assert response.status_code == 307
    assert response.headers["location"] == "/skills/alpha"


def test_detail_page_renders_the_skill() -> None:
    response = run(ui.skill_detail(make_request("/skills/beta", path_params={"name": "beta"})))
    assert response.status_code == 200
    body = response.body.decode()
    assert "<h2>beta</h2>" in body
    assert "raw markdown" in body
    assert "Use when testing beta." in body


def test_detail_lists_files() -> None:
    response = run(ui.skill_detail(make_request("/skills/alpha", path_params={"name": "alpha"})))
    body = response.body.decode()
    assert "run.py" in body


def test_unknown_skill_is_404() -> None:
    response = run(ui.skill_detail(make_request("/skills/nope", path_params={"name": "nope"})))
    assert response.status_code == 404
    assert "Unknown skill" in response.body.decode()


def test_raw_returns_markdown() -> None:
    response = run(ui.skill_raw(make_request("/skills/beta/raw", path_params={"name": "beta"})))
    assert response.status_code == 200
    assert response.media_type.startswith("text/markdown")
    assert response.body.decode().strip() == "Do the thing."


def test_raw_unknown_is_404() -> None:
    response = run(ui.skill_raw(make_request("/skills/nope/raw", path_params={"name": "nope"})))
    assert response.status_code == 404


# ------------------------------------------------------------------ identity


def test_identity_header_is_displayed_when_trusted(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_TRUST_PROXY_IDENTITY", "true")
    response = run(
        ui.skill_detail(
            make_request("/skills/beta", headers={"X-Forwarded-User": "bryan@corp"}, path_params={"name": "beta"})
        )
    )
    assert "bryan@corp" in response.body.decode()


def test_identity_header_is_ignored_by_default() -> None:
    response = run(
        ui.skill_detail(
            make_request("/skills/beta", headers={"X-Forwarded-User": "bryan@corp"}, path_params={"name": "beta"})
        )
    )
    assert "anonymous" in response.body.decode()


def test_anonymous_is_labelled() -> None:
    response = run(ui.skill_detail(make_request("/skills/beta", path_params={"name": "beta"})))
    assert "anonymous" in response.body.decode()


# ------------------------------------------------------------------ switch


def test_ui_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_UI_ENABLED", "0")
    assert ui.build_ui_routes() == []


def test_routes_present_by_default() -> None:
    paths = [route.path for route in ui.build_ui_routes()]
    assert paths == [
        "/",
        "/me",
        "/me/token",
        "/me/keys/{key_id}/revoke",
        "/skills/{name}",
        "/skills/{name}/toggle",
        "/skills/{name}/raw",
    ]


# ------------------------------------------------------------------ personal actions


def _trusted(monkeypatch, identity: str = "alice"):
    monkeypatch.setenv("SKILLS_TRUST_PROXY_IDENTITY", "true")
    return {"X-Forwarded-User": identity}


def test_toggle_disables_then_enables(tmp_path, monkeypatch) -> None:
    from skills_mcp import prefs

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)

    response = run(ui.skill_toggle(make_request("/skills/beta/toggle", headers=headers, path_params={"name": "beta"})))
    assert response.status_code == 303
    assert "beta" in prefs.disabled("alice")

    run(ui.skill_toggle(make_request("/skills/beta/toggle", headers=headers, path_params={"name": "beta"})))
    assert "beta" not in prefs.disabled("alice")


def test_toggle_requires_an_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    response = run(ui.skill_toggle(make_request("/skills/beta/toggle", path_params={"name": "beta"})))
    assert response.status_code == 401


def test_me_page_shows_identity_and_panels(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    body = run(ui.me_page(make_request("/me", headers=headers))).body.decode()
    assert "alice" in body
    assert "Mint a key" in body
    assert "Private skills" in body


def test_mint_shows_the_key_once_and_allows_more(tmp_path, monkeypatch) -> None:
    from skills_mcp import prefs

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)

    response = run(ui.me_token(make_request("/me/token", headers=headers)))
    assert response.status_code == 200
    assert "sk_live_" in response.body.decode()
    assert len(prefs.keys("alice")) == 1

    again = run(ui.me_token(make_request("/me/token", headers=headers)))
    assert again.status_code == 200
    assert len(prefs.keys("alice")) == 2


def test_mint_refused_for_operator_managed_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_MCP_CLIENTS", "alice:keyA")
    headers = _trusted(monkeypatch)
    response = run(ui.me_token(make_request("/me/token", headers=headers)))
    assert response.status_code == 409
