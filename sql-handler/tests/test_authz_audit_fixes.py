"""P2/P3 authz audit fixes (2026-11-12 wave 2).

Covers, in the order the findings were fixed:

1. keyed hash column masks (SQLHANDLER_MASK_HASH_SECRET) + the unkeyed warning
   and the cache-key fold;
2. the sharing action-stream per-LINE byte cap;
3. the presigned-URL fetch policy (scheme + private-range SSRF guard, the
   sharing endpoint's own origin exception) and the never-echo-the-body rule;
4. COPY refused as a scratch write (its payload is the COPY itself);
5. OIDC audience: aud is required, azp is additional;
6. SSO discovery fetched outside the lock (one fetch for concurrent callers);
7. sharing local staging: 0o700 dir + a per-file byte cap;
8. the mask hash grammar refuses unsupported algorithms instead of silently
   substituting (sha1 used to become md5, sha512 became sha256, "256" meant
   sha256, "md5:8" became a literal);
9. _glob_match is case-insensitive BY DESIGN (pinned; changing it would
   silently revoke grants).

Run:  python -m pytest tests/test_authz_audit_fixes.py -v
"""

import json
import stat
import threading

import pytest

from sqlhandler import oidc_identity as oidc
from sqlhandler import oidc_sso, policy, sharing, writes
from sqlhandler.identity import Caller
from sqlhandler.policy import PolicyError, build_mask_select, load_policy

NL = bytes([10])


def _mask_policy(tmp_path, mask="hash"):
    """A minimal groups-form policy carrying ONE column mask."""
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "g",
                "groups": {"g": {"tables": {"t/*": {"column_masks": {"ssn": mask}}}}},
                "subjects": {"alice": ["g"]},
            }
        )
    )
    return pf


# ---------------------------------------------------------------------------
# 1. keyed hash masks
# ---------------------------------------------------------------------------


def test_hash_mask_unkeyed_is_the_plain_digest(monkeypatch):
    monkeypatch.delenv(policy.MASK_HASH_SECRET_ENV, raising=False)
    assert build_mask_select("b", ["ssn"], {"ssn": "hash"}, None) == 'SELECT sha256("ssn") AS "ssn" FROM b'


def test_hash_mask_keyed_when_the_secret_is_set(monkeypatch):
    monkeypatch.setenv(policy.MASK_HASH_SECRET_ENV, "s3cr3t")
    sql = build_mask_select("b", ["ssn"], {"ssn": "hash"}, None)
    assert sql == "SELECT sha256(CAST(\"ssn\" AS VARCHAR) || 's3cr3t') AS \"ssn\" FROM b"
    # every supported algorithm keys, and truncation still applies
    assert "md5(CAST(\"ssn\" AS VARCHAR) || 's3cr3t')" in build_mask_select("b", ["ssn"], {"ssn": "md5"}, None)
    assert "substring(sha256(CAST(\"ssn\" AS VARCHAR) || 's3cr3t'), 1, 8)" in build_mask_select(
        "b", ["ssn"], {"ssn": "sha256:8"}, None
    )


def test_the_key_moves_the_effective_policy_hash(tmp_path, monkeypatch):
    """A keyed digest must never share a cache entry with an unkeyed one."""
    pf = _mask_policy(tmp_path)
    monkeypatch.delenv(policy.MASK_HASH_SECRET_ENV, raising=False)
    pol = load_policy(str(pf))
    unkeyed = pol.effective_hash("alice", None)
    monkeypatch.setenv(policy.MASK_HASH_SECRET_ENV, "k1")
    keyed = pol.effective_hash("alice", None)
    monkeypatch.setenv(policy.MASK_HASH_SECRET_ENV, "k2")
    rotated = pol.effective_hash("alice", None)
    assert unkeyed and keyed and rotated
    assert len({unkeyed, keyed, rotated}) == 3


