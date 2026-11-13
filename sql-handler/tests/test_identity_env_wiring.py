"""The identity rungs must be WIRED, not merely accepted by the values files.

Regression pin for the 2026-11-12 finding: ``helm/local/values.g2.yaml`` set
``security.identity.trustBrowserHeaders`` / ``requireIdentity``, the chart accepted
the keys and rendered NOTHING — so the oauth2-proxy browser rung and the
anonymous-caller gate looked enabled while being absent from the pod.

These tests render the REAL chart (a values/template drift fails here) and pin:

* requireIdentity is ON by default since the 2026-11-12 audit (P1-6: the
  /api data plane was anonymous in the chart posture) and can be opted out;
* trustBrowserHeaders stays ABSENT by default — an absent env is the server's
  off default, and that rung is a trust gate, not a convenience switch;
* each flag renders only its own env, as "1" (no cross-wiring);
* the tracked bootstrap Secret template names the PER-SERVICE Secret
  (sqlhandler-apikey), not the retired fleet-shared mcp-fleet-apikeys.

Run: python -m pytest tests/test_identity_env_wiring.py -v
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "helm"
EXAMPLE_SECRETS = CHART / "values-examples" / "secrets.example.yaml"

TRUST_ENV = "SQLHANDLER_TRUST_BROWSER_HEADERS"
REQUIRE_ENV = "SQLHANDLER_REQUIRE_IDENTITY"


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


def _env(docs):
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_require_identity_on_by_default_and_trust_gate_off():
    env = _env(_render())
    assert TRUST_ENV not in env, "trustBrowserHeaders must default OFF (it is a trust gate)"
    assert REQUIRE_ENV in env, "requireIdentity must default ON (audit 2026-11-12, P1-6)"
    assert env[REQUIRE_ENV]["value"] == "1"


def test_require_identity_can_be_disabled():
    env = _env(_render("--set", "security.identity.requireIdentity=false"))
    assert REQUIRE_ENV not in env, "an explicit opt-out must leave the env unset (server default off)"


def test_trust_browser_headers_renders_only_its_own_flag():
    env = _env(_render("--set", "security.identity.trustBrowserHeaders=true"))
    assert env[TRUST_ENV]["value"] == "1"
    # requireIdentity is on by default now; the trust gate must not toggle it.
    assert env[REQUIRE_ENV]["value"] == "1"


def test_require_identity_renders_only_its_own_flag():
    env = _env(_render("--set", "security.identity.requireIdentity=true"))
    assert env[REQUIRE_ENV]["value"] == "1"
    assert TRUST_ENV not in env


def test_values_defaults_are_secure():
    """2026-11-12 audit: the anonymous-caller gate is ON by default; the
    browser-header trust gate stays OFF (it is a trust gate, not a switch)."""
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    identity = values["security"]["identity"]
    assert identity["trustBrowserHeaders"] is False
    assert identity["requireIdentity"] is True


def test_bootstrap_admin_defaults_on_in_the_render():
    """A fresh install must have a reachable admin: no policy -> the
    configured key administers (admin_keys.bootstrap_admin_active)."""
    env = _env(_render())
    assert env["SQLHANDLER_BOOTSTRAP_ADMIN"]["value"] == "1"


def test_bootstrap_admin_can_be_disabled():
    env = _env(_render("--set", "security.policy.bootstrapAdmin=false"))
    assert env["SQLHANDLER_BOOTSTRAP_ADMIN"]["value"] == "0"


def test_example_secret_is_the_per_service_name():
    docs = [d for d in yaml.safe_load_all(EXAMPLE_SECRETS.read_text(encoding="utf-8")) if d]
    names = {d["metadata"]["name"] for d in docs if d.get("kind") == "Secret"}
    assert "sqlhandler-apikey" in names, "the mandatory /mcp key Secret must be in the template"
    assert "mcp-fleet-apikeys" not in names, (
        "the fleet-shared Secret is NOT what the chart wires (default is <fullname>-apikey)"
    )
