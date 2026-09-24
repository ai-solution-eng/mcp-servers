"""Unit tests for the extended Iceberg catalog types (glue | hive | nessie).

No network anywhere: config parsing is pure env-dict work, and _catalog()
branch selection is verified by mocking ``load_catalog`` (asserting the exact
kwargs each catalog type passes to pyiceberg) plus the graceful missing-dep
translation pyiceberg raises when boto3/thrift are absent (they are, in this
venv — asserted up front so the intent of those tests stays explicit).
"""

import pytest

import sqlhandler.iceberg as iceberg_mod
from sqlhandler.config import IcebergConfig, load_iceberg_config
from sqlhandler.iceberg import IcebergProvider
from sqlhandler.provider import LakehouseError

try:  # the glue/hive extras are expected to be absent in the dev venv
    import boto3  # noqa: F401

    HAVE_BOTO3 = True
except ImportError:
    HAVE_BOTO3 = False


# ---------------------------------------------------------------- config env


def test_catalog_type_garbage_falls_back_to_rest():
    """An unknown ICEBERG_CATALOG_TYPE falls back to rest (existing behavior)."""
    cfg = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "sponge", "ICEBERG_CATALOG_URI": "http://x"})
    assert cfg.catalog_type == "rest"
    assert cfg.is_configured


def test_catalog_type_case_and_space_normalized():
    cfg = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "  GLUE ", "ICEBERG_WAREHOUSE": "s3://b/wh"})
    assert cfg.catalog_type == "glue"


def test_glue_needs_nothing_but_the_aws_environment():
    cfg = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "glue"})
    assert cfg.is_configured  # no URI required; AWS_* env is boto3's business
    assert not load_iceberg_config({}).is_configured  # default rest still needs a URI


def test_hive_requires_thrift_uri():
    ok = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "hive", "ICEBERG_CATALOG_URI": "thrift://hms:9083"})
    assert ok.is_configured
    assert ok.catalog_uri == "thrift://hms:9083"
    bad = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "hive"})
    assert not bad.is_configured  # hive with no thrift URI is not configured


def test_nessie_uri_and_ref_flow():
    cfg = load_iceberg_config(
        {
            "ICEBERG_CATALOG_TYPE": "nessie",
            "ICEBERG_CATALOG_URI": "http://nessie:19120/api/iceberg",
            "ICEBERG_NESSIE_REF": "etl-branch",
        }
    )
    assert cfg.catalog_type == "nessie"
    assert cfg.is_configured
    assert cfg.nessie_ref == "etl-branch"
    assert not load_iceberg_config({"ICEBERG_CATALOG_TYPE": "nessie"}).is_configured


def test_is_configured_matrix():
    assert not IcebergConfig(catalog_type="glue").is_configured is False  # glue: True
    assert IcebergConfig(catalog_type="glue").is_configured
    assert IcebergConfig(catalog_type="hive", catalog_uri="thrift://h:9083").is_configured
    assert not IcebergConfig(catalog_type="hive", catalog_uri="").is_configured
    assert IcebergConfig(catalog_type="nessie", catalog_uri="http://n/iceberg").is_configured
    assert not IcebergConfig(catalog_type="nessie", catalog_uri="").is_configured
    assert IcebergConfig(catalog_type="rest", catalog_uri="http://r").is_configured
    assert IcebergConfig(catalog_type="sql", catalog_uri="sqlite:///x.db").is_configured
    # The dataclass itself still refuses a bogus type it was built with directly.
    assert not IcebergConfig(catalog_type="bogus", catalog_uri="http://x").is_configured


# ------------------------------------------------------------- nessie URI


def test_nessie_uri_ref_appended():
    p = IcebergProvider(
        IcebergConfig(catalog_type="nessie", catalog_uri="http://n:19120/api/iceberg", nessie_ref="main")
    )
    assert p._nessie_uri() == "http://n:19120/api/iceberg/main"


def test_nessie_uri_no_ref_is_passthrough():
    p = IcebergProvider(IcebergConfig(catalog_type="nessie", catalog_uri="http://n:19120/api/iceberg/"))
    assert p._nessie_uri() == "http://n:19120/api/iceberg"


def test_nessie_uri_pre_pinned_ref_wins():
    """A URI already carrying /iceberg/<ref> is honored; the env ref is ignored."""
    p = IcebergProvider(
        IcebergConfig(catalog_type="nessie", catalog_uri="http://n:19120/api/iceberg/prod", nessie_ref="main")
    )
    assert p._nessie_uri() == "http://n:19120/api/iceberg/prod"


def test_nessie_uri_ref_with_warehouse_suffix_kept():
    """<ref>|<warehouse> URI suffixes are left alone (projectnessie convention)."""
    uri = "http://n:19120/api/iceberg/main|s3://wh"
    p = IcebergProvider(IcebergConfig(catalog_type="nessie", catalog_uri=uri, nessie_ref="dev"))
    assert p._nessie_uri() == uri


# --------------------------------------------------- _catalog branch kwargs


class _Captured(Exception):
    """Internal marker raised by the fake load_catalog.

    _catalog() deliberately wraps every construction error into a
    LakehouseError, so tests assert on ``LakehouseError`` AND the captured
    kwargs (the marker text rides along in the message).
    """


