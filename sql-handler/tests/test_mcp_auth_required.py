"""MCP auth is MANDATORY for SQLhandler (the applygate/k8s-mcp posture).

SQLhandler reads the WHOLE data lake, so the chart now wires MCP_API_KEYS from
an operator-created Secret UNCONDITIONALLY: the pod fails loudly
(CreateContainerConfigError) until that Secret exists. These tests render the
REAL chart (so a values/template drift fails here) and pin:

* the default render carries the secretKeyRef (and never `optional: true`);
* security.apiKey.existingSecret still wins when set;
* security.apiKey.openMode=true (the documented LOCAL-DEV escape) removes the
  env — and that is the ONLY way to get an open /mcp;
* the chart NOTES document the breaking change and the escape.

Run: python -m pytest tests/test_mcp_auth_required.py -v
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "helm"


def _helm():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm not installed")
    if not CHART.is_dir():
        pytest.skip("helm chart not present")
    return helm


def _render(*extra):
    helm = _helm()
    out = subprocess.run(
        [helm, "template", "sqlhandler", str(CHART), *extra],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _deployment_env(docs):
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container.get("env", [])}


def _api_key_env(docs):
    env = _deployment_env(docs)
    assert "MCP_API_KEYS" in env, "the chart does not wire MCP_API_KEYS by default"
    return env["MCP_API_KEYS"]["valueFrom"]["secretKeyRef"]


def test_default_render_requires_the_api_key_secret():
    """Breaking change: auth is MANDATORY out of the box."""
    ref = _api_key_env(_render())
    assert ref["name"] == "sqlhandler-apikey"  # "<fullname>-apikey"
    assert ref["key"] == "api-keys"
    assert ref.get("optional") is not True  # a missing Secret must FAIL the pod


def test_existing_secret_value_still_wins():
    ref = _api_key_env(_render("--set", "security.apiKey.existingSecret=mcp-fleet-apikeys"))
    assert ref["name"] == "mcp-fleet-apikeys"


def test_custom_secret_key_is_honored():
    ref = _api_key_env(_render("--set", "security.apiKey.existingSecretKey=keys"))
    assert ref["key"] == "keys"


def test_open_mode_is_the_documented_local_dev_escape():
    env = _deployment_env(_render("--set", "security.apiKey.openMode=true"))
    assert "MCP_API_KEYS" not in env
    assert "SQLHANDLER_API_KEYS" not in env


def test_the_chart_never_creates_the_key_secret():
    """The K8S-MCP lesson: the chart must never own the key Secret."""
    docs = _render()
    secret_names = {d["metadata"]["name"] for d in docs if d.get("kind") == "Secret"}
    assert "sqlhandler-apikey" not in secret_names


def test_notes_document_the_breaking_change_and_the_escape():
    helm = _helm()
    out = subprocess.run(
        [helm, "install", "sqlhandler", str(CHART), "--dry-run=client", "--replace"],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    notes = out.split("NOTES:", 1)[-1]
    assert "CreateContainerConfigError" in notes
    assert "MANDATORY" in notes
    assert "security.apiKey.openMode=true" in notes
    assert "create secret generic sqlhandler-apikey" in notes


def test_server_open_mode_is_a_loud_documented_local_dev_escape(monkeypatch, caplog):
    """With no key env the server still runs (the escape) — but says why it is
    not a production posture."""
    import logging

    from sqlhandler.server import _build_http_app

    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        monkeypatch.delenv(var, raising=False)
    with caplog.at_level(logging.WARNING, logger="sqlhandler.server"):
        _build_http_app()
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    joined = " ".join(warnings)
    assert "LOCAL-DEV escape" in joined
    assert "security.apiKey.openMode=true" in joined
