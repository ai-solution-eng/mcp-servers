"""Contract tests for scripts/configure-oidc-sql.sh (the SSO callback registrar).

The script runs ONCE per deployment on the ops box against the REAL Keycloak
(admin API) — these tests exercise its LOGIC against a local fake Keycloak +
stubbed kubectl, pinning the fleet-script contract (the MM-RAG
configure-oidc-rag.sh shape, adapted):

* Idempotency — an ALREADY-registered callback URL takes the no-PUT path
  (never rewrites the client; a concurrent Keycloak edit is untouched).
* Registration — a NEW callback URL is PUT with a FIELD-SCOPED body
  (clientId/name/redirectUris only) and the script VERIFIES the URI landed
  (a lying 200 never reads as success).
* The values block — printed values must MATCH the chart contract exactly:
  the UA realm issuer, the in-cluster headless JWKS URL, the callback URL
  derived from VIRTUAL_SERVICE_NAME, the external discovery URL, and the
  pcai-sso cookie block.
* The client secret is printed EXACTLY ONCE (the same once-only contract
  as the key mint) — with a masked echo in the comment and the full value
  on its own line; the script must NEVER write it to a file.

Run:  python -m pytest tests/test_configure_oidc_script.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "configure-oidc-sql.sh"

DOMAIN = "pcai-se-ai-application.hst.rdlabs.hpecorp.net"
EXPECTED_CALLBACK = f"https://sqlhandler.{DOMAIN}/oauth/oidc/callback"
FAKE_SECRET = "ua-client-secret-FAKE1234567890"


class _FakeKeycloak:
    """The slice of the Keycloak admin API the script touches."""

    def __init__(self, initial_uris: list[str]):
        self.uris = list(initial_uris)
        self.put_bodies: list[dict] = []
        self.put_count = 0
        self._lock = threading.Lock()

    def start(self):
        holder = self

        class _H(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._send(200, {"access_token": "fake-admin-token"})

            def do_GET(self):
                if "/admin/realms/UA/clients?clientId=ua" in self.path:
                    self._send(200, [{"id": "client-uuid-1"}])
                else:
                    self._send(200, {"secret": FAKE_SECRET, "redirectUris": list(holder.uris)})

            def do_PUT(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with holder._lock:
                    holder.put_bodies.append(payload)
                    holder.put_count += 1
                    holder.uris = list(payload["redirectUris"])
                self._send(200, {})

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def fake_kc():
    holder = _FakeKeycloak(
        [f"https://rag-mcp-server.{DOMAIN}/oauth/oidc/callback"]  # RAG's URI pre-exists
    ).start()
    yield holder
    holder.stop()


@pytest.fixture()
def script_env(tmp_path, monkeypatch):
    """PATH with a stubbed kubectl (the two reads the script makes) + the
    script copy with KEYCLOAK_URL pointed at the fake server."""

    def _make(kc_url: str):
        stub = tmp_path / "bin"
        stub.mkdir(exist_ok=True)
        (stub / "kubectl").write_text(
            "#!/bin/bash\n"
            'case "$*" in\n'
            f'  *"ezapp-manager-parameters"*) echo "{DOMAIN}" ;;\n'
            '  *"secrets admin-pass"*) echo "cGFzc3dvcmQxMjM=" ;;\n'
            '  *) echo "unexpected kubectl call: $*" >&2; exit 1 ;;\n'
            "esac\n"
        )
        (stub / "kubectl").chmod(0o755)
        patched = tmp_path / "configure-test.sh"
        src = SCRIPT.read_text(encoding="utf-8")
        patched.write_text(
            src.replace('KEYCLOAK_URL="https://keycloak.${DOMAIN_NAME}"', f'KEYCLOAK_URL="{kc_url}"'),
            encoding="utf-8",
        )
        patched.chmod(0o755)
        return patched

    return _make


def _run(script_path: Path, stub_bin: Path) -> str:
    proc = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub_bin}:/usr/bin:/bin", "VIRTUAL_SERVICE_NAME": "sqlhandler"},
        timeout=30,
    )
    assert proc.returncode == 0, f"script exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


def test_missing_tools_fail_fast(script_env):
    """The script refuses to run without jq/kubectl — a clean early exit,
    not a mid-registration failure."""
    patched = script_env("http://127.0.0.1:1")  # never reached
    empty = patched.parent / "bin-empty"
    empty.mkdir(exist_ok=True)

    bash = shutil.which("bash")
    assert bash is not None  # the test box always has bash
    proc = subprocess.run(
        [bash, str(patched)],
        capture_output=True,
        text=True,
        env={"PATH": str(empty)},  # NO jq, NO kubectl, NO curl
        timeout=30,
    )
    assert proc.returncode != 0
    assert "required" in (proc.stdout + proc.stderr)


def test_registers_new_callback_and_verifies(script_env, fake_kc):
    """A NEW callback URI: PUT with the field-scoped body, verified
    registered, and the URI is present in the fake's state afterwards."""
    patched = script_env(fake_kc.url)
    out = _run(patched, _stub_dir(patched))
    assert "redirect URI registered + verified" in out
    assert EXPECTED_CALLBACK in fake_kc.uris
    # RAG's pre-existing URI was never dropped (the PUT appends).
    assert f"https://rag-mcp-server.{DOMAIN}/oauth/oidc/callback" in fake_kc.uris
    # FIELD-SCOPED PUT: only clientId/name/redirectUris — a full-object PUT
    # could clobber concurrent client edits.
    assert set(fake_kc.put_bodies[0].keys()) == {"clientId", "name", "redirectUris"}
    assert fake_kc.put_bodies[0]["clientId"] == "ua"


