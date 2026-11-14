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


def make_request(
    path: str = "/",
    headers: dict | None = None,
    path_params: dict | None = None,
    method: str = "GET",
    body: bytes = b"",
) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": raw,
            "query_string": b"",
            "path_params": path_params or {},
        },
        receive,
    )


def form_request(
    path: str,
    fields: list[tuple[str, str]],
    headers: dict,
    path_params: dict | None = None,
) -> Request:
    from urllib.parse import urlencode

    merged = dict(headers)
    merged["content-type"] = "application/x-www-form-urlencoded"
    return make_request(
        path,
        headers=merged,
        method="POST",
        body=urlencode(fields).encode(),
        path_params=path_params,
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
        "/how-to-connect",
        "/me/sync",
        "/me/skills",
        "/me/skills",
        "/me/private/new",
        "/me/private",
        "/me/private/{name}/edit",
        "/me/private/{name}/delete",
        "/me/private/{name}/delete",
        "/me/private/{name}",
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


def test_mint_with_an_expiry_date(tmp_path, monkeypatch) -> None:
    """The date input sends YYYY-MM-DD; the key must expire at the END of it."""
    from skills_mcp import prefs

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    request = form_request("/me/token", [("label", "temp"), ("expires", "2026-12-14")], headers)
    response = run(ui.me_token(request))
    assert response.status_code == 200
    record = prefs.keys("alice")[0]
    assert record["expires_at"] == "2026-12-14T23:59:59Z"
    assert record["label"] == "temp"


def test_the_me_page_offers_an_expiry_input(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    body = run(ui.me_page(make_request("/me", headers=headers))).body.decode()
    assert "type=date" in body and "name=expires" in body


# ------------------------------------------------------------------ bulk manage


def test_manage_page_renders_one_checkbox_per_global_skill(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    body = run(ui.manage_skills(make_request("/me/skills", headers=headers))).body.decode()
    # each skill appears twice: once in the sidebar, once in the manage list
    assert body.count("value='alpha'") == 2
    assert body.count("value='beta'") == 2
    assert "Select all" in body and "Disable all" in body


def test_the_sidebar_has_checkboxes_and_a_save_button(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    body = run(ui.skill_detail(make_request("/skills/alpha", headers=headers, path_params={"name": "alpha"}))).body.decode()
    assert "name=visible" in body and "id=skillform" in body
    assert "Disable all" in body and ">Save<" in body
    assert "id=pickall" in body


def test_sidebar_is_read_only_when_anonymous() -> None:
    body = run(ui.skill_detail(make_request("/skills/alpha", path_params={"name": "alpha"}))).body.decode()
    assert "type=checkbox name=visible" not in body and "id=skillform" not in body


def test_save_returns_to_the_page_you_came_from(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    request = form_request(
        "/me/skills", [("visible", "beta"), ("next", "/skills/beta")], headers
    )
    response = run(ui.save_skills(request))
    assert response.status_code == 303
    assert response.headers["location"] == "/skills/beta"


def test_save_ignores_a_dangerous_next(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    request = form_request("/me/skills", [("visible", "beta"), ("next", "//evil.example")], headers)
    assert run(ui.save_skills(request)).headers["location"] == "/me/skills"


def test_manage_page_lists_private_skills(tmp_path, monkeypatch) -> None:
    from skills_mcp import private

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    private.save("alice", "my-note", "Use when testing.", "body", forbidden={"alpha", "beta"})
    body = run(ui.manage_skills(make_request("/me/skills", headers=headers))).body.decode()
    assert "value='my-note'" in body
    assert "private" in body


def test_saving_the_checkbox_set_stores_the_hidden_ones(tmp_path, monkeypatch) -> None:
    from skills_mcp import prefs

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    request = form_request("/me/skills", [("visible", "beta")], headers)   # alpha unchecked
    response = run(ui.save_skills(request))
    assert response.status_code == 303
    assert prefs.disabled("alice") == {"alpha"}


def test_unchecking_everything_hides_every_global_skill(tmp_path, monkeypatch) -> None:
    from skills_mcp import prefs, visibility

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    run(ui.save_skills(form_request("/me/skills", [], headers)))
    assert prefs.disabled("alice") == {"alpha", "beta"}
    assert visibility.visible_names("alice") == []


# ------------------------------------------------------------------ private skills


def test_create_a_private_skill_then_read_it(tmp_path, monkeypatch) -> None:
    from skills_mcp import visibility

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    request = form_request(
        "/me/private",
        [("name", "my-runbook"), ("description", "Use when the pager fires."), ("body", "# steps")],
        headers,
    )
    response = run(ui.private_create(request))
    assert response.status_code == 303
    assert "my-runbook" in visibility.visible_names("alice")
    assert "my-runbook" not in visibility.visible_names("bob")     # private, truly


def test_private_name_cannot_reuse_a_global_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    request = form_request(
        "/me/private",
        [("name", "alpha"), ("description", "d"), ("body", "b")],
        headers,
    )
    response = run(ui.private_create(request))
    assert response.status_code == 200                       # re-rendered, not saved
    assert "already used by a global skill" in response.body.decode()


def test_private_rejects_a_bad_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    request = form_request(
        "/me/private",
        [("name", "Bad Name!"), ("description", "d"), ("body", "b")],
        headers,
    )
    body = run(ui.private_create(request)).body.decode()
    assert "lowercase letters" in body


def test_edit_and_delete_a_private_skill(tmp_path, monkeypatch) -> None:
    from skills_mcp import private, visibility

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    headers = _trusted(monkeypatch)
    private.save("alice", "note", "first", "body one", forbidden={"alpha", "beta"})
    private.invalidate("alice")

    updated = form_request(
        "/me/private/note",
        [("description", "second"), ("body", "body two")],
        headers,
        path_params={"name": "note"},
    )
    assert run(ui.private_update(updated)).status_code == 303
    assert private.read("alice", "note")["description"] == "second"

    delete = make_request("/me/private/note/delete", headers=headers,
                          path_params={"name": "note"}, method="POST")
    assert run(ui.private_delete(delete)).status_code == 303
    assert "note" not in visibility.visible_names("alice")


# ------------------------------------------------------------------ header / sync


def test_header_offers_the_three_actions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    body = run(ui.skill_detail(
        make_request("/skills/alpha", headers=headers, path_params={"name": "alpha"})
    )).body.decode()
    assert "Sync now" in body
    assert "New Private Skill" in body
    assert "My keys" in body and "How to connect" in body
    assert "href='/me'" in body            # the identity pill links to /me


def test_header_hides_the_actions_when_anonymous() -> None:
    body = run(ui.skill_detail(make_request("/skills/alpha", path_params={"name": "alpha"}))).body.decode()
    assert "Sync now" not in body and "New Private Skill" not in body


def test_source_line_is_human_with_tooltips() -> None:
    from skills_mcp.state import Source

    class _S:
        source = Source(origin="github", ref="main", commit="edb1f6fa237f", refreshed_at=None)

    line = ui._source_line(_S())
    assert "GitHub" in line and "main" in line
    assert "edb1f6fa237f" in line          # full sha lives in the tooltip
    assert "edb1f6fa237f<" not in line      # not in the visible text


def test_ago_reads_like_english() -> None:
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    older = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat().replace("+00:00", "Z")
    assert ui._ago(recent).endswith("m ago")
    assert ui._ago(older) == "1d ago"


def test_first_run_banner_until_a_key_exists(tmp_path, monkeypatch) -> None:
    from skills_mcp import auth

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    body = run(ui.skill_detail(
        make_request("/skills/alpha", headers=headers, path_params={"name": "alpha"})
    )).body.decode()
    assert "You have no key yet" in body

    auth.mint("alice")
    body = run(ui.skill_detail(
        make_request("/skills/alpha", headers=headers, path_params={"name": "alpha"})
    )).body.decode()
    assert "You have no key yet" not in body


def test_sync_reports_the_result_on_the_next_page(tmp_path, monkeypatch) -> None:
    from skills_mcp import refresh

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    monkeypatch.setattr(
        refresh, "refresh_from_source",
        lambda: {"ok": True, "commit": "abc1234def", "count": 26, "ref": "main"},
    )
    request = form_request("/me/sync", [("next", "/skills/alpha")], headers)
    response = run(ui.me_sync(request))
    assert response.status_code == 303
    assert response.headers["location"] == "/skills/alpha"

    body = run(ui.skill_detail(
        make_request("/skills/alpha", headers=headers, path_params={"name": "alpha"})
    )).body.decode()
    assert "Synced" in body and "26 skills" in body and "abc1234" in body


def test_sync_failure_keeps_serving_and_says_so(tmp_path, monkeypatch) -> None:
    from skills_mcp import refresh

    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)

    def boom():
        raise refresh.RefreshError("proxy unreachable")

    monkeypatch.setattr(refresh, "refresh_from_source", boom)
    run(ui.me_sync(form_request("/me/sync", [("next", "/")], headers)))
    body = run(ui.skill_detail(
        make_request("/skills/alpha", headers=headers, path_params={"name": "alpha"})
    )).body.decode()
    assert "Sync failed" in body and "proxy unreachable" in body


def test_how_to_connect_page_renders_the_steps(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    headers = _trusted(monkeypatch)
    body = run(ui.how_to_connect(make_request("/how-to-connect", headers=headers))).body.decode()
    assert "How to connect" in body
    assert "Mint a key" in body and "gateway" in body.lower()
    assert "/mcp" in body
    assert "either or both" in body            # (a) gateway or (b) direct
    assert "Verify" not in body                # the curl step was removed
