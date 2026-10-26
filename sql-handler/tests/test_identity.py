"""Middleware ladder + identity-spine tests (Stage 1).

The resolution ladder per DECISIONS.md "OAuth REVISION (final)":
1. relay attribution headers (X-MCP-Caller-Subject + Class) — ONLY over a
   key-valid request;
2. oauth2-proxy headers — ONLY under SQLHANDLER_TRUST_BROWSER_HEADERS;
3. matched-key fingerprint — recorded additively by _McpApiKeyMiddleware;
4. anonymous.
Plus: the mcp-fleet-api-key experience is UNCHANGED (envs, headers, 401
posture), the vendored-copy sync check, the audit caller field, and the
caller-class metric.

Run:  python -m pytest tests/test_identity.py -v
"""

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from starlette.testclient import TestClient

from sqlhandler import identity
from sqlhandler.identity import (
    CALLER_CLASS_BROWSER,
    CALLER_CLASS_KEY,
    CALLER_CLASS_USER,
    RELAY_HMAC_SECRET_ENV,
    TRUST_BROWSER_HEADERS_ENV,
    Caller,
    match_api_key,
)
from sqlhandler.server import (
    _build_http_app,
    _CallerIdentityMiddleware,
    _McpApiKeyMiddleware,
)


def _sig(secret: str, ts: str, subject: str, cls: str) -> str:
    """The gateway's signature over identity.py's canonical string."""
    msg = f"{ts}\n{subject}\n{cls}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _signed(secret: str, subject: str, cls: str = "user", ts: str | None = None) -> list[tuple[bytes, bytes]]:
    """The (ts, sig) headers a correctly-signing gateway would stamp."""
    stamp = ts if ts is not None else str(int(time.time()))
    return [
        (b"x-mcp-caller-ts", stamp.encode("latin-1")),
        (b"x-mcp-caller-sig", _sig(secret, stamp, subject, cls).encode("latin-1")),
    ]


def _resolve_through_stack(
    monkeypatch,
    keys: str | None,
    headers: list[tuple[bytes, bytes]],
    secret: str | None = None,
):
    """Run one request through the REAL middleware order (key gate outer →
    identity inner) and return the resolved Caller + the inner scope state.
    This is the ladder exactly as production runs it.

    ``secret`` pins SQLHANDLER_RELAY_HMAC_SECRET for the request (None = the
    legacy, signature-less posture every pre-existing test asserts).
    """

    if keys is not None:
        monkeypatch.setenv("MCP_API_KEYS", keys)
    else:
        monkeypatch.delenv("MCP_API_KEYS", raising=False)
        monkeypatch.delenv("SQLHANDLER_API_KEYS", raising=False)
    if secret is not None:
        monkeypatch.setenv(RELAY_HMAC_SECRET_ENV, secret)
    else:
        monkeypatch.delenv(RELAY_HMAC_SECRET_ENV, raising=False)
    captured: list = []

    async def inner(scope, receive, send):
        captured.append((identity.caller_from_scope(scope), dict(scope.get("state") or {})))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass  # 401s flow here; nothing to assert on them in this helper

    stack = _McpApiKeyMiddleware(_CallerIdentityMiddleware(inner))
    scope = {"type": "http", "path": "/mcp", "headers": headers, "state": {}}
    asyncio.run(stack(scope, receive, send))
    if captured:
        return captured[0]
    # The key gate REFUSED the request (401) — it never reached the identity
    # middleware, so the resolved identity is "nothing": anonymous.
    return (Caller(cls="anonymous"), {})


@pytest.fixture()
def app(monkeypatch):
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", TRUST_BROWSER_HEADERS_ENV, RELAY_HMAC_SECRET_ENV):
        monkeypatch.delenv(var, raising=False)
    with TestClient(_build_http_app()) as client:
        yield client


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def _resolved(client, headers=None):
    """The Caller the middleware resolved for one request (via the state
    slot the inner middleware publishes — read through a tool call's scope)."""
    # Simplest observable: the identity middleware publishes into scope state;
    # TestClient cannot introspect it, so resolve directly from the scope:
    return


# ---------------------------------------------------------------------------
# the key middleware: fp recording is ADDITIVE, experience unchanged
# ---------------------------------------------------------------------------


