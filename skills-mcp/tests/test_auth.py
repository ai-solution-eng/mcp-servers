"""Authentication: env keys (operator) union minted keys (PVC), plus SSO header."""

import hashlib
from pathlib import Path

import pytest

from skills_mcp import auth, prefs


def scope_with(header: str, value: str) -> dict:
    return {"type": "http", "headers": [(header.lower().encode(), value.encode())]}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLS_MCP_API_KEYS", "base-key-123")
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    monkeypatch.delenv("SKILLS_MCP_CLIENTS", raising=False)
    monkeypatch.delenv("SKILLS_TRUST_PROXY_IDENTITY", raising=False)
    yield


# ------------------------------------------------------------------ env keys


def test_operator_base_key_resolves_anonymously() -> None:
    found = auth.resolve(scope_with("x-api-key", "base-key-123"))
    assert found is not None
    assert found.name is None
    assert found.source == "env-base"


def test_named_env_key_resolves_the_name(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_MCP_CLIENTS", "alice:keyA;bob:keyB")
    found = auth.resolve(scope_with("x-api-key", "keyA"))
    assert found.name == "alice"
    assert found.source == "env-user"


def test_bearer_header_is_accepted() -> None:
    found = auth.resolve(scope_with("authorization", "Bearer base-key-123"))
    assert found is not None


def test_unknown_key_is_anonymous() -> None:
    assert auth.resolve(scope_with("x-api-key", "nope")) is None


def test_no_key_is_anonymous() -> None:
    assert auth.resolve({"type": "http", "headers": []}) is None


# ------------------------------------------------------------------ minted


def test_minted_key_resolves_its_identity() -> None:
    key = auth.mint("carol")
    found = auth.resolve(scope_with("x-api-key", key))
    assert found.name == "carol"
    assert found.source == "minted"


def test_rotating_a_token_kills_the_old_one() -> None:
    first = auth.mint("carol")
    second = auth.mint("carol")
    assert auth.resolve(scope_with("x-api-key", first)) is None
    assert auth.resolve(scope_with("x-api-key", second)).name == "carol"


def test_env_key_wins_over_a_minted_key(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_MCP_CLIENTS", "alice:keyA")
    found = auth.resolve(scope_with("x-api-key", "keyA"))
    assert found.source == "env-user"


def test_minted_index_is_cached_but_refreshes(tmp_path) -> None:
    auth.minted_index()
    key = auth.mint("dave")
    assert auth.resolve(scope_with("x-api-key", key)).name == "dave"


# ------------------------------------------------------------------ sso


def test_sso_header_is_ignored_by_default() -> None:
    scope = scope_with("x-forwarded-user", "alice@corp")
    assert auth.resolve(scope) is None


def test_sso_header_resolves_when_trusted(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_TRUST_PROXY_IDENTITY", "true")
    found = auth.resolve(scope_with("x-forwarded-user", "Alice@Corp"))
    assert found.name == "alice@corp"
    assert found.source == "sso"


def test_a_key_beats_the_sso_header(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_TRUST_PROXY_IDENTITY", "true")
    monkeypatch.setenv("SKILLS_MCP_CLIENTS", "alice:keyA")
    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"keyA"), (b"x-forwarded-user", b"mallory@corp")],
    }
    assert auth.resolve(scope).name == "alice"


# ------------------------------------------------------------------ helpers


def test_env_managed_names(monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_MCP_CLIENTS", "alice:keyA;bob:keyB")
    assert auth.env_managed_names() == {"alice", "bob"}


def test_fingerprint_is_short_and_stable() -> None:
    one = auth._fingerprint("abc")
    assert one == auth._fingerprint("abc")
    assert one.startswith("sha256:") and len(one) == len("sha256:") + 12
    assert one != auth._fingerprint("abd")
