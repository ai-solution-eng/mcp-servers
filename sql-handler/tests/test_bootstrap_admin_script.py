"""Contract tests for scripts/bootstrap_admin.py (the new-site bootstrap CLI).

The contract: the Secret and the policy ConfigMap are INDEPENDENT objects,
and the only link between them is the fingerprint written into "admins".
The script must emit both from ONE key, with the fingerprint computed by the
SERVER'S OWN key_fingerprint (never a local reimplementation), and the
emitted YAML must be valid kubectl input.

Run: python -m pytest tests/test_bootstrap_admin_script.py -v
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "bootstrap_admin.py"

sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlhandler.mcp_fleet_common.audit import key_fingerprint  # noqa: E402

# A SYNTHETIC fixture key, derived at import time. It must be 64 hex chars to
# exercise the script's key handling, but a literal 64-hex run in source is
# exactly what the fleet secret scanner flags (its HEX64 pass has no
# allow-marker), so the value is computed instead of pasted. Nothing secret
# about it: it is a hash of a fixed string, never used against a real service.
KNOWN_KEY = hashlib.sha256(b"sqlhandler-bootstrap-admin-fixture").hexdigest()
MARKER = "# objects (Secret authenticates; ConfigMap authorizes)"


def _run(*flags: str) -> str:
    """Run the script the way an operator does; return stdout."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *flags],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode == 0, f"bootstrap_admin.py exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


def _field(stdout: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\s*:\s*(\S+)$", stdout, re.MULTILINE)
    assert match, f"{name} not found in output:\n{stdout}"
    return match.group(1)


def _docs(stdout: str) -> list[dict]:
    """The emitted manifests (everything after the marker)."""
    idx = stdout.index(MARKER) + len(MARKER)
    return [d for d in yaml.safe_load_all(stdout[idx:]) if d]


def test_fingerprint_comes_from_the_audit_module():
    stdout = _run("--key", KNOWN_KEY)
    assert _field(stdout, "key") == KNOWN_KEY
    assert _field(stdout, "fingerprint") == key_fingerprint(KNOWN_KEY)


def test_emits_the_secret_and_the_policy_configmap():
    docs = _docs(_run("--key", KNOWN_KEY))
    kinds = {d["kind"]: d for d in docs}
    assert set(kinds) == {"Secret", "ConfigMap"}
    secret = kinds["Secret"]
    assert secret["metadata"]["name"] == "sqlhandler-apikey"
    assert secret["stringData"]["api-keys"] == KNOWN_KEY
    configmap = kinds["ConfigMap"]
    assert configmap["metadata"]["name"] == "sqlhandler-policy"
    policy = json.loads(configmap["data"]["policy.json"])
    assert policy["admins"] == [f"key:{key_fingerprint(KNOWN_KEY)}"]


def test_generated_key_is_32_random_bytes_of_hex():
    stdout = _run()
    key = _field(stdout, "key")
    assert re.fullmatch(r"[0-9a-f]{64}", key), key
    assert _field(stdout, "fingerprint") == key_fingerprint(key)


def test_subject_entries_are_designated_alongside_the_key():
    stdout = _run("--key", KNOWN_KEY, "--subject", "andrew-bydlon", "--subject", "jane-doe")
    policy = json.loads(_docs(stdout)[1]["data"]["policy.json"])
    assert f"key:{key_fingerprint(KNOWN_KEY)}" in policy["admins"]
    assert "subject:andrew-bydlon" in policy["admins"]
    assert "subject:jane-doe" in policy["admins"]


def test_custom_names_and_namespace_propagate():
    docs = _docs(
        _run(
            "--key",
            KNOWN_KEY,
            "--namespace",
            "other-ns",
            "--secret",
            "other-apikey",
            "--configmap",
            "other-policy",
        )
    )
    for doc in docs:
        assert doc["metadata"]["namespace"] == "other-ns"
    names = {d["kind"]: d["metadata"]["name"] for d in docs}
    assert names == {"Secret": "other-apikey", "ConfigMap": "other-policy"}