def test_key_middleware_experience_unchanged(app, monkeypatch):
    """The hard constraint: same envs, same headers, same 401 posture."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    r = _ping(app)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert _ping(app, {"X-API-Key": "uni-k"}).status_code == 200
    assert _ping(app, {"Authorization": "Bearer uni-k"}).status_code == 200
    assert _ping(app, {"X-API-Key": "wrong"}).status_code == 401


def test_fp_recorded_into_scope_state(app, monkeypatch):
    """A matched key records its FINGERPRINT (never the key) into the state
    slot the identity middleware reads."""
    monkeypatch.setenv("MCP_API_KEYS", "secret-key-1")
    # Drive the middleware stack directly with a scope recorder.
    recorded_scopes = []

    async def app_spy(scope, receive, send):
        recorded_scopes.append(dict(scope.get("state") or {}))

    from sqlhandler.server import _CallerIdentityMiddleware

    # Build: key middleware OUTER, identity INNER, then the spy.
    key_mw = _McpApiKeyMiddleware(_CallerIdentityMiddleware(app_spy))
    import asyncio

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"x-api-key", b"secret-key-1")],
        "state": {},
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        pass

    asyncio.run(key_mw(scope, _receive, _send))
    fp = recorded_scopes[0].get(_McpApiKeyMiddleware.KEY_FP_STATE, "")
    assert fp.startswith("sha256:")
    assert "secret-key-1" not in fp, "the raw key never enters scope state (only its fingerprint)"


def test_no_keys_anonymous_resolution(app, monkeypatch):
    """No key gate: a request resolves anonymous (no rung has anything to
    anchor on — attribution headers are NOT trusted keyless)."""
    c, _state = _resolve_through_stack(
        monkeypatch, None, [(b"x-mcp-caller-subject", b"alice"), (b"x-mcp-caller-class", b"user")]
    )
    assert c.cls == "anonymous"


# ---------------------------------------------------------------------------
# rung 1 — relay attribution over a valid key
# ---------------------------------------------------------------------------


def test_relay_subject_over_valid_key(app, monkeypatch):
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
        ],
    )
    assert c.cls == CALLER_CLASS_USER
    assert c.subject == "alice"
    assert c.key_fp == "sha256:" + hashlib.sha256(b"k1").hexdigest()[:12]
    assert c.via == "relay"


def test_relay_subject_refused_without_key(app, monkeypatch):
    """Attribution-never-authorization: the SAME headers without a valid key
    resolve anonymous — a leaked header is worthless without the key."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
        ],
    )
    assert c.cls == "anonymous" and c.subject is None


def test_relay_subject_wrong_key_still_refused(app, monkeypatch):
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"NOT-VALID"),
            (b"x-mcp-caller-subject", b"alice"),
        ],
    )
    assert c.cls == "anonymous"


def test_relay_class_header_sanitized(app, monkeypatch):
    """An unknown/absent class header collapses to the bounded 'user' label;
    control characters in the subject are stripped (the gateway's sanitize)."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", "ali\x01ce".encode("latin-1")),
            (b"x-mcp-caller-class", b"totally-unknown-kind"),
        ],
    )
    assert c.subject == "alice"
    assert c.cls in ("user", "browser", "key", "anonymous")


# ---------------------------------------------------------------------------
# rung 1b — HMAC-VERIFIED relay attribution (opt-in via SQLHANDLER_RELAY_HMAC_SECRET)
#
# THE finding: key_valid proves a KEY matched, not that the GATEWAY wrote the
# attribution header — so without a proof any key holder could claim any
# subject, admins included. The secret-bearing deployment requires the
# signature; the secret-less deployment is byte-identical to the legacy trust.
# ---------------------------------------------------------------------------

SECRET = "relay-shared-secret-1"


def test_relay_signature_headers_helper():
    """The helper reads the two proof headers (or None when either is absent)."""
    scope = {"headers": [(b"x-mcp-caller-ts", b"1700000000"), (b"x-mcp-caller-sig", b"deadbeef")]}
    assert identity.relay_signature_headers(scope) == ("1700000000", "deadbeef")
    assert identity.relay_signature_headers({"headers": [(b"x-mcp-caller-ts", b"1")]}) is None
    assert identity.relay_signature_headers({"headers": [(b"x-mcp-caller-sig", b"s")]}) is None
    assert identity.relay_signature_headers({"headers": []}) is None


def test_relay_sig_valid_pure_function():
    """The unit-testable seam: canonical `ts\\nsubject\\ncls`, hex digest."""
    ts = str(int(time.time()))
    good = _sig(SECRET, ts, "alice", "user")
    assert identity._relay_sig_valid(SECRET, ts, "alice", "user", good)
    # Any component change invalidates it (binding to ALL THREE fields).
    assert not identity._relay_sig_valid(SECRET, ts, "admin", "user", good)
    assert not identity._relay_sig_valid(SECRET, ts, "alice", "browser", good)
    assert not identity._relay_sig_valid("other-secret", ts, "alice", "user", good)
    # Malformed inputs fail closed rather than raising.
    assert not identity._relay_sig_valid("", ts, "alice", "user", good)
    assert not identity._relay_sig_valid(SECRET, "not-a-number", "alice", "user", good)
    assert not identity._relay_sig_valid(SECRET, ts, "alice", "user", "")
    # A non-finite timestamp must never slip past the replay window
    # (float("nan") compares False against every bound without this guard).
    for bogus in ("nan", "inf", "-inf"):
        assert not identity._relay_sig_valid(SECRET, bogus, "alice", "user", _sig(SECRET, bogus, "alice", "user")), (
            bogus
        )


def test_relay_signed_subject_accepted_under_secret(app, monkeypatch):
    """The gateway's valid signature resolves the relay rung as before."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
            *_signed(SECRET, "alice"),
        ],
        secret=SECRET,
    )
    assert c.cls == CALLER_CLASS_USER
    assert c.subject == "alice"
    assert c.via == "relay"
    assert c.key_fp == identity.key_fp("k1")


