"""Mask-spec validation: a mask is CODE, and a spec that does not compile must
never load - let alone degrade to the raw column at query time.

Audit 2026-11-12 P1-3: a mask of "sha256:12x" used to load cleanly (only
"non-empty string <= 256 chars" was checked) and then build_mask_select
swallowed the compile error per column and emitted the RAW column. These tests
pin both halves of the fix:

* the LOADER refuses a policy file whose mask cannot be compiled, naming the
  table/column/spec - and the broken-first sentinel still fails closed;
* the RUNTIME fallback renders the redact constant (never the raw column) and
  logs one warning naming the column.

Run:  python -m pytest tests/test_policy_mask_validation.py -v
"""

import json

import pytest

from sqlhandler import policy as policy_mod
from sqlhandler.policy import (
    POLICY_ENABLED_ENV,
    POLICY_FILE_ENV,
    PolicyError,
    build_mask_select,
    load_policy,
    policy_store,
    reset_policy_store,
)

#: Specs that CANNOT compile (the truncation branch raises PolicyError).
MALFORMED = ["sha256:12x", "sha256:0", "sha256:129", "sha256:"]

#: Specs that must keep loading exactly as before.
VALID = ["redact", "hash", "sha256", "sha256:12", "md5", "REDACTED-CUSTOMER", "'0'"]


def _write(tmp_path, mask, column="ssn", pattern="t/*"):
    """A minimal groups-form policy carrying ONE column mask."""
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "g",
                "groups": {"g": {"tables": {pattern: {"column_masks": {column: mask}}}}},
            }
        )
    )
    return pf


@pytest.mark.parametrize("mask", MALFORMED)
def test_malformed_mask_refused_at_load(tmp_path, mask):
    """The operator typo fails LOUDLY at load, naming table/column/spec."""
    with pytest.raises(PolicyError) as exc:
        load_policy(str(_write(tmp_path, mask)))
    msg = str(exc.value)
    assert "column mask" in msg, msg
    assert "ssn" in msg, msg
    assert repr(mask) in msg, "the offending spec must be named: " + msg


@pytest.mark.parametrize("mask", VALID)
def test_valid_mask_still_loads(tmp_path, mask):
    pol = load_policy(str(_write(tmp_path, mask)))
    assert pol.groups["g"]["tables"]["t/*"]["column_masks"]["ssn"] == mask


def test_mask_column_must_exist_when_the_catalog_is_known(tmp_path):
    """With real columns available, a mask naming an absent column refuses."""
    pf = _write(tmp_path, "redact", column="ssn")
    with pytest.raises(PolicyError, match="does not resolve"):
        load_policy(str(pf), table_columns={"t/one": ["id"]})
    assert load_policy(str(pf), table_columns={"t/one": ["id", "ssn"]}) is not None


def test_mask_column_may_live_on_any_covered_table(tmp_path):
    """A glob covering several tables passes when ANY of them carries it."""
    pf = _write(tmp_path, "redact", column="ssn", pattern="t/*")
    assert load_policy(str(pf), table_columns={"t/one": ["id"], "t/two": ["id", "ssn"]}) is not None


def test_runtime_fallback_renders_redact_never_the_raw_column():
    """The hot-reload-race fallback must not project the masked column bare."""
    sql = build_mask_select("base", ["id", "ssn", "email"], {"ssn": "sha256:12x"}, None)
    assert sql == "SELECT \"id\", '***' AS \"ssn\", \"email\" FROM base"
    projection = sql[len("SELECT ") : sql.index(" FROM ")].split(", ")
    assert '"ssn"' not in projection, "the masked column must never be projected bare: " + sql


def test_runtime_fallback_warns_once_per_column(caplog):
    policy_mod._MASK_FALLBACK_WARNED.clear()
    with caplog.at_level("WARNING", logger="sqlhandler.policy"):
        build_mask_select("base", ["ssn"], {"ssn": "sha256:0"}, None)
        build_mask_select("base", ["ssn"], {"ssn": "sha256:0"}, None)
    hits = [r for r in caplog.records if "column mask" in r.getMessage()]
    assert len(hits) == 1, "one warning per (column, spec), never per query"
    assert "ssn" in hits[0].getMessage()


def test_broken_first_file_with_a_bad_mask_still_fails_closed(tmp_path, monkeypatch):
    """A refused file must not weaken the broken-first sentinel (all hidden)."""
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(_write(tmp_path, "sha256:12x")))
    reset_policy_store()
    try:
        pol = policy_store().get()
        assert pol.source == "<broken>"
        groups = pol.groups_for("alice", None)
        assert pol.rule_for_table("t/one", "one", groups).hidden is True
    finally:
        reset_policy_store()