def test_unkeyed_hash_masks_warn_once_at_load(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(policy.MASK_HASH_SECRET_ENV, raising=False)
    monkeypatch.setattr(policy, "_UNKEYED_MASK_WARNED", False)
    pf = _mask_policy(tmp_path)
    with caplog.at_level("WARNING", logger="sqlhandler.policy"):
        load_policy(str(pf))
        load_policy(str(pf))
    hits = [r for r in caplog.records if "UNKEYED hash column masks" in r.getMessage()]
    assert len(hits) == 1, "one warning per process, not per load"
    assert policy.MASK_HASH_SECRET_ENV in hits[0].getMessage()


def test_keyed_hash_masks_do_not_warn(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(policy.MASK_HASH_SECRET_ENV, "k")
    monkeypatch.setattr(policy, "_UNKEYED_MASK_WARNED", False)
    with caplog.at_level("WARNING", logger="sqlhandler.policy"):
        load_policy(str(_mask_policy(tmp_path)))
    assert not [r for r in caplog.records if "UNKEYED" in r.getMessage()]


# ---------------------------------------------------------------------------
# 8. the mask hash grammar: supported specs, and refusals instead of
#    silent substitutions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,fragment",
    [
        ("hash", 'sha256("ssn")'),
        ("sha256", 'sha256("ssn")'),
        ("sha1", 'sha1("ssn")'),  # used to be silently md5
        ("md5", 'md5("ssn")'),
        ("sha256:12", 'substring(sha256("ssn"), 1, 12)'),
        ("md5:8", 'substring(md5("ssn"), 1, 8)'),  # used to be a literal constant
        ("sha1:4", 'substring(sha1("ssn"), 1, 4)'),
        ("redact", "'***'"),
        ("0", "'0'"),
    ],
)
def test_supported_mask_grammar(spec, fragment, monkeypatch):
    monkeypatch.delenv(policy.MASK_HASH_SECRET_ENV, raising=False)
    assert fragment in build_mask_select("b", ["ssn"], {"ssn": spec}, None)


@pytest.mark.parametrize("spec", ["sha512", "sha224", "sha384", "256", "512", "sha:8", "sha512:8"])
def test_unsupported_hash_algorithms_are_refused(tmp_path, spec):
    with pytest.raises(PolicyError, match="unsupported algorithm"):
        policy._mask_sql("ssn", spec, {"ssn"})
    with pytest.raises(PolicyError, match="unsupported algorithm"):
        load_policy(str(_mask_policy(tmp_path, spec)))


# ---------------------------------------------------------------------------
# 9. _glob_match is case-insensitive by design
# ---------------------------------------------------------------------------


def test_glob_match_is_case_insensitive_both_ways():
    """Pinned: grants are case-insensitive (changing it would revoke grants)."""
    assert policy._glob_match("public_*", "PUBLIC_PII") is True
    assert policy._glob_match("Payroll*", "payroll") is True
    assert policy._glob_match("workorder/*", "workorder/orders") is True
    assert policy._glob_match("workorder/*", "sales/orders") is False
    assert policy._glob_match("", "anything") is False


# ---------------------------------------------------------------------------
# 2. the action-stream per-line byte cap
# ---------------------------------------------------------------------------


class _CappedResp:
    """A minimal HTTPResponse stand-in with readline(limit) semantics."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0

    def readline(self, limit=-1):
        if self._pos >= len(self._payload):
            return b""
        end = self._payload.find(NL, self._pos)
        end = len(self._payload) if end == -1 else end + 1
        if limit is not None and limit >= 0:
            end = min(end, self._pos + limit)
        chunk = self._payload[self._pos : end]
        self._pos = end
        return chunk


def test_action_line_cap_rejects_an_oversized_line():
    resp = _CappedResp(b"x" * (sharing._MAX_ACTION_BYTES + 10))
    with pytest.raises(sharing.LakehouseError, match="longer than"):
        list(sharing._iter_action_lines(resp, sharing._MAX_ACTION_BYTES))


def test_action_line_cap_allows_a_final_line_without_a_newline():
    resp = _CappedResp(b'{"a": 1}')
    assert list(sharing._iter_action_lines(resp, 1024)) == [b'{"a": 1}']


def test_consume_stream_rejects_an_oversized_line():
    client = sharing.SharingClient.__new__(sharing.SharingClient)
    big = b'{"add": {"url": "x", "size": 1}}' + b" " * (sharing._MAX_ACTION_BYTES + 5)
    with pytest.raises(sharing.LakehouseError, match="longer than"):
        client._consume_query_stream(_CappedResp(big))


# ---------------------------------------------------------------------------
# 3. the presigned-URL fetch policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://h/f", "s3://bucket/key", ""])
def test_presigned_url_schemes_are_refused(url):
    with pytest.raises(sharing.LakehouseError, match="scheme"):
        sharing._validate_presigned_url(url)


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "10.1.2.3", "192.168.1.5", "::1"])
def test_presigned_private_hosts_are_refused(host, monkeypatch):
    monkeypatch.delenv(sharing.ALLOW_PRIVATE_SHARING_URLS_ENV, raising=False)
    url = f"http://[{host}]/f.parquet" if ":" in host else f"http://{host}/f.parquet"
    with pytest.raises(sharing.LakehouseError, match="non-public address"):
        sharing._validate_presigned_url(url)


def test_presigned_private_hosts_allowed_by_the_explicit_opt_in(monkeypatch):
    monkeypatch.setenv(sharing.ALLOW_PRIVATE_SHARING_URLS_ENV, "1")
    sharing._validate_presigned_url("http://127.0.0.1:9000/bucket/f.parquet")
    sharing._validate_presigned_url("http://10.0.0.5/f.parquet")


def test_the_sharing_endpoints_own_origin_is_allowed(monkeypatch):
    """Some on-prem servers hand out file URLs on themselves (proxy)."""
    monkeypatch.delenv(sharing.ALLOW_PRIVATE_SHARING_URLS_ENV, raising=False)
    sharing._validate_presigned_url("http://127.0.0.1:8080/dl/f", "http://127.0.0.1:8080")
    with pytest.raises(sharing.LakehouseError, match="non-public address"):
        sharing._validate_presigned_url("http://127.0.0.1:9999/dl/f", "http://127.0.0.1:8080")


def test_presigned_fetch_error_never_echoes_the_body():
    class _Resp:
        status = 403

        def read(self, n=-1):
            return b"SECRET-INTERNAL-DATA"

        def close(self):
            pass

    class _Conn:
        def request(self, *a, **k):
            pass

        def getresponse(self):
            return _Resp()

    f = sharing._HTTPRandomAccessFile("https://files.example/f", 5, None)
    f._conn = _Conn()
    with pytest.raises(OSError) as exc:
        f._open_at(0)
    message = str(exc.value)
    assert "SECRET-INTERNAL-DATA" not in message
    assert "withheld" in message


# ---------------------------------------------------------------------------
# 4. COPY is refused as a scratch write
# ---------------------------------------------------------------------------


def test_copy_is_refused_as_a_scratch_write():
    c = writes.classify_sql("COPY (SELECT 1) TO 'out.parquet'")[0]
    assert c.kind == writes.CLASS_WRITE_OTHER
    assert c.target == "out.parquet", "the target is still parsed for the message"
    assert "CREATE TABLE" in c.reason
    with pytest.raises(writes.WriteError) as exc:
        writes.assert_writable_sql("COPY (SELECT 1) TO 'out.parquet'", Caller(cls="user", subject="alice"))
    assert "COPY" in str(exc.value) and "CREATE TABLE" in str(exc.value)


def test_copy_target_parsing_is_unchanged():
    """The decoy/quote handling still works (it feeds the refusal message)."""
    decoy = "COPY /* TO '/srv/scratch/alice/decoy.parquet' */ (SELECT 1 AS c) TO '/etc/cron.d/pwn'"
    assert writes.classify_sql(decoy)[0].target == "/etc/cron.d/pwn"


# ---------------------------------------------------------------------------
# 5. OIDC audience: aud required, azp additional
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims,expected",
    [
        ({"aud": "ua"}, True),
        ({"aud": ["ua", "other"]}, True),
        ({"aud": "ua", "azp": "ua"}, True),
        ({"aud": ["other"], "azp": "ua"}, False),  # THE regression: azp alone
        ({"aud": "ua", "azp": "other"}, False),  # azp must also match
        ({"aud": ["other"]}, False),
        ({}, False),
        ({"aud": 7}, False),
    ],
)
def test_audience_requires_aud_and_treats_azp_as_additional(claims, expected, monkeypatch):
    monkeypatch.setenv(oidc.AUDIENCE_ENV, "ua")
    assert oidc._audience_matches(claims) is expected


# ---------------------------------------------------------------------------
# 6. SSO discovery: one fetch for concurrent callers
# ---------------------------------------------------------------------------


def test_sso_discovery_fetches_once_for_concurrent_callers(monkeypatch):
    calls = []
    started = threading.Event()
    release = threading.Event()
    doc = {"authorization_endpoint": "http://idp/auth", "token_endpoint": "http://idp/token"}

    def slow_fetch(url, timeout):
        calls.append(url)
        started.set()
        release.wait(2.0)
        return doc

    monkeypatch.setenv(oidc_sso.PROVIDER_URL_ENV, "http://idp.test/.well-known/openid-configuration")
    monkeypatch.delenv(oidc_sso.AUTHZ_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(oidc_sso.TOKEN_ENDPOINT_ENV, raising=False)
    monkeypatch.setattr(oidc_sso, "_fetch_discovery", slow_fetch)
    with oidc_sso._discovery_lock:
        oidc_sso._discovery_cache.clear()
        oidc_sso._discovery_negative.clear()
        oidc_sso._discovery_fetching.clear()

    worker = threading.Thread(target=oidc_sso.provider_config)
    worker.start()
    try:
        assert started.wait(2.0), "the first caller must start the fetch"
        # A concurrent caller must NOT block on the in-flight fetch and must
        # not start a second one (no cached document yet -> decline).
        assert oidc_sso.provider_config() is None
    finally:
        release.set()
        worker.join(3.0)
    assert len(calls) == 1
    assert oidc_sso.provider_config() == ("http://idp/auth", "http://idp/token")


# ---------------------------------------------------------------------------
# 7. sharing local staging: private dir + per-file cap
# ---------------------------------------------------------------------------


def test_staging_dir_is_created_private(tmp_path, monkeypatch):
    monkeypatch.setattr(sharing, "_download_to", lambda url, target, max_bytes=None: target.write_bytes(b"x"))
    dest = tmp_path / "stage"
    out = sharing._stage_files_locally([{"url": "https://h/f", "size": 1}], dest)
    assert out and out[0].exists()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


def test_staged_download_is_capped_and_cleans_up(tmp_path, monkeypatch):
    class _Fake:
        def __init__(self, *a, **k):
            self._chunks = [b"x" * 10, b"y" * 10, b""]

        def read(self, n=-1):
            return self._chunks.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(sharing, "_HTTPRandomAccessFile", lambda *a, **k: _Fake())
    target = tmp_path / "f.parquet"
    with pytest.raises(sharing.LakehouseError, match="exceeded its"):
        sharing._download_to("https://h/f", target, max_bytes=15)
    assert not target.exists(), "a failed download must not leave a partial file"
