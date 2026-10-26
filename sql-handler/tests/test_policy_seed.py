"""The writable-policy mode (claim-backed policy file) + one-time seeding.

Live 2026-10-05, G2: the policy file was a read-only ConfigMap mount —
"[Errno 30] Read-only file system" on every runtime policy write (the
Access-control policy editor, grant-by-name, admin policy twins). The
writable mode mounts the policy on an RWX claim (the catalog PVC) and the
server SEEDS it from the ConfigMap on first boot (one-time migration;
after that the live file is the truth and ConfigMap edits stop applying).

Contracts pinned here:

* SEEDING — a configured seed source is copied to the live path ONCE
  (only when the live file does not exist); idempotent (an existing live
  file is never overwritten — the live file is the truth); the live
  file's PARENT DIRECTORY is created (same self-init rule as the keys
  store); failures log loudly and continue (the pod must boot).
* NO SEED configured / paths equal → the function is a no-op.
* The seeded file is a VALID policy document (the store compiles it and
  enforcement sees the seed's grants).

Run:  python -m pytest tests/test_policy_seed.py -v
"""

from __future__ import annotations

import json

import pytest

from sqlhandler import policy as _policy
from sqlhandler.server import _seed_policy_from_configmap

SEED_ENV = "SQLHANDLER_POLICY_SEED_FILE"
LIVE_ENV = "SQLHANDLER_POLICY_FILE"

DOC = {
    "datasets": {"global": ["*"], "assignments": {}},
    "admins": ["sha256:0123456789ab"],
}


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    seed = tmp_path / "seed" / "policy.json"
    seed.parent.mkdir()
    live = tmp_path / "live" / "policy.json"
    monkeypatch.setenv(SEED_ENV, str(seed))
    monkeypatch.setenv(LIVE_ENV, str(live))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    yield seed, live
    _policy.reset_policy_store()


def test_seeds_when_live_missing(paths):
    seed, live = paths
    seed.write_text(json.dumps(DOC))
    assert not live.exists()
    _seed_policy_from_configmap()
    assert live.exists()
    assert json.loads(live.read_text()) == DOC


def test_seed_creates_the_live_directory(paths):
    """The live path's parent does not exist (fresh claim subdir) — the
    seed creates it (same self-init rule as the keys store)."""
    seed, live = paths
    assert not live.parent.exists()
    seed.write_text(json.dumps(DOC))
    _seed_policy_from_configmap()
    assert live.parent.is_dir() and live.exists()


def test_seed_is_idempotent_live_file_wins(paths):
    """An EXISTING live file is never overwritten — the live file is the
    truth from then on (ConfigMap edits stop applying; the documented
    trade of the writable mode)."""
    seed, live = paths
    seed.write_text(json.dumps(DOC))
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({"datasets": {"global": ["changed"]}}))
    _seed_policy_from_configmap()
    assert json.loads(live.read_text()) == {"datasets": {"global": ["changed"]}}


def test_no_seed_configured_is_a_noop(paths, monkeypatch):
    _seed, live = paths
    monkeypatch.delenv(SEED_ENV, raising=False)
    _seed_policy_from_configmap()
    assert not live.exists()


def test_equal_paths_are_a_noop(paths, monkeypatch):
    """A degenerate config (seed == live) must not clobber the live file
    with itself (or fail) — skip silently."""
    _seed, live = paths
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps(DOC))
    monkeypatch.setenv(SEED_ENV, str(live))
    _seed_policy_from_configmap()
    assert json.loads(live.read_text()) == DOC


def test_missing_seed_file_is_a_noop(paths):
    _seed, live = paths
    assert not live.exists()
    _seed_policy_from_configmap()
    assert not live.exists()


def test_seeded_file_is_a_valid_policy(paths):
    """The end-to-end point of the feature: after seeding, the policy
    store compiles the live file and enforcement sees the seed's grants."""
    seed, _live = paths
    seed.write_text(json.dumps(DOC))
    _seed_policy_from_configmap()
    _policy.reset_policy_store()
    pol = _policy.policy_store().get()
    assert pol is not None
    # The seed's admins designation is live.
    assert "sha256:0123456789ab" in pol.admins
    # The seed's global grant resolves for any subject (default group).
    r = pol.rule_for_table("orders", "orders", pol.groups_for("andrew-bydlon", None))
    assert not r.hidden
