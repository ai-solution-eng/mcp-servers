"""Relay attribution is NOT an authorization input by default.

Audit 2026-11-12 P1-5: with SQLHANDLER_RELAY_HMAC_SECRET unset, any holder of
a valid /mcp key could send X-MCP-Caller-Subject: <admin subject> and
admin_keys.is_admin() returned True - a read-only key could mint keys and
rewrite the policy document. The rung now fails CLOSED unless the operator
opts into the legacy posture explicitly.

Matrix pinned here:

* no secret, no opt-in      -> header REFUSED (the key-fingerprint rung wins)
* no secret, opt-in truthy  -> trusted (legacy), one loud warning
* secret + valid signature  -> trusted
* secret + bad/missing/stale signature -> refused
* no valid key              -> refused (unchanged: the rung stays key-gated)

Run:  python -m pytest tests/test_relay_attribution.py -v
"""

import hashlib
import hmac
import json
import time

import pytest

from sqlhandler import admin_keys, identity
from sqlhandler.policy import POLICY_ENABLED_ENV, POLICY_FILE_ENV, reset_policy_store

KEY_FP = "sha256:deadbeef1234"
SECRET = "relay-secret-1"


def _sig(secret, ts, subject, cls="user"):
    """The gateway's signature over identity.py's canonical string."""
    msg = chr(10).join([ts, subject, cls])
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _signed(subject, cls="user", ts=None, secret=SECRET):
    stamp = ts if ts is not None else str(int(time.time()))
    return [
        (b"x-mcp-caller-ts", stamp.encode()),
        (b"x-mcp-caller-sig", _sig(secret, stamp, subject, cls).encode()),
    ]


def _scope(subject="alice", extra=(), key_fp=KEY_FP):
    headers = [(b"x-mcp-caller-subject", subject.encode())] + list(extra)
    state = {"sqlhandler.key_fp": key_fp} if key_fp else {}
    return {"type": "http", "headers": headers, "state": state}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with no secret, no opt-in, no policy."""
    for name in (
        "SQLHANDLER_RELAY_HMAC_SECRET",
        "SQLHANDLER_TRUST_RELAY_HEADERS",
        POLICY_ENABLED_ENV,
        POLICY_FILE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(identity, "_relay_unsigned_warned", False)
    monkeypatch.setattr(identity, "_relay_optin_warned", False)
    reset_policy_store()
    yield
    reset_policy_store()


def _admins_policy(tmp_path, monkeypatch, admins):
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "g",
                "groups": {"g": {"hidden_tables": ["never/*"]}},
                "admins": admins,
            }
        )
    )
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    return pf


# ---------------------------------------------------------------------------
# the default: fail closed
# ---------------------------------------------------------------------------


def test_unsigned_header_refused_by_default():
    """No secret, no opt-in: the header is not trusted; the key rung wins."""
    assert identity.resolve_relay_caller(_scope(), key_valid=True) is None
    caller = identity.caller_from_scope(_scope())
    assert caller.cls == identity.CALLER_CLASS_KEY
    assert caller.subject is None
    assert caller.key_fp == KEY_FP
    assert caller.via == "key"


def test_unsigned_header_never_grants_admin(tmp_path, monkeypatch):
    """THE regression: a low-privilege key cannot claim an admin subject."""
    _admins_policy(tmp_path, monkeypatch, ["alice"])
    untrusted = identity.caller_from_scope(_scope(subject="alice"))
    assert untrusted.subject is None
    assert admin_keys.is_admin(untrusted) is False

    # Control: the SAME subject through a trusted rung IS the designated admin.
    monkeypatch.setenv("SQLHANDLER_TRUST_RELAY_HEADERS", "1")
    trusted = identity.caller_from_scope(_scope(subject="alice"))
    assert trusted.via == "relay" and trusted.subject == "alice"
    assert admin_keys.is_admin(trusted) is True


def test_unsigned_rejection_warns_once(caplog):
    with caplog.at_level("WARNING", logger="sqlhandler.identity"):
        identity.resolve_relay_caller(_scope(), key_valid=True)
        identity.resolve_relay_caller(_scope(), key_valid=True)
    hits = [r for r in caplog.records if "UNSIGNED relay attribution" in r.getMessage()]
    assert len(hits) == 1, "one loud warning per process, never per request"


# ---------------------------------------------------------------------------
# the explicit legacy opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_opt_in_trusts_the_header(monkeypatch, value):
    monkeypatch.setenv("SQLHANDLER_TRUST_RELAY_HEADERS", value)
    caller = identity.caller_from_scope(_scope())
    assert caller.via == "relay"
    assert caller.subject == "alice"
    assert caller.cls == identity.CALLER_CLASS_USER


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_opt_in_false_stays_closed(monkeypatch, value):
    monkeypatch.setenv("SQLHANDLER_TRUST_RELAY_HEADERS", value)
    assert identity.resolve_relay_caller(_scope(), key_valid=True) is None


def test_opt_in_warns_loudly_once(monkeypatch, caplog):
    monkeypatch.setenv("SQLHANDLER_TRUST_RELAY_HEADERS", "1")
    with caplog.at_level("WARNING", logger="sqlhandler.identity"):
        identity.resolve_relay_caller(_scope(), key_valid=True)
        identity.resolve_relay_caller(_scope(), key_valid=True)
    hits = [r for r in caplog.records if "UNSIGNED AND TRUSTED" in r.getMessage()]
    assert len(hits) == 1


def test_relay_headers_trusted_reads_the_env_mapping():
    assert identity.relay_headers_trusted({"SQLHANDLER_TRUST_RELAY_HEADERS": "1"}) is True
    assert identity.relay_headers_trusted({"SQLHANDLER_TRUST_RELAY_HEADERS": "off"}) is False
    assert identity.relay_headers_trusted({}) is False


# ---------------------------------------------------------------------------
# the HMAC posture is unchanged
# ---------------------------------------------------------------------------


def test_valid_signature_trusted(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RELAY_HMAC_SECRET", SECRET)
    caller = identity.caller_from_scope(_scope(extra=_signed("alice")))
    assert caller.via == "relay" and caller.subject == "alice"
    assert caller.key_fp == KEY_FP


def test_signature_for_another_subject_rejected(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RELAY_HMAC_SECRET", SECRET)
    # The gateway signed "mallory"; the header claims "alice".
    assert identity.resolve_relay_caller(_scope(extra=_signed("mallory")), key_valid=True) is None


def test_missing_signature_rejected(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RELAY_HMAC_SECRET", SECRET)
    assert identity.resolve_relay_caller(_scope(), key_valid=True) is None


def test_stale_timestamp_rejected(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RELAY_HMAC_SECRET", SECRET)
    old = str(int(time.time()) - identity.RELAY_SIG_MAX_SKEW_SECONDS - 5)
    assert identity.resolve_relay_caller(_scope(extra=_signed("alice", ts=old)), key_valid=True) is None


def test_signature_does_not_replace_the_key_gate():
    """A correctly-signed header on a KEYLESS request stays anonymous."""
    signed = _scope(extra=_signed("alice"), key_fp=None)
    assert identity.resolve_relay_caller(signed, key_valid=False) is None
