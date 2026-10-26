"""Tests for the policy WRITE posture (read-only-filesystem detection).


The live 3.9.0 deployment mounted the policy file from a ConfigMap (read-only
filesystem): enforcement worked, but every admin write failed at the LAST
moment with a raw "[Errno 30] Read-only file system" and the operator had no
way to know the deployment was degraded. These tests pin the three surfaces
of the fix:

* the startup probe (side-effect-free — os.access, never a probe file) and
  its loud WARNING banner / INFO line;
* the actionable EROFS message at the point of failure (names BOTH remedies:
  claim mode AND kubectl, plus the path);
* the /health probe body carrying the writable flag when enforcement is on.

The read-only cases are simulated by monkeypatching ``os.access`` rather than
chmod 0555: root ignores mode bits, so a chmod test can be vacuous (the one
chmod-based test is guarded). The EROFS case is simulated at
``tempfile.mkstemp`` — the exact syscall boundary _policy_atomic_write
fails at on a read-only mount.

Run:  python -m pytest tests/test_policy_write_posture.py -v
"""

from __future__ import annotations

import errno
import json
import logging
import os

import pytest
from starlette.testclient import TestClient

from sqlhandler import policy as _policy
from sqlhandler import server as server_module

POLICY_DOC = {"datasets": {"global": ["workorder/*"]}, "admins": []}


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    """Enforcement ON + a tmp policy file (the fleet fixture pattern)."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY_DOC, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(path))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    yield tmp_path
    _policy.reset_policy_store()


# ---------------------------------------------------------------------------
# the startup posture probe
# ---------------------------------------------------------------------------


def test_posture_writable_dir(policy_env):
    """A writable policy directory reports writable=True (and the probe
    leaves NO litter: it never creates a file)."""
    posture = server_module._policy_write_posture()
    assert posture["path"] == str(policy_env / "policy.json")
    assert posture["writable"] is True
    assert "writable" in posture["reason"].lower()
    # side-effect-free: only the fixture's policy.json exists in the dir
    assert sorted(p.name for p in policy_env.iterdir()) == ["policy.json"]


def test_posture_no_policy_file(monkeypatch):
    """No SQLHANDLER_POLICY_FILE = not writable (nothing to persist to),
    never raises."""
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    posture = server_module._policy_write_posture()
    assert posture["path"] is None
    assert posture["writable"] is False


def test_posture_readonly_dir_via_os_access(policy_env, monkeypatch):
    """os.access(W_OK) False on the policy DIRECTORY (the ConfigMap-mount
    signature: the mount refuses every create) reports not writable.
    Monkeypatched rather than chmod 0555: root ignores mode bits."""
    real_access = os.access

    def fake_access(path, mode, *args, **kwargs):
        if mode == os.W_OK and str(path) == str(policy_env):
            return False  # read-only mount
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(server_module.os, "access", fake_access)
    posture = server_module._policy_write_posture()
    assert posture["writable"] is False
    assert "read-only" in posture["reason"]


def test_posture_readonly_file_via_os_access(policy_env, monkeypatch):
    """A read-only policy FILE inside a writable directory (chmod 0444
    policy.json) reports not writable."""
    real_access = os.access
    policy_path = policy_env / "policy.json"
    policy_path.chmod(0o444)

    def fake_access(path, mode, *args, **kwargs):
        if mode == os.W_OK and str(path) == str(policy_path):
            return False  # file mode denies this euid
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(server_module.os, "access", fake_access)
    posture = server_module._policy_write_posture()
    assert posture["writable"] is False
    assert "not writable" in posture["reason"]


def test_posture_probe_never_raises(policy_env, monkeypatch):
    """A probe failure degrades quietly to not writable — the probe
    must never raise into the startup path."""

    def boom(path, mode, *args, **kwargs):
        raise OSError(errno.EACCES, "simulated probe failure")

    monkeypatch.setattr(server_module.os, "access", boom)
    posture = server_module._policy_write_posture()
    assert posture["writable"] is False
    assert "probe failed" in posture["reason"]


def test_startup_warning_on_readonly(policy_env, monkeypatch, caplog):
    """Not-writable = the loud "=" * 72 WARNING banner naming the ConfigMap
    posture, the broken surfaces, and BOTH remedies."""
    real_access = os.access

    def fake_access(path, mode, *args, **kwargs):
        if mode == os.W_OK and str(path) == str(policy_env):
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(server_module.os, "access", fake_access)
    with caplog.at_level(logging.WARNING, logger="sqlhandler"):
        server_module._log_policy_write_posture()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "=" * 72 in joined
    assert "READ-ONLY" in joined
    assert "security.policy.existingClaim" in joined
    assert "kubectl" in joined


def test_startup_info_on_writable(policy_env, caplog):
    """Writable = a single INFO line (no banner noise on healthy deploys)."""
    with caplog.at_level(logging.INFO, logger="sqlhandler"):
        server_module._log_policy_write_posture()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("WRITABLE" in msg for msg in infos)
    assert not any("=" * 72 in msg for msg in infos)


# ---------------------------------------------------------------------------
# the point-of-failure message (EROFS)
# ---------------------------------------------------------------------------


def test_atomic_write_erofs_message(policy_env, monkeypatch):
    """EROFS at the mkstemp boundary (the ConfigMap-mount failure) produces
    the self-correcting message: read-only posture named, BOTH remedies
    (claim mode + kubectl), and the path — still ONE string the admin UI
    renders verbatim."""

    def erofs_mkstemp(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(server_module.tempfile, "mkstemp", erofs_mkstemp)
    with pytest.raises(server_module.AdminHTTPError) as excinfo:
        server_module._policy_atomic_write(json.dumps(POLICY_DOC))
    msg = excinfo.value.args[0]
    assert isinstance(msg, str)  # ONE string - the UI renders it verbatim
    assert str(policy_env / "policy.json") in msg
    assert "read-only" in msg.lower()
    assert "existingClaim" in msg
    assert "kubectl" in msg
    # the original errno detail survives
    assert "errno 30" in msg or "Errno 30" in msg or "EROFS" in msg
    assert excinfo.value.status == 503


def test_atomic_write_other_errno_names_path_and_errno(policy_env, monkeypatch):
    """Non-EROFS OSErrors keep a message naming the path AND the errno."""

    def eacces_mkstemp(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(server_module.tempfile, "mkstemp", eacces_mkstemp)
    with pytest.raises(server_module.AdminHTTPError) as excinfo:
        server_module._policy_atomic_write(json.dumps(POLICY_DOC))
    msg = excinfo.value.args[0]
    assert str(policy_env / "policy.json") in msg
    assert "errno" in msg.lower()
    assert str(int(errno.EACCES)) in msg


def test_atomic_write_success_untouched(policy_env):
    """The happy path still writes (the fix is message-only; no write
    semantics changed)."""
    doc = json.dumps(POLICY_DOC, indent=2) + "\n"
    server_module._policy_atomic_write(doc)
    assert (policy_env / "policy.json").read_text(encoding="utf-8") == doc


# ---------------------------------------------------------------------------
# the /health probe surface
# ---------------------------------------------------------------------------


def test_health_carries_writable_flag(policy_env):
    """/health body carries policy.writable=True when enforcement is on and
    the location is writable — probe-safe, liveness unchanged."""
    app = server_module._build_http_app()
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"  # liveness unchanged
        assert body["policy"]["enabled"] is True
        assert body["policy"]["writable"] is True


def test_health_writable_false_on_readonly(policy_env, monkeypatch):
    """/health reports writable=False for a read-only mount — the
    silently degraded state is now probe-visible."""
    real_access = os.access

    def fake_access(path, mode, *args, **kwargs):
        if mode == os.W_OK and str(path) == str(policy_env):
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(server_module.os, "access", fake_access)
    app = server_module._build_http_app()
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"  # posture, not health: liveness stays ok
        assert body["policy"]["writable"] is False


def test_health_without_policy_unchanged(monkeypatch):
    """/health stays byte-shaped for deployments without a policy file
    (additive ONLY when enforcement is on)."""
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    _policy.reset_policy_store()
    try:
        app = server_module._build_http_app()
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
    finally:
        _policy.reset_policy_store()



def test_ready_distinguishes_missing_file_from_invalid_file(monkeypatch, tmp_path):
    """A MISSING policy file is NOT the same state as an INVALID one, and the
    readiness warning must not conflate them.

    Verified against the loader: an absent live file yields an EMPTY Policy
    (groups_for() == (), every rule default-permissive) — NO enforcement.
    An invalid file yields the _BROKEN_POLICY sentinel (every covered table
    hidden) — genuinely fail-closed. The old warning said "failing closed"
    for both, which is the log-says-safe/behaviour-says-open inversion this
    deployment has already been burned by once.

    The missing-file state is reachable in CLAIM mode (empty PVC on first
    boot + a best-effort seed), so the distinction is load-bearing now.
    """
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_READINESS_CHECK", "0")  # isolate the policy branch
    live = tmp_path / "policy.json"
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(live))

    # 1. MISSING file -> not enforcing, and the warning must SAY so.
    _policy.reset_policy_store()
    try:
        # The loader must genuinely resolve to an UNENFORCED policy here —
        # assert the behaviour first, so the wording cannot drift away from it.
        pol = _policy.policy_store().get()
        assert pol.groups_for("anyone", None) == ()
        assert pol.rule_for_table("employees", "employees", ()).hidden is False

        body = server_module._policy_readiness_warning()
        assert "PERMISSIVE" in body["policy_warning"], body["policy_warning"]
        assert "failing CLOSED" not in body["policy_warning"]
    finally:
        _policy.reset_policy_store()

    # 2. INVALID file -> fail-closed, and the warning says THAT.
    live.write_text("{ not json", encoding="utf-8")
    _policy.reset_policy_store()
    try:
        assert _policy.policy_store().get() is _policy._BROKEN_POLICY
        body = server_module._policy_readiness_warning()
        assert "failing CLOSED" in body["policy_warning"], body["policy_warning"]
        assert "PERMISSIVE" not in body["policy_warning"]
    finally:
        _policy.reset_policy_store()




def test_missing_policy_is_a_PERMISSIVE_BOOTSTRAP_and_still_serves(monkeypatch, tmp_path):
    """A MISSING policy file is the permissive bootstrap default: the pod must
    come up SERVING, and the state must be loudly reported.

    This is a deliberate product decision. A fresh install has no policy file
    yet, and the operator authors restrictions through the admin panel — so a
    pod that refused traffic until a policy existed would be unreachable,
    including the panel that fixes it. (An earlier revision gated /ready on
    this state; on first boot with a failed seed that produced exactly that
    deadlock.)

    The safety boundary is therefore explicit:
      * MISSING file  -> permissive, pod READY, warning says so;
      * INVALID file  -> broken sentinel hides every covered table; READY.
    """
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_READINESS_CHECK", "0")  # isolate the policy branch
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(tmp_path / "absent.json"))
    _policy.reset_policy_store()
    try:
        app = server_module._build_http_app()
        with TestClient(app) as client:
            r = client.get("/ready")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ready"
            # no gate flag survives from the earlier fail-closed revision
            assert "policy_gate_failed" not in r.json()
            # ...but the permissive state is never silent
            assert "PERMISSIVE" in r.json()["policy_warning"]
            assert client.get("/health").status_code == 200
    finally:
        _policy.reset_policy_store()


def test_ready_is_200_once_a_valid_policy_loads(monkeypatch, tmp_path):
    """The gate must open as soon as a valid policy exists — otherwise a
    correct deployment would be stuck out of the Service forever."""
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_READINESS_CHECK", "0")
    live = tmp_path / "policy.json"
    live.write_text(json.dumps(POLICY_DOC) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(live))
    _policy.reset_policy_store()
    try:
        app = server_module._build_http_app()
        with TestClient(app) as client:
            r = client.get("/ready")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ready"
            assert "policy_gate_failed" not in r.json()
    finally:
        _policy.reset_policy_store()




def test_startup_warns_loudly_when_policy_is_not_restricting(monkeypatch, tmp_path, caplog):
    """A missing policy file means NOTHING is restricted — say so at startup.

    The permissive default is intentional (fresh installs must be
    administrable), but an operator who believes restrictions are in force
    when none are is the real hazard. The warning must name the path and the
    remedy.
    """
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(tmp_path / "absent.json"))

    with caplog.at_level(logging.WARNING):
        server_module._log_policy_enforcement_posture()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("NOT RESTRICTING ANYTHING" in m for m in msgs), msgs
    assert any("PERMISSIVE" in m for m in msgs), msgs
    assert any("admin panel" in m for m in msgs), msgs


def test_startup_is_quiet_when_a_policy_file_exists(monkeypatch, tmp_path, caplog):
    """No spurious warning once a file exists — load_policy logs its own state."""
    live = tmp_path / "policy.json"
    live.write_text(json.dumps(POLICY_DOC) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(live))

    with caplog.at_level(logging.WARNING):
        server_module._log_policy_enforcement_posture()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("NOT RESTRICTING ANYTHING" in m for m in msgs), msgs


def test_startup_is_silent_when_policy_is_disabled(monkeypatch, tmp_path, caplog):
    """Enforcement off is a legitimate posture — no warning noise."""
    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(tmp_path / "absent.json"))

    with caplog.at_level(logging.WARNING):
        server_module._log_policy_enforcement_posture()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("NOT RESTRICTING ANYTHING" in m for m in msgs), msgs

def test_health_never_touches_the_filesystem_on_the_liveness_path(monkeypatch, tmp_path):
    """The LIVENESS path must read a CACHED posture, never os.access.

    Claim mode requires a network filesystem, and a hung mount (stale NFS
    handle) makes os.access block until the kernel gives up. The liveness
    probe timeout is 5s, so a blocking probe would restart-loop every replica
    of exactly the deployment this feature exists to enable — and it blocks
    the event loop, stalling /metrics too.
    """
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    live = tmp_path / "policy.json"
    live.write_text(json.dumps(POLICY_DOC) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(live))
    _policy.reset_policy_store()

    calls: list[str] = []

    def _explode(*a, **kw):
        calls.append("os.access")
        raise AssertionError("os.access called on the liveness path (blocking I/O)")

    try:
        app = server_module._build_http_app()
        with TestClient(app) as client:
            server_module._refresh_policy_write_posture()  # prime the cache
            monkeypatch.setattr(os, "access", _explode)
            r = client.get("/health")
            assert r.status_code == 200
            assert calls == [], "the liveness probe performed blocking filesystem I/O"
    finally:
        _policy.reset_policy_store()


# ---------------------------------------------------------------------------
# Atomic first-boot seeding (claim mode, shared RWX claim)
# ---------------------------------------------------------------------------


def _seed_env(monkeypatch, tmp_path):
    """A seed file + a not-yet-existing live path in separate directories."""
    seed_dir = tmp_path / "seed"
    live_dir = tmp_path / "live"
    seed_dir.mkdir()
    live_dir.mkdir()
    seed = seed_dir / "policy.json"
    seed.write_text(json.dumps(POLICY_DOC) + "\n", encoding="utf-8")
    live = live_dir / "policy.json"
    monkeypatch.setenv("SQLHANDLER_POLICY_SEED_FILE", str(seed))
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(live))
    return seed, live


def test_seed_is_atomic_no_partial_file_is_ever_visible(monkeypatch, tmp_path):
    """The seed must publish the live policy with ONE atomic rename.

    WHY: the claim is RWX and shared by every replica. A plain copyfile
    wrote the document IN PLACE, so a concurrent reader (another replica's
    mtime hot-reload) could observe a HALF-WRITTEN policy. The store keeps
    the previous policy when a reload fails — but on FIRST boot there is no
    previous policy, so the loader returns the broken sentinel and the pod
    fails CLOSED (every covered table hidden). A torn read on the rollout
    that was meant to be zero-downtime is a self-inflicted outage.

    Pinned by watching the WRITE primitive: os.replace is the only way the
    live path may be created.
    """
    _seed, live = _seed_env(monkeypatch, tmp_path)
    created: list[str] = []
    real_replace = os.replace

    def _spy(src, dst, *a, **kw):
        created.append(str(dst))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", _spy)
    server_module._seed_policy_from_configmap()

    assert live.exists(), "the seed did not publish a live policy"
    assert created == [str(live)], "the live file must be created by os.replace only"
    assert json.loads(live.read_text(encoding="utf-8")) == POLICY_DOC
    # No temp litter is left behind in the live directory.
    assert [p.name for p in live.parent.iterdir()] == ["policy.json"]


def test_seed_is_idempotent_and_never_overwrites_an_existing_policy(monkeypatch, tmp_path):
    """Once the live file exists it is the truth FOREVER (the documented
    trade of writable-policy mode): a restart must not re-seed over panel
    edits, even though the seed ConfigMap still mounts."""
    _seed, live = _seed_env(monkeypatch, tmp_path)
    edited = {"admins": ["subject:andrew-bydlon"], "groups": {"g": {}}}
    live.write_text(json.dumps(edited) + "\n", encoding="utf-8")

    server_module._seed_policy_from_configmap()

    assert json.loads(live.read_text(encoding="utf-8")) == edited, "the seed clobbered a live policy"


def test_seed_failure_is_loud_and_leaves_no_temp_file(monkeypatch, tmp_path, caplog):
    """A missing/unreadable seed must log an error, not raise (the pod must
    still boot) and must not leave a stray temp file behind."""
    _, live = _seed_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SQLHANDLER_POLICY_SEED_FILE", str(tmp_path / "does-not-exist.json"))

    with caplog.at_level(logging.ERROR):
        server_module._seed_policy_from_configmap()  # must not raise

    assert not live.exists()
    assert [p.name for p in live.parent.iterdir()] == [], "temp file leaked on seed failure"
    assert any("policy seeding FAILED" in r.getMessage() for r in caplog.records)