def test_relay_wrong_signature_rejected(app, monkeypatch):
    """A key holder claiming someone ELSE's subject: signature mismatch →
    the ladder falls through to the pseudonymous key rung (not anonymous)."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"admin-user"),
            (b"x-mcp-caller-class", b"user"),
            (b"x-mcp-caller-ts", str(int(time.time())).encode()),
            (b"x-mcp-caller-sig", b"0" * 64),
        ],
        secret=SECRET,
    )
    assert c.cls == CALLER_CLASS_KEY, "forged attribution never resolves the relay rung"
    assert c.subject is None
    assert c.key_fp == identity.key_fp("k1")


def test_relay_signature_of_different_subject_rejected(app, monkeypatch):
    """Even a GENUINE signature is bound to its subject+class: replaying a
    valid alice signature on a request claiming `admin` must fail."""
    ts = str(int(time.time()))
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"admin"),
            (b"x-mcp-caller-class", b"user"),
            (b"x-mcp-caller-ts", ts.encode()),
            (b"x-mcp-caller-sig", _sig(SECRET, ts, "alice", "user").encode()),
        ],
        secret=SECRET,
    )
    assert c.cls == CALLER_CLASS_KEY and c.subject is None


def test_relay_stale_timestamp_rejected(app, monkeypatch):
    """Replay window: a signature older than 300 s is refused."""
    stale = str(int(time.time()) - 301)
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
            *_signed(SECRET, "alice", ts=stale),
        ],
        secret=SECRET,
    )
    assert c.cls == CALLER_CLASS_KEY, "stale proof is not a proof"


def test_relay_fresh_timestamp_within_skew_accepted(app, monkeypatch):
    """Just inside the window (299 s old) still verifies — the boundary is
    the documented 300 s, not something stricter."""
    fresh = str(int(time.time()) - 299)
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
            *_signed(SECRET, "alice", ts=fresh),
        ],
        secret=SECRET,
    )
    assert c.via == "relay" and c.subject == "alice"


def test_relay_missing_sig_headers_when_secret_set(app, monkeypatch):
    """Secret set + NO proof headers at all: None (the regression this fix
    exists for — the bare header is no longer self-attributing)."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
        ],
        secret=SECRET,
    )
    assert c.cls == CALLER_CLASS_KEY and c.subject is None


def test_relay_incomplete_proof_headers_when_secret_set(app, monkeypatch):
    """BOTH headers are required — a lone sig (or lone ts) is refused."""
    ts = str(int(time.time()))
    for partial in (
        [(b"x-mcp-caller-sig", _sig(SECRET, ts, "alice", "user").encode())],
        [(b"x-mcp-caller-ts", ts.encode())],
    ):
        c, _state = _resolve_through_stack(
            monkeypatch,
            "k1",
            [
                (b"x-api-key", b"k1"),
                (b"x-mcp-caller-subject", b"alice"),
                (b"x-mcp-caller-class", b"user"),
                *partial,
            ],
            secret=SECRET,
        )
        assert c.cls == CALLER_CLASS_KEY and c.subject is None, partial


