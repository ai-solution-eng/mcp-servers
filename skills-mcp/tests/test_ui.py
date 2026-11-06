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


def test_identity_header_is_displayed() -> None:
    response = run(
        ui.skill_detail(
            make_request("/skills/beta", headers={"X-Forwarded-User": "bryan@corp"}, path_params={"name": "beta"})
        )
    )
    assert "bryan@corp" in response.body.decode()


def test_anonymous_is_labelled() -> None:
    response = run(ui.skill_detail(make_request("/skills/beta", path_params={"name": "beta"})))
    assert "anonymous" in response.body.decode()


# ------------------------------------------------------------------ switch


def test_ui_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_UI_ENABLED", "0")
    assert ui.build_ui_routes() == []


def test_routes_present_by_default() -> None:
    paths = [route.path for route in ui.build_ui_routes()]
    assert paths == ["/", "/skills/{name}", "/skills/{name}/raw"]