def test_already_registered_is_idempotent_no_put(script_env, fake_kc):
    """Re-running with the URI present: the no-PUT path, a clear message,
    and the fake's PUT counter stays at ZERO (the client was never
    rewritten)."""
    fake_kc.uris.append(EXPECTED_CALLBACK)
    patched = script_env(fake_kc.url)
    out = _run(patched, _stub_dir(patched))
    assert "redirect URI already registered" in out
    assert fake_kc.put_count == 0


def _stub_dir(_patched: Path) -> Path:
    # The stubbed kubectl lives next to the patched script's tmp dir root —
    # reconstruct from the env the fixture built (the fixture's stub is
    # <tmp>/bin; the patched script is <tmp>/configure-test.sh).
    return _patched.parent / "bin"


def test_values_block_matches_chart_contract(script_env, fake_kc):
    """The printed values block must match the chart's security.oidc.sso
    shape exactly — a block that drifts from the chart would paste-apply
    the wrong realm/URLs."""
    patched = script_env(fake_kc.url)
    out = _run(patched, _stub_dir(patched))
    # The realm-qualified URLs derive from the Keycloak URL the script was
    # GIVEN (the fake here; the real one on the ops box) — assert the
    # DERIVATION, not the literal host:
    assert f'issuer: "{fake_kc.url}/realms/UA"' in out
    assert (
        'jwksUrl: "http://keycloak-headless.keycloak.svc.cluster.local:8080/realms/UA/protocol/openid-connect/certs"'
        in out
    )
    assert f'redirectUri: "{EXPECTED_CALLBACK}"' in out
    assert f'providerUrl: "{fake_kc.url}/realms/UA/.well-known/openid-configuration"' in out
    assert "cookieName: pcai-sso" in out
    assert 'clientId: "ua"' in out
    assert "identityClaim: preferred_username" in out
    assert "audience: ua" in out


def test_secret_printed_exactly_once(script_env, fake_kc):
    """The client secret appears exactly once — on its own line (the
    copy-me value); the comment line above it is MASKED (first 4 chars).
    The secret never appears in any other output line."""
    patched = script_env(fake_kc.url)
    out = _run(patched, _stub_dir(patched))
    lines = [ln for ln in out.splitlines() if FAKE_SECRET in ln]
    assert len(lines) == 1, f"secret must appear exactly once, got {len(lines)}"
    masked = [ln for ln in out.splitlines() if "ua-c…" in ln]
    assert masked, "the masked comment form (ua-c…) should mark the printed-once line"
    assert FAKE_SECRET not in "\n".join(masked)


def test_failure_of_verification_aborts(script_env, fake_kc, monkeypatch):
    """If the post-PUT verification cannot find the URI (a lying 200), the
    script exits non-zero with a FAILED message — it never claims success
    it did not verify."""
    # Sabotage: make the fake IGNORE the PUT (state never updates).
    original_start = _FakeKeycloak.start

    def _stub_start(self):
        original_start(self)
        # Wrap: capture and drop PUT effects. `real_handler` is whatever
        # BaseHTTPRequestHandler subclass the fake mounted at start time —
        # a dynamic base class, so the mypy suppressions are the contract.
        real_handler = self.httpd.RequestHandlerClass  # type: ignore[attr-defined]

        class _NoPut(real_handler):  # type: ignore[misc,valid-type]
            def do_PUT(self_inner):
                self_inner.rfile.read(int(self_inner.headers.get("Content-Length", 0)))
                self_inner.send_response(200)
                self_inner.send_header("Content-Length", "0")
                self_inner.end_headers()

        self.httpd.RequestHandlerClass = _NoPut  # type: ignore[attr-defined]
        return self

    monkeypatch.setattr(_FakeKeycloak, "start", _stub_start)
    fresh = _FakeKeycloak([f"https://rag-mcp-server.{DOMAIN}/oauth/oidc/callback"]).start()
    try:
        patched2 = script_env(fresh.url)
        proc = subprocess.run(
            ["bash", str(patched2)],
            capture_output=True,
            text=True,
            env={"PATH": f"{_patched_tmp(patched2)}:/usr/bin:/bin", "VIRTUAL_SERVICE_NAME": "sqlhandler"},
            timeout=30,
        )
        assert proc.returncode != 0
        assert "FAILED" in proc.stdout + proc.stderr
    finally:
        fresh.stop()


def _patched_tmp(patched: Path) -> Path:
    return patched.parent / "bin"
