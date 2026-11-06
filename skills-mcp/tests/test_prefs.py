"""Per-identity state: disabled list + hashed token, on the PVC."""

from pathlib import Path

import pytest

from skills_mcp import prefs


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_PRIVATE_DIR", str(tmp_path / "private"))
    yield


def test_missing_state_is_empty() -> None:
    assert prefs.load("alice") == {}
    assert prefs.disabled("alice") == set()
    assert prefs.token_record("alice") is None


def test_disable_and_reenable_round_trip() -> None:
    prefs.set_disabled("alice", "beta", True)
    prefs.set_disabled("alice", "gamma", True)
    assert prefs.disabled("alice") == {"beta", "gamma"}
    prefs.set_disabled("alice", "beta", False)
    assert prefs.disabled("alice") == {"gamma"}


def test_identities_are_isolated() -> None:
    prefs.set_disabled("alice", "beta", True)
    assert prefs.disabled("bob") == set()


def test_identity_is_filename_safe(tmp_path) -> None:
    prefs.set_disabled("Alice/../../etc", "beta", True)
    files = list((tmp_path / "state").glob("*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name and "/" not in files[0].name


def test_token_is_stored_hashed_never_in_the_clear() -> None:
    key = "sk_live_supersecretvalue"
    record = prefs.set_token("alice", key)
    assert record["sha256"] != key
    assert len(record["sha256"]) == 64
    assert record["prefix"] == key[:14]
    raw = (Path(prefs.state_dir()) / "alice.json").read_text(encoding="utf-8")
    assert key not in raw


def test_clear_token() -> None:
    prefs.set_token("alice", "sk_live_x")
    assert prefs.clear_token("alice") is True
    assert prefs.token_record("alice") is None
    assert prefs.clear_token("alice") is False


def test_writes_are_atomic_no_temp_file_left(tmp_path) -> None:
    prefs.set_disabled("alice", "beta", True)
    assert list((tmp_path / "state").glob("*.tmp")) == []
