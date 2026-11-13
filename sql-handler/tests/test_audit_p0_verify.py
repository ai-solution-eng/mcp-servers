"""Independent P0 verification -- verifier tests written from the FINDINGS.

NOT written from the implementers' patches: each assertion below restates the
observable requirement from the audit findings, so a green suite means the
BEHAVIOUR is fixed, not that a particular implementation exists.

P0-1 (masking bypass): a policy-covered table's RAW pyarrow dataset is
registered on the query connection under a private name
(engine._register_schema -> "__sqlhandler_base_<n>") and the masking view is
built over it. The caller's SQL runs on that SAME connection, so the private
name is addressable. Requirement: any SQL naming the internal base relation is
REFUSED, while the ordinary masked path still returns "***" and the
row-filtered count.

P0-2 (federation): every load_*_config(env) treats the passed dict as a
FALLBACK (_getenv reads os.environ first), so in a helm deployment
(SQLHANDLER_BACKEND=onelake is rendered unconditionally) every per-source
field is silently shadowed by the global environment. Requirement: per-source
values win, keys the source omits still come from the environment, the no-arg
call keeps working, and the sources-path S3_USE_SSL default is true.
"""

import json

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import load_source_providers
from sqlhandler.engine import SqlEngine
from sqlhandler.identity import Caller
from sqlhandler.policy import POLICY_ENABLED_ENV, POLICY_FILE_ENV, reset_policy_store
from sqlhandler.provider import DataProvider, TableInfo

TABLES = [TableInfo(name="work_order", schema="workorder", format="parquet")]


class PolicyProvider(DataProvider):
    """Fake provider with policy-relevant columns (ssn = the leak canary)."""

    kind = "fake"

    def __init__(self, root):
        self.root = root

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return "fake://" + info.path

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return None


def _seed(root):
    d = root / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": pa.array(range(3), type=pa.int64()),
                "ssn": ["ssn-000", "ssn-001", "ssn-002"],
                "amount": [10.0, 20.0, 30.0],
                "kind": ["a", "secret", "a"],
            }
        ),
        d / "part.parquet",
    )


def _make_engine(tmp_path):
    root = tmp_path / "data"
    _seed(root)
    return SqlEngine(PolicyProvider(root), cache_ttl=3600, dataset_cache_ttl=0)


ALICE = Caller(cls="user", subject="alice", key_fp="sha256:aaaaaaaaaaaa", via="relay")
CAROL_UNMASKED = Caller(cls="key", subject=None, key_fp="sha256:cccccccccccc", via="key")

SQL_MASKED = "SELECT id, ssn, kind FROM work_order ORDER BY id"

# Every shape the finding says must be refused: the join the lead reproduced,
# the case variant, the quoted identifier, and the minimal direct read.
BYPASS_SQL = [
    "SELECT b.ssn FROM work_order o JOIN __sqlhandler_base_1 b ON true",
    "SELECT b.ssn FROM work_order o JOIN __SQLHANDLER_BASE_1 b ON true",
    'SELECT b.ssn FROM work_order o JOIN "__sqlhandler_base_1" b ON true',
    "SELECT ssn, kind FROM __sqlhandler_base_1",
]


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    """Enforcement ON: alice is masked + row-filtered, carol is unmasked.

    Mirrors the finding's fixture exactly (default_group analysts, subject
    alice -> analysts, the empty 'unrestricted' group bound to carol's key
    fingerprint). Result caching is off so no cache entry can mask a bypass.
    """
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "0")
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "analysts",
                "groups": {
                    "analysts": {
                        "tables": {
                            "workorder/work_order": {
                                "row_filter": "kind != 'secret'",
                                "column_masks": {"ssn": "redact"},
                            }
                        }
                    },
                    "unrestricted": {},
                },
                "subjects": {"alice": ["analysts"]},
                "key_fps": {"sha256:cccccccccccc": ["unrestricted"]},
            }
        )
    )
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    yield pf
    reset_policy_store()


# ---------------------------------------------------------------------------
# P0-1 -- masking: the internal base relation must not be addressable
# ---------------------------------------------------------------------------


def test_control_unmasked_caller_still_sees_raw_data(tmp_path, policy_env):
    """CONTROL: proves the policy is live, so a refusal below is not vacuous.

    Without this, a broken fixture (policy not loaded) would make every bypass
    test 'pass' because the base relation would never exist.
    """
    eng = _make_engine(tmp_path)
    t = eng.query_duckdb(SQL_MASKED, caller=CAROL_UNMASKED)
    assert t.num_rows == 3
    assert sorted(t.column("ssn").to_pylist()) == ["ssn-000", "ssn-001", "ssn-002"]


def test_masked_path_unchanged(tmp_path, policy_env):
    """No regression: alice still gets the redaction and the row filter."""
    eng = _make_engine(tmp_path)
    t = eng.query_duckdb(SQL_MASKED, caller=ALICE)
    assert t.num_rows == 2, "row_filter must still drop the 'secret' row"
    assert t.column("ssn").to_pylist() == ["***", "***"], "masked ssn must be redacted"
    assert "secret" not in t.column("kind").to_pylist()


@pytest.mark.parametrize("sql", BYPASS_SQL)
def test_internal_base_relation_is_refused(tmp_path, policy_env, sql):
    """P0-1: naming the raw base relation must never return raw values."""
    eng = _make_engine(tmp_path)
    try:
        t = eng.query_duckdb(sql, caller=ALICE)
    except Exception as exc:  # refusal is the expected outcome
        assert "ssn-00" not in str(exc), "the refusal message must not leak raw values"
        return
    pytest.fail("MASKING BYPASS: " + repr(sql) + " returned rows: " + repr(t.to_pylist()))


