"""Query/fairness caps must be WIRED, not merely accepted by the values files.

Renders the REAL chart and pins:
* the documented defaults reach the pod (ConfigMap data, consumed via envFrom);
* the optional per-caller keys are ABSENT by default (empty value = the
  server's "= the global cap" default) and render when set;
* the values defaults match the code defaults, so a render without overrides
  is behaviour-identical to an unconfigured image.

Run: python -m pytest tests/test_query_env_wiring.py -v
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "helm"

#: env name -> the chart's default value (must match the code defaults).
DEFAULT_CAPS = {
    "SQLHANDLER_MAX_CONCURRENT_QUERIES": "8",
    "SQLHANDLER_QUEUE_TIMEOUT": "30",
    "SQLHANDLER_CANCEL_GRACE_SECONDS": "5",
    "SQLHANDLER_MAX_ROWS": "1000",
    "SQLHANDLER_MAX_OUTPUT_ROWS": "1000",
    "SQLHANDLER_MAX_RESULT_BYTES": "100000",
    "SQLHANDLER_MAX_PAGE_ROWS": "1000",
    "SQLHANDLER_MAX_SCOPES": "256",
    "SQLHANDLER_MAX_JOBS": "8",
    "SQLHANDLER_MAX_ASYNC_QUERIES": "100",
    "SQLHANDLER_ASK_MAX_TABLES": "5",
    "SQLHANDLER_ASK_MAX_COLUMNS": "20",
    "SQLHANDLER_ASK_MAX_PROFILE_COLUMNS": "6",
    "SQLHANDLER_EXPORT_DEFAULT_ROWS": "10000",
    "SQLHANDLER_EXPORT_MAX_ROWS": "100000",
    "SQLHANDLER_PROFILE_MAX_ROWS": "1000000",
    "SQLHANDLER_PROFILE_CACHE_SIZE": "512",
    "SQLHANDLER_SINGLEFLIGHT": "1",
    "SQLHANDLER_FAIRNESS_SCOPE": "identity",
}

#: optional per-caller keys: absent by default (empty = "= the global cap").
OPTIONAL_CAPS = (
    "SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER",
    "SQLHANDLER_MAX_JOBS_PER_CALLER",
    "SQLHANDLER_MAX_ASYNC_QUERIES_PER_CALLER",
    "SQLHANDLER_SINGLEFLIGHT_TIMEOUT",
)


def _helm():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm not installed")
    if not CHART.is_dir():
        pytest.skip("helm chart not present")
    return helm


def _render(*extra):
    out = subprocess.run(
        [_helm(), "template", "sqlhandler", str(CHART), *extra],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _configmap(docs):
    cm = next(
        d for d in docs if d.get("kind") == "ConfigMap" and d.get("metadata", {}).get("name") == "sqlhandler-config"
    )
    return cm.get("data") or {}


def test_cap_envs_render_the_documented_defaults():
    data = _configmap(_render())
    for name, value in DEFAULT_CAPS.items():
        assert data.get(name) == value, f"{name} missing or drifted: {data.get(name)!r}"


def test_optional_per_caller_caps_are_absent_by_default():
    data = _configmap(_render())
    present = [name for name in OPTIONAL_CAPS if name in data]
    assert not present, f"optional caps must be absent by default (empty = global): {present}"


def test_cap_overrides_reach_the_pod():
    data = _configmap(
        _render(
            "--set",
            "query.maxConcurrent=4",
            "--set",
            "query.maxConcurrentPerCaller=2",
            "--set",
            "query.maxJobs=16",
            "--set",
            "query.maxJobsPerCaller=3",
            "--set",
            "query.maxAsyncQueries=50",
            "--set",
            "query.maxAsyncQueriesPerCaller=5",
            "--set",
            "query.maxPageRows=250",
            "--set",
            "query.maxScopes=64",
            "--set",
            "query.maxResultBytes=50000",
            "--set",
            "cache.profileMaxRows=250000",
            "--set",
            "cache.profileCacheSize=128",
            "--set",
            "query.singleflight=false",
            "--set",
            "query.singleflightTimeout=2",
            "--set",
            "query.fairnessScope=rules",
        )
    )
    assert data["SQLHANDLER_MAX_CONCURRENT_QUERIES"] == "4"
    assert data["SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER"] == "2"
    assert data["SQLHANDLER_MAX_JOBS"] == "16"
    assert data["SQLHANDLER_MAX_JOBS_PER_CALLER"] == "3"
    assert data["SQLHANDLER_MAX_ASYNC_QUERIES"] == "50"
    assert data["SQLHANDLER_MAX_ASYNC_QUERIES_PER_CALLER"] == "5"
    assert data["SQLHANDLER_MAX_PAGE_ROWS"] == "250"
    assert data["SQLHANDLER_MAX_SCOPES"] == "64"
    assert data["SQLHANDLER_MAX_RESULT_BYTES"] == "50000"
    assert data["SQLHANDLER_PROFILE_MAX_ROWS"] == "250000"
    assert data["SQLHANDLER_PROFILE_CACHE_SIZE"] == "128"
    assert data["SQLHANDLER_SINGLEFLIGHT"] == "0"
    assert data["SQLHANDLER_SINGLEFLIGHT_TIMEOUT"] == "2"
    assert data["SQLHANDLER_FAIRNESS_SCOPE"] == "rules"


def test_values_defaults_match_the_documented_caps():
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    query = values["query"]
    assert query["maxConcurrent"] == "8"
    assert query["maxConcurrentPerCaller"] == ""
    assert query["queueTimeoutSeconds"] == "30"
    assert query["maxRows"] == "1000"
    assert query["maxOutputRows"] == "1000"
    assert query["maxResultBytes"] == "100000"
    assert query["maxJobs"] == "8"
    assert query["maxJobsPerCaller"] == ""
    assert query["maxAsyncQueries"] == "100"
    assert query["maxAsyncQueriesPerCaller"] == ""
    assert query["maxPageRows"] == "1000"
    assert query["maxScopes"] == "256"
    assert query["cancelGraceSeconds"] == "5"
    assert query["askMaxTables"] == "5"
    assert query["askMaxColumns"] == "20"
    assert query["askMaxProfileColumns"] == "6"
    assert query["exportDefaultRows"] == "10000"
    assert query["exportMaxRows"] == "100000"
    assert values["cache"]["profileMaxRows"] == "1000000"
    assert values["cache"]["profileCacheSize"] == "512"
    assert query["singleflight"] is True
    assert query["singleflightTimeout"] == ""
    assert query["fairnessScope"] == "identity"
