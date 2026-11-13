"""The keys store's four states - the /mcp gate's input.

Audit 2026-11-12 P1-4: a corrupt keys file and an empty one both returned []
from _load, so the gate could not tell "nothing minted yet" (the documented
bootstrap state) from "authentication is broken", and it disarmed on both.
store_state() makes the distinction explicit:

    disabled | ok | empty | unreadable

These tests pin the mapping, the keep-last-good-contents behavior, and the
recovery path (a good write clears the unreadable marker).

Run:  python -m pytest tests/test_keys_store_state.py -v
"""

import json

import pytest

from sqlhandler.admin_keys import (
    ADMIN_KEYS_FILE_ENV,
    STORE_DISABLED,
    STORE_EMPTY,
    STORE_OK,
    STORE_UNREADABLE,
    add_key,
    list_keys,
    store_state,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ADMIN_KEYS_FILE_ENV, raising=False)
    yield


def _path(tmp_path, payload=None):
    p = tmp_path / "admin-keys.json"
    if payload is not None:
        p.write_text(payload)
    return p


def test_disabled_store(monkeypatch):
    assert store_state() == STORE_DISABLED == "disabled"
    assert list_keys() == []


def test_missing_file_is_empty_not_unreadable(tmp_path, monkeypatch):
    """Configured-but-never-created is the bootstrap state, not a failure."""
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(_path(tmp_path)))
    assert store_state() == STORE_EMPTY
    assert list_keys() == []


def test_ok_after_a_mint(tmp_path, monkeypatch):
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(_path(tmp_path)))
    entry = add_key("raw-key-one", label="t", created_by="t")
    assert store_state() == STORE_OK
    assert [e["fp"] for e in list_keys()] == [entry["fp"]]


def test_valid_but_empty_store(tmp_path, monkeypatch):
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(_path(tmp_path, json.dumps({"keys": []}))))
    assert store_state() == STORE_EMPTY
    assert list_keys() == []


def test_corrupt_json_is_unreadable_not_empty(tmp_path, monkeypatch):
    """THE regression: a truncated file must never look like "no keys minted"."""
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(_path(tmp_path, "{ this is not json")))
    assert store_state() == STORE_UNREADABLE
    assert list_keys() == []


def test_wrong_shape_is_unreadable(tmp_path, monkeypatch):
    """Valid JSON that is not the store's shape is unusable, not empty."""
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(_path(tmp_path, json.dumps(["not", "a", "store"]))))
    assert store_state() == STORE_UNREADABLE


def test_unreadable_keeps_the_last_good_contents(tmp_path, monkeypatch):
    p = _path(tmp_path)
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(p))
    entry = add_key("raw-key-one", label="t", created_by="t")
    assert store_state() == STORE_OK

    p.write_text("{ truncated")
    assert store_state() == STORE_UNREADABLE
    assert [e["fp"] for e in list_keys()] == [entry["fp"]], "the last valid contents keep serving"


def test_a_good_write_recovers_the_state(tmp_path, monkeypatch):
    p = _path(tmp_path, "{ truncated")
    monkeypatch.setenv(ADMIN_KEYS_FILE_ENV, str(p))
    assert store_state() == STORE_UNREADABLE

    add_key("raw-key-two", label="t", created_by="t")
    assert store_state() == STORE_OK
    assert len(list_keys()) == 1


def test_states_are_the_documented_vocabulary():
    assert {STORE_DISABLED, STORE_OK, STORE_EMPTY, STORE_UNREADABLE} == {"disabled", "ok", "empty", "unreadable"}