@pytest.fixture()
def capture_load_catalog(monkeypatch):
    """Patch load_catalog where _catalog imports it from; capture the kwargs."""
    calls: dict = {}

    def fake_load_catalog(name, **kwargs):
        calls["name"] = name
        calls.update(kwargs)
        raise _Captured("captured")

    monkeypatch.setattr("pyiceberg.catalog.load_catalog", fake_load_catalog)
    return calls


def _provider(**over) -> IcebergProvider:
    return IcebergProvider(IcebergConfig(**over))


def _run_branch(capture_load_catalog, **over) -> dict:
    """Run a _catalog() branch; return the kwargs load_catalog received.

    Asserts the provider translated the (fake) failure into a LakehouseError
    — the same wrapping real catalog errors get — and returns the kwargs for
    the per-type assertions.
    """
    with pytest.raises(LakehouseError, match="captured"):
        _provider(**over)._catalog()
    return capture_load_catalog


def test_rest_branch_kwargs(capture_load_catalog):
    kw = _run_branch(
        capture_load_catalog,
        catalog_type="rest",
        catalog_uri="http://rest:8181",
        catalog_token="tok",
        warehouse="s3://wh",
    )
    assert kw["name"] == "default"
    assert kw["type"] == "rest"
    assert kw["uri"] == "http://rest:8181"
    assert kw["token"] == "tok"
    assert kw["warehouse"] == "s3://wh"


def test_glue_branch_kwargs_no_credentials(capture_load_catalog):
    kw = _run_branch(capture_load_catalog, catalog_type="glue", warehouse="s3://b/wh")
    assert kw["type"] == "glue"
    assert kw["warehouse"] == "s3://b/wh"
    # NO credential values ever reach pyiceberg for glue.
    assert "uri" not in kw
    assert "token" not in kw
    assert not any("aws_access" in k or "secret" in k for k in kw)


def test_hive_branch_kwargs(capture_load_catalog):
    kw = _run_branch(capture_load_catalog, catalog_type="hive", catalog_uri="thrift://hms:9083", warehouse="s3://wh")
    assert kw["type"] == "hive"
    assert kw["uri"] == "thrift://hms:9083"
    assert kw["warehouse"] == "s3://wh"


def test_nessie_branch_is_rest_with_ref_uri(capture_load_catalog):
    kw = _run_branch(
        capture_load_catalog,
        catalog_type="nessie",
        catalog_uri="http://n:19120/api/iceberg",
        nessie_ref="etl",
        catalog_token="tok",
    )
    assert kw["type"] == "rest"  # nessie rides the REST catalog type
    assert kw["uri"] == "http://n:19120/api/iceberg/etl"
    assert kw["token"] == "tok"


def test_sql_branch_untouched(capture_load_catalog):
    """sql keeps its direct SqlCatalog path (no load_catalog call)."""
    p = _provider(catalog_type="sql", catalog_uri="sqlite:///x.db", catalog_name="c")
    p._cat = object()  # pretend it is built; only assert _catalog returns it
    assert p._catalog() is p._cat


# ------------------------------------------------- graceful missing deps


@pytest.mark.skipif(HAVE_BOTO3, reason="boto3 installed; missing-dep path not reachable")
def test_glue_missing_dep_translates_to_lakehouse_error():
    """Without boto3, pyiceberg's NotInstalledError becomes a LakehouseError
    that names the pip extra (graceful, actionable — never a raw traceback)."""
    with pytest.raises(LakehouseError) as ei:
        _provider(catalog_type="glue")._catalog()
    assert "pyiceberg[glue]" in str(ei.value)


@pytest.mark.skipif(HAVE_BOTO3, reason="thrift may be present when boto3 is")
def test_hive_missing_dep_translates_to_lakehouse_error():
    if HAVE_BOTO3:
        pytest.skip("thrift present")
    try:
        import thrift  # noqa: F401

        pytest.skip("thrift installed")
    except ImportError:
        pass
    with pytest.raises(LakehouseError) as ei:
        _provider(catalog_type="hive", catalog_uri="thrift://hms:9083")._catalog()
    assert "pyiceberg[hive]" in str(ei.value)


def test_pyiceberg_native_registry_documents_our_types():
    """The types we advertise must exist in the INSTALLED pyiceberg registry
    (nessie intentionally absent — it rides rest; guard against pyiceberg
    growing a native 'nessie' silently changing our branch semantics)."""
    from pyiceberg.catalog import CatalogType

    native = {t.value for t in CatalogType}
    assert {"rest", "sql", "glue", "hive"} <= native
    assert "nessie" not in native  # our nessie branch is the REST+URI convention
    assert set(IcebergConfig.KNOWN_CATALOG_TYPES) - {"nessie"} <= native


def test_pyiceberg_glue_hive_gated_by_import(monkeypatch):
    """Document (by exercising) that pyiceberg gates glue behind its boto3
    import — the NotInstalledError our except-clause translates."""
    from pyiceberg.catalog import load_catalog

    if HAVE_BOTO3:
        monkeypatch.setattr(
            iceberg_mod, "_GLUE_MISSING_MSG", iceberg_mod._GLUE_MISSING_MSG
        )  # no-op; keeps the import meaningful
    try:
        load_catalog("x", type="glue", warehouse="s3://b/wh")
        raised = None
    except Exception as exc:  # NotInstalledError when boto3 absent
        raised = exc
    if not HAVE_BOTO3:
        assert raised is not None and "pyiceberg[glue]" in str(raised)
    else:
        assert raised is None or "glue" in str(raised).lower()
