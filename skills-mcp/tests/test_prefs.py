"""Per-identity state: disabled list + minted keys (hash, label, expiry, revoke)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skills_mcp import prefs


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    yield


def iso(delta_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).isoformat().replace("+00:00", "Z")


def test_missing_state_is_empty() -> None:
    assert prefs.load("alice") == {}
    assert prefs.disabled("alice") == set()
    assert prefs.keys("alice") == []


def test_disable_and_reenable_round_trip() -> None:
    prefs.set_disabled("alice", "beta", True)
    assert prefs.disabled("alice") == {"beta"}
    prefs.set_disabled("alice", "beta", False)
    assert prefs.disabled("alice") == set()


def test_identity_is_filename_safe(tmp_path) -> None:
    prefs.set_disabled("Alice/../../etc", "beta", True)
    files = list((tmp_path / "state").glob("*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name and "/" not in files[0].name


# ------------------------------------------------------------------ keys


def test_key_is_stored_hashed_with_a_label() -> None:
    key = "sk_live_supersecretvalue"
    record = prefs.add_key("alice", key, label="laptop-dsh")
    assert record["label"] == "laptop-dsh"
    assert record["prefix"] == key[:14]
    assert "sha256" not in record            # the public view never exposes it
    raw = (Path(prefs.state_dir()) / "alice.json").read_text(encoding="utf-8")
    assert key not in raw                      # never in the clear
    assert "sha256" in raw


def test_many_keys_coexist() -> None:
    prefs.add_key("alice", "sk_live_one", label="one")
    prefs.add_key("alice", "sk_live_two", label="two")
    keys = prefs.keys("alice")
    assert len(keys) == 2
    assert {k["label"] for k in keys} == {"one", "two"}
    assert len({k["id"] for k in keys}) == 2    # ids are unique


def test_revoke_one_key_leaves_the_others() -> None:
    prefs.add_key("alice", "sk_live_one", label="one")
    prefs.add_key("alice", "sk_live_two", label="two")
    victim = prefs.keys("alice")[0]["id"]
    assert prefs.revoke_key("alice", victim) is True
    assert prefs.revoke_key("alice", victim) is False       # already revoked
    active = [k for k in prefs.keys("alice") if prefs.is_active(k)]
    assert len(active) == 1


def test_expiry_makes_a_key_inactive() -> None:
    prefs.add_key("alice", "sk_live_old", expires_at=iso(-1))
    prefs.add_key("alice", "sk_live_new", expires_at=iso(+1))
    active = [k for k in prefs.keys("alice") if prefs.is_active(k)]
    assert len(active) == 1


def test_rename_key() -> None:
    record = prefs.add_key("alice", "sk_live_one", label="old")
    assert prefs.rename_key("alice", record["id"], "new") is True
    assert prefs.keys("alice")[0]["label"] == "new"


def test_legacy_single_token_is_migrated(tmp_path) -> None:
    """v0.2.x wrote {"token": {...}}; the upgrade must not invalidate it."""
    import hashlib
    import json

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    legacy = {
        "token": {
            "sha256": hashlib.sha256(b"sk_live_legacy").hexdigest(),
            "prefix": "sk_live_legacy",
            "created_at": "2026-11-01T00:00:00Z",
        }
    }
    (state / "alice.json").write_text(json.dumps(legacy), encoding="utf-8")

    keys = prefs.keys("alice")
    assert len(keys) == 1
    assert keys[0]["id"] == "k_legacy"
    assert keys[0]["label"] == "default"
    assert prefs.is_active(keys[0])
    # the migration is persisted: the old field is gone
    assert "token" not in json.loads((state / "alice.json").read_text(encoding="utf-8"))


def test_writes_are_atomic_no_temp_file_left(tmp_path) -> None:
    prefs.add_key("alice", "sk_live_x")
    assert list((tmp_path / "state").glob("*.tmp")) == []