def test_relay_secret_unset_trusts_header_unchanged(app, monkeypatch):
    """BACKWARD COMPATIBILITY (the hard constraint): with the secret UNSET,
    today's behavior is preserved exactly — bare relay headers over a valid
    key resolve the relay rung with no signature anywhere."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
        ],
        secret=None,
    )
    assert c.cls == CALLER_CLASS_USER and c.subject == "alice" and c.via == "relay"
    assert identity.relay_hmac_secret({"OTHER": "1"}) == ""
    monkeypatch.setenv(RELAY_HMAC_SECRET_ENV, "   ")
    assert identity.relay_hmac_secret() == "", "blank secret = unset (legacy trust)"


def test_relay_sig_ignored_when_secret_unset(app, monkeypatch):
    """A garbage signature is HARMLESS without the secret (never inspected)."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-sig", b"garbage"),
            (b"x-mcp-caller-ts", b"nonsense"),
        ],
        secret=None,
    )
    assert c.via == "relay" and c.subject == "alice"


def test_relay_verified_signature_still_key_gated(app, monkeypatch):
    """Signature verification does NOT replace the key gate — attribution is
    never authorization: a correctly-signed header keyless stays anonymous."""
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-mcp-caller-subject", b"alice"),
            (b"x-mcp-caller-class", b"user"),
            *_signed(SECRET, "alice"),
        ],
        secret=SECRET,
    )
    assert c.cls == "anonymous" and c.subject is None


def test_relay_header_names_exported():
    """The header constants are part of the module's public surface."""
    assert identity.HEADER_CALLER_SIG == "X-MCP-Caller-Sig"
    assert identity.HEADER_CALLER_TS == "X-MCP-Caller-Ts"
    assert identity.RELAY_HMAC_SECRET_ENV == "SQLHANDLER_RELAY_HMAC_SECRET"
    for name in (
        "HEADER_CALLER_SIG",
        "HEADER_CALLER_TS",
        "RELAY_HMAC_SECRET_ENV",
        "resolve_relay_caller",
        "relay_signature_headers",
    ):
        assert name in identity.__all__, name


# ---------------------------------------------------------------------------
# rung 2 — oauth2-proxy browser headers, trust-gated
# ---------------------------------------------------------------------------


def test_browser_headers_ignored_without_trust_flag(app, monkeypatch):
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-auth-request-user", b"webuser"),
            (b"x-forwarded-groups", b"analysts,viewers"),
        ],
    )
    assert c.cls == CALLER_CLASS_KEY, "no trust flag: headers ignored, key fp wins"


def test_browser_headers_resolved_under_trust_flag(app, monkeypatch):
    monkeypatch.setenv(TRUST_BROWSER_HEADERS_ENV, "1")
    c, _state = _resolve_through_stack(
        monkeypatch,
        "k1",
        [
            (b"x-api-key", b"k1"),
            (b"x-auth-request-user", b"webuser"),
            (b"x-forwarded-groups", b"analysts, viewers"),
        ],
    )
    assert c.cls == CALLER_CLASS_BROWSER
    assert c.subject == "webuser"
    assert c.groups == ("analysts", "viewers")
    assert c.via == "browser"


def test_browser_headers_keyless_under_trust_flag(app, monkeypatch):
    """Trust flag set, NO key: the browser rung OPENS (the UI path — the
    workload AuthorizationPolicy pins ingress to the gateway, which
    authenticated the browser session; the /ui + /api surfaces resolve the
    same Caller per review §5). This is the flag's entire purpose: trust is
    the deployment's, asserted by the operator's AuthorizationPolicy, not
    per-request credentials."""
    monkeypatch.setenv(TRUST_BROWSER_HEADERS_ENV, "1")
    c, _state = _resolve_through_stack(monkeypatch, None, [(b"x-auth-request-user", b"webuser")])
    assert c.cls == CALLER_CLASS_BROWSER and c.subject == "webuser"


def test_browser_headers_keyless_without_trust_flag(app, monkeypatch):
    """Same keyless request WITHOUT the flag: anonymous (headers ignored —
    a pod reachable without the gateway pin must never trust them)."""
    c, _state = _resolve_through_stack(monkeypatch, None, [(b"x-auth-request-user", b"webuser")])
    assert c.cls == "anonymous"