# ---------------------------------------------------------------------------
# P0-2 -- federation: per-source values must beat a polluted environment
# ---------------------------------------------------------------------------

_POLLUTED_KEYS = [
    "SQLHANDLER_BACKEND",
    "SQLHANDLER_SOURCES",
    "S3_BUCKET",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_REGION",
    "S3_USE_SSL",
    "S3_PREFIX",
    "S3_ANONYMOUS",
    "S3_FORMAT",
    "S3_SESSION_TOKEN",
    "S3_PATH_STYLE",
]


@pytest.fixture()
def clean_env(monkeypatch):
    """Hermetic S3/backend env: each test opts in to exactly what it needs."""
    for key in _POLLUTED_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _pollute_chart_style(monkeypatch):
    """The env the helm chart produces: SQLHANDLER_BACKEND rendered globally."""
    monkeypatch.setenv("SQLHANDLER_BACKEND", "onelake")
    monkeypatch.setenv("S3_BUCKET", "GLOBAL-BUCKET")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://global.example:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "GLOBAL-AK")
    monkeypatch.setenv("S3_SECRET_KEY", "GLOBAL-SK")
    monkeypatch.setenv("S3_REGION", "us-east-1")


def _load(monkeypatch, source, *, via_environ=False):
    payload = json.dumps([source])
    if via_environ:
        monkeypatch.setenv("SQLHANDLER_SOURCES", payload)
        mp = load_source_providers()
    else:
        mp = load_source_providers({"SQLHANDLER_SOURCES": payload})
    assert mp is not None, "load_source_providers returned None"
    return mp


def test_per_source_backend_and_fields_beat_polluted_environ(clean_env, monkeypatch):
    """P0-2 core: backend=s3 + its own bucket/endpoint/keys must win."""
    _pollute_chart_style(monkeypatch)
    source = {
        "name": "sales",
        "backend": "s3",
        "bucket": "bucket-a",
        "endpointUrl": "https://minio.corp:9443",
        "accessKey": "AK-A",
        "secretKey": "SK-A",
        "region": "ap-south-1",
        "useSsl": True,
    }
    try:
        mp = _load(monkeypatch, source)
    except Exception as exc:
        pytest.fail(
            "resolved to the wrong backend instead of the s3 source: "
            + type(exc).__name__
            + ": "
            + str(exc)[:300]
        )
    assert mp.sources == ["sales"]
    provider = mp.providers[0]
    assert provider.kind == "s3", "per-source backend=s3 must not be shadowed by SQLHANDLER_BACKEND"
    cfg = provider.config
    assert cfg.bucket == "bucket-a"
    assert cfg.endpoint_url == "https://minio.corp:9443"
    assert cfg.access_key == "AK-A"
    assert cfg.secret_key == "SK-A"
    assert cfg.region == "ap-south-1"


def test_source_omitted_key_falls_back_to_environment(clean_env, monkeypatch):
    """P0-2: explicit-override, not explicit-only -- absent keys still come from env."""
    _pollute_chart_style(monkeypatch)
    monkeypatch.setenv("S3_REGION", "eu-central-1")
    source = {
        "name": "sales",
        "backend": "s3",
        "bucket": "bucket-a",
        "endpointUrl": "https://minio.corp:9443",
    }
    mp = _load(monkeypatch, source)
    cfg = mp.providers[0].config
    assert cfg.bucket == "bucket-a", "the per-source key still wins"
    assert cfg.region == "eu-central-1", "an omitted source key must fall back to the environment"
    assert cfg.access_key == "GLOBAL-AK", "omitted credentials must fall back to the environment"
    assert cfg.secret_key == "GLOBAL-SK"


def test_no_arg_call_still_reads_sources_from_environ(clean_env, monkeypatch):
    """P0-2: env=None keeps working (the production call site takes no argument)."""
    source = {
        "name": "sales",
        "backend": "s3",
        "bucket": "bucket-a",
        "endpointUrl": "https://minio.corp:9443",
        "accessKey": "AK-A",
        "secretKey": "SK-A",
    }
    mp = _load(monkeypatch, source, via_environ=True)
    assert mp.sources == ["sales"]
    provider = mp.providers[0]
    assert provider.kind == "s3"
    assert provider.config.bucket == "bucket-a"


def test_sources_use_ssl_defaults_true(clean_env, monkeypatch):
    """P0-2: a sources entry WITHOUT useSsl must not downgrade to plaintext http."""
    source = {
        "name": "sales",
        "backend": "s3",
        "bucket": "bucket-a",
        "endpointUrl": "minio.corp:9000",
        "accessKey": "AK-A",
        "secretKey": "SK-A",
    }
    mp = _load(monkeypatch, source)
    provider = mp.providers[0]
    assert provider.config.use_ssl is True, "sources-path S3_USE_SSL must default to true"
    assert provider._endpoint_override() == "https://minio.corp:9000"


def test_sources_use_ssl_false_is_honored(clean_env, monkeypatch):
    """P0-2: an explicit useSsl:false (plain-http MinIO) still works."""
    source = {
        "name": "sales",
        "backend": "s3",
        "bucket": "bucket-a",
        "endpointUrl": "minio.corp:9000",
        "accessKey": "AK-A",
        "secretKey": "SK-A",
        "useSsl": False,
    }
    mp = _load(monkeypatch, source)
    provider = mp.providers[0]
    assert provider.config.use_ssl is False
    assert provider._endpoint_override() == "http://minio.corp:9000"
