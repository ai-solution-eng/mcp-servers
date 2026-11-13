"""Bootstrap admin: with NO policy document, the configured API key IS the admin.

Live 2026-11-12: a fresh deployment had a working key but no way to reach the
Access-control panel — the policy named a fingerprint that no configured key
matched, and the default posture was "no policy => nobody administers". The
fix is scoped deliberately:

* NO policy document enforcing (disabled, or enabled with no file yet) ->
  a KEY-authenticated caller is the administrator, with one loud WARNING;
* a policy that IS enforcing never bootstraps — an empty or stale 'admins'
  list stays fail-closed (a typo'd fingerprint must never grant admin);
* subjects never bootstrap (only the credential the operator created does);
* ``SQLHANDLER_BOOTSTRAP_ADMIN=0`` restores the strict posture.

The 403 body also names the CAUSE now, so an operator can tell "no policy"
from "no admins entry" from "not on the list".

Run: python -m pytest tests/test_bootstrap_admin.py -v
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sqlhandler import admin_keys as _admin_keys_mod
from sqlhandler import server as server_module
from sqlhandler.admin_keys import (
    BOOTSTRAP_ADMIN_ENV,
    bootstrap_admin_active,
    bootstrap_admin_enabled,
    is_admin,
)
from sqlhandler.identity import Caller
from sqlhandler.mcp_fleet_common.audit import key_fingerprint
from sqlhandler.policy import POLICY_ENABLED_ENV, POLICY_FILE_ENV, reset_policy_store

ADMIN_KEY = "bootstrap-admin-key-0123456789abcdef"
ADMIN_FP = key_fingerprint(ADMIN_KEY)
OTHER_FP = "sha256:000000000000"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Every test starts policy- and bootstrap-clean (module flag included)."""
    for env in (
        BOOTSTRAP_ADMIN_ENV,
        POLICY_ENABLED_ENV,
        POLICY_FILE_ENV,
        "MCP_API_KEYS",
        "SQLHANDLER_API_KEYS",
    ):
        monkeypatch.delenv(env, raising=False)
    _admin_keys_mod._bootstrap_warned = False
    reset_policy_store()
    yield
    reset_policy_store()


def _key_caller(fp: str = ADMIN_FP) -> Caller:
    return Caller(cls="key", subject=None, key_fp=fp, via="key")


def _policy_file(tmp_path: Path, spec: dict) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _enable(monkeypatch, path: Path) -> None:
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(path))
    reset_policy_store()


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------


def test_env_switch_defaults_on_and_parses_off():
    assert bootstrap_admin_enabled({}) is True, "unset must default ON (fresh installs need an admin)"
    for off in ("0", "false", "FALSE", "no", "off"):
        assert bootstrap_admin_enabled({BOOTSTRAP_ADMIN_ENV: off}) is False, off
    for on in ("1", "true", "yes", "on"):
        assert bootstrap_admin_enabled({BOOTSTRAP_ADMIN_ENV: on}) is True, on


# ---------------------------------------------------------------------------
# the fallback
# ---------------------------------------------------------------------------


def test_no_policy_key_caller_is_admin():
    assert bootstrap_admin_active() is True
    assert is_admin(_key_caller()) is True


def test_env_off_restores_fail_closed(monkeypatch):
    monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "0")
    assert bootstrap_admin_active() is False
    assert is_admin(_key_caller()) is False


def test_subject_and_anonymous_never_bootstrap():
    assert is_admin(Caller(cls="user", subject="alice", via="jwt")) is False
    assert is_admin(Caller()) is False
    assert is_admin(None) is False


def test_policy_with_admins_never_bootstraps(tmp_path, monkeypatch):
    _enable(monkeypatch, _policy_file(tmp_path, {"admins": [ADMIN_FP]}))
    assert bootstrap_admin_active() is False
    assert is_admin(_key_caller()) is True
    assert is_admin(_key_caller(OTHER_FP)) is False


def test_loaded_policy_without_admins_stays_closed(tmp_path, monkeypatch):
    """A policy that IS enforcing is never overridden by the fallback."""
    _enable(monkeypatch, _policy_file(tmp_path, {"groups": {"g": {}}, "default_group": "g"}))
    assert bootstrap_admin_active() is False
    assert is_admin(_key_caller()) is False


def test_broken_policy_never_bootstraps(tmp_path, monkeypatch):
    """Present-but-invalid fails CLOSED — the key must not become admin."""
    path = tmp_path / "policy.json"
    path.write_text("{not json", encoding="utf-8")
    _enable(monkeypatch, path)
    assert bootstrap_admin_active() is False
    assert is_admin(_key_caller()) is False


def test_bootstrap_warning_is_logged_once(caplog):
    caplog.set_level(logging.WARNING, logger="sqlhandler.admin_keys")
    assert is_admin(_key_caller()) is True
    assert is_admin(_key_caller()) is True
    hits = [r for r in caplog.records if "BOOTSTRAP ADMIN ACTIVE" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in hits]


# ---------------------------------------------------------------------------
# the 403 bodies name the cause (never the designated identities)
# ---------------------------------------------------------------------------


def test_403_no_policy_bootstrap_disabled(monkeypatch):
    monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, "0")
    reason = server_module._admin_forbidden_reason(_key_caller())
    assert "bootstrap fallback is disabled" in reason


def test_403_loaded_policy_without_admins(tmp_path, monkeypatch):
    _enable(monkeypatch, _policy_file(tmp_path, {"groups": {"g": {}}, "default_group": "g"}))
    reason = server_module._admin_forbidden_reason(_key_caller())
    assert "designates no admins" in reason


def test_403_not_on_the_list_names_own_fingerprint_only(tmp_path, monkeypatch):
    _enable(monkeypatch, _policy_file(tmp_path, {"admins": ["sha256:aaaaaaaaaaaa", "subject:alice"]}))
    reason = server_module._admin_forbidden_reason(_key_caller())
    assert ADMIN_FP in reason, "the caller may see their OWN fingerprint"
    assert "sha256:aaaaaaaaaaaa" not in reason, "the admins list must never be echoed"
    assert "alice" not in reason, "the admins list must never be echoed"


def test_403_broken_policy_says_invalid(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text("{not json", encoding="utf-8")
    _enable(monkeypatch, path)
    reason = server_module._admin_forbidden_reason(_key_caller())
    assert "INVALID" in reason


# ---------------------------------------------------------------------------
# probe visibility
# ---------------------------------------------------------------------------


def test_ready_reports_bootstrap_admin_while_active(monkeypatch):
    """The posture rides /ready (where policy posture already lives); /health
    stays byte-identical — its shape is pinned by the probe contracts."""
    monkeypatch.setenv("MCP_API_KEYS", ADMIN_KEY)
    monkeypatch.setenv("SQLHANDLER_READINESS_CHECK", "0")
    app = server_module._build_http_app()
    with TestClient(app) as c:
        body = c.get("/ready").json()
    assert "bootstrap_admin" in body
    assert "configured API key is the administrator" in body["bootstrap_admin"]


def test_ready_hides_bootstrap_admin_when_a_policy_enforces(tmp_path, monkeypatch):
    _enable(monkeypatch, _policy_file(tmp_path, {"admins": [ADMIN_FP]}))
    monkeypatch.setenv("MCP_API_KEYS", ADMIN_KEY)
    monkeypatch.setenv("SQLHANDLER_READINESS_CHECK", "0")
    app = server_module._build_http_app()
    with TestClient(app) as c:
        body = c.get("/ready").json()
    assert "bootstrap_admin" not in body