# ---------------------------------------------------------------------------
# rung 3/4 — key fp and anonymous
# ---------------------------------------------------------------------------


def test_key_fp_rung(app, monkeypatch):
    c, _state = _resolve_through_stack(monkeypatch, "solo", [(b"x-api-key", b"solo")])
    assert c.cls == CALLER_CLASS_KEY
    assert c.subject is None  # pseudonymous by design
    assert c.key_fp == identity.key_fp("solo")


def test_stdio_anonymous():
    assert identity.caller_from_scope({"type": "stdio"}).cls == "anonymous"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_match_api_key_constant_time_match():
    keys = ["alpha", "beta", "gamma"]
    assert match_api_key("beta", keys) == "beta"
    assert match_api_key("nope", keys) is None
    assert match_api_key("", keys) is None
    assert match_api_key("beta", []) is None


# ---------------------------------------------------------------------------
# audit + metrics (additive observability)
# ---------------------------------------------------------------------------


def test_audit_query_caller_field(tmp_path, monkeypatch):
    from sqlhandler import observability

    monkeypatch.setenv("SQLHANDLER_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    alice = Caller(cls="user", subject="alice", key_fp="sha256:abc")
    observability.audit_query("SELECT 1", "ok", 1.0, 1, None, caller=alice)
    line = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert line["caller"] == {"class": "user", "subject": "alice", "key_fp": "sha256:abc"}

    # No caller → the field is ABSENT (byte-shape of the old lines kept).
    observability.audit_query("SELECT 2", "ok", 1.0, 1, None)
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert "caller" not in json.loads(lines[1])


def test_caller_class_metric_bounded():
    from sqlhandler import observability

    m = observability.metrics
    before = dict(m.caller_queries.snapshot())
    m.record_caller_query("user")
    m.record_caller_query("browser")
    m.record_caller_query("WEIRD-UNBOUNDED-LABEL")
    snap = m.caller_queries.snapshot()
    assert snap["user"] == before.get("user", 0) + 1
    assert snap["browser"] == before.get("browser", 0) + 1
    assert snap["anonymous"] >= before.get("anonymous", 0) + 1  # unknown → anonymous
    assert "WEIRD-UNBOUNDED-LABEL" not in snap, "labels stay in the closed vocabulary"


def test_metrics_render_includes_caller_series():
    from sqlhandler import observability

    observability.metrics.record_caller_query("key")
    text = observability.metrics.render(None)
    assert "sqlhandler_caller_queries_total{caller_class=" in text
    # and the historical series still render byte-shape:
    assert "# HELP sqlhandler_queries_total" in text


# ---------------------------------------------------------------------------
# vendored-copy drift check (the adoption protocol's gate, as a TEST)
# ---------------------------------------------------------------------------


def test_mcp_fleet_common_copy_in_sync():
    """fleet_common_sync.sh --check equivalent, in-suite: every file in the
    vendored copy matches MANIFEST.sha256 (drift = the copy was edited by
    hand — re-run the sync script instead)."""
    import hashlib
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "src" / "sqlhandler" / "mcp_fleet_common"
    manifest = pkg / "MANIFEST.sha256"
    assert manifest.exists(), "vendored package missing MANIFEST.sha256 (run fleet_common_sync.sh)"
    entries = {}
    for line in manifest.read_text().splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            entries[name.strip()] = digest
    assert entries, "empty manifest"
    for name, digest in entries.items():
        f = pkg / name
        assert f.exists(), f"manifest lists {name} but the file is missing"
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        assert got == digest, f"DRIFT in vendored {name}: re-run fleet_common_sync.sh"
    # AND: no unmanaged extra .py files (a hand-dropped module would be drift).
    shipped = {n for n in entries}
    for f in pkg.glob("*.py"):
        assert f.name in shipped, f"unmanaged extra file in the vendored copy: {f.name}"


def test_nested_import_shim():
    """The vendored package imports through the nested (sqlhandler-qualified)
    location; the shim in __init__ keeps both import paths working."""
    from sqlhandler.mcp_fleet_common import audit as nested_audit

    assert nested_audit.AUDIT_GENESIS == "0" * 64
    assert callable(nested_audit.key_fingerprint)
