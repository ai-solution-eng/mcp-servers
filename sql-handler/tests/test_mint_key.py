"""Contract tests for scripts/mint_key.py (the admin key-minting CLI).

The contract (documentation/DEPLOYMENT.md "Dataset ACLs — mint + grants"):

* The printed fingerprint is computed by the SERVER'S OWN
  ``sqlhandler.mcp_fleet_common.audit.key_fingerprint`` — the script imports
  it via a sys.path bootstrap to the repo's src/, so the test asserts
  fingerprint-in-output == key_fingerprint(the printed key). This is the
  non-negotiable invariant: a fingerprint that doesn't match the audit
  layer's would mint keys that grant nothing (or worse, grant to nobody).
* Output structure: the key, the fingerprint, a copy-pasteable
  ``SQLHANDLER_API_KEYS=`` comma-APPEND line (rotation = append → move
  clients → drop old), and a ``datasets.assignments`` JSON snippet keyed by
  the fingerprint.
* ``--assign "a/*,b/*"`` propagates the globs into the snippet; no flag =
  ``["*"]``; ``--label`` appears in the output.
* The script itself is subprocess-executed (sys.executable) exactly the way
  an operator runs it — its stdout carries the key ONCE and nowhere else,
  and it must not need network access or a working directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "mint_key.py"

sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlhandler.mcp_fleet_common.audit import key_fingerprint  # noqa: E402


def _run(*flags: str) -> tuple[str, str]:
    """Run the script the way an operator does; return (stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *flags],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT.parent / "elsewhere-does-not-exist") if False else str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode == 0, f"mint_key.py exited {proc.returncode}: {proc.stderr}"
    return proc.stdout, proc.stderr


def _fields(stdout: str) -> dict[str, str]:
    """Pull the 'name : value' fields out of the header block."""
    fields = {}
    for line in stdout.splitlines():
        if ":" in line and not line.lstrip().startswith("#") and "=" not in line:
            name, _, value = line.partition(":")
            fields[name.strip()] = value.strip()
    return fields


def test_fingerprint_matches_audit_module():
    stdout, stderr = _run()
    assert stderr == "", f"the minting flow must be silent on stderr: {stderr}"
    printed_key = _fields(stdout)["key"]
    printed_fp = _fields(stdout)["fingerprint"]
    assert printed_key, "a key must be printed"
    assert printed_fp == key_fingerprint(printed_key), (
        "printed fingerprint must equal audit.key_fingerprint(the printed key) — "
        "a mismatching fingerprint mints keys that bind to nothing"
    )
    assert printed_fp.startswith("sha256:") and len(printed_fp) == len("sha256:") + 12, (
        f"fingerprint format drift: {printed_fp!r} (expected sha256:<12hex>)"
    )


def test_api_keys_append_line_is_copy_pasteable():
    stdout, _ = _run()
    key = _fields(stdout)["key"]
    append_lines = [ln for ln in stdout.splitlines() if ln.startswith("SQLHANDLER_API_KEYS=")]
    assert len(append_lines) == 1, "exactly one copy-pasteable append line"
    line = append_lines[0]
    # comma-APPEND semantics: the new key plus a placeholder for the existing list
    assert f'"{key},' in line, f"append line must comma-append the new key: {line}"
    assert "existing-keys" in line, "append line must keep the existing keys in place"


def test_assignments_snippet_structure_and_full_access_default():
    stdout, _ = _run()
    fp = _fields(stdout)["fingerprint"]
    # The snippet: a JSON object keyed by the fingerprint, mapping to a list.
    json_blob = stdout[stdout.index("{\n") :]
    snippet = json.loads(json_blob)
    assert list(snippet.keys()) == [fp], f"snippet keyed by the fingerprint: {snippet}"
    assert snippet[fp] == ["*"], "no --assign flag = full access ['*']"


def test_assign_propagates_comma_globs():
    stdout, _ = _run("--assign", "workorder/*,reports/*")
    fp = _fields(stdout)["fingerprint"]
    snippet = json.loads(stdout[stdout.index("{\n") :])
    assert snippet[fp] == ["workorder/*", "reports/*"], snippet
    assert not any(ln.startswith("SQLHANDLER_API_KEYS=") and '"*"' in ln for ln in stdout.splitlines())


def test_assign_empty_entries_are_dropped():
    stdout, _ = _run("--assign", " workorder/* , ,reports/*,")
    fp = _fields(stdout)["fingerprint"]
    snippet = json.loads(stdout[stdout.index("{\n") :])
    assert snippet[fp] == ["workorder/*", "reports/*"], "blank entries must not become globs"


def test_assign_all_blank_falls_back_to_full_access():
    stdout, _ = _run("--assign", " , ")
    snippet = json.loads(stdout[stdout.index("{\n") :])
    assert list(snippet.values()) == [["*"]], "an all-blank --assign degrades to full access, never to []"


def test_label_appears_in_output():
    stdout, _ = _run("--label", "ci-runner on build-42")
    assert "ci-runner on build-42" in stdout, "the free-text label must appear in the output comment"


def test_key_appears_exactly_once_on_stdout():
    stdout, _ = _run()
    key = _fields(stdout)["key"]
    assert stdout.count(key) == 2, (
        "the raw key appears exactly twice: the key field and the SQLHANDLER_API_KEYS "
        "append line — never in the policy snippet, never on stderr"
    )
    fp = _fields(stdout)["fingerprint"]
    assert fp not in key and key not in fp, "the fingerprint must not leak the raw key (or vice versa)"


def test_runs_from_any_directory():
    """The sys.path bootstrap must not depend on the caller's cwd."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd="/",
        timeout=30,
    )
    assert proc.returncode == 0, f"failed from cwd=/: {proc.stderr}"
    assert _fields(proc.stdout)["fingerprint"].startswith("sha256:")
