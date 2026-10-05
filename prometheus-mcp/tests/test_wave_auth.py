"""Wave: fleet-mcp_auth adoption + honest-caps follow-ups for prometheus-mcp.

Covers five changes, each pinned where it can regress:

1. mcp_auth adoption (OPTIONAL per fleet decision 2026-09): /mcp requires a
   key ONLY when PROMETHEUS_API_KEYS / MCP_API_KEYS is configured; /health,
   /healthz, the console and /api/* stay key-free; warn_if_open fires in
   HTTP main() when neither is set.
2. Transport security via the shared mcp_auth.transport_security_from_env():
   no env → None (the SDK's implicit loopback protection — dev unchanged);
   MCP_HOSTNAME / MCP_EXTRA_ALLOWED_HOSTS set → explicit allowlist.
3. prom_rules max_rules cap with the fleet truncation-footer convention.
4. Saved-store per-replica honesty: query_save carries a storage block;
   PROMETHEUS_SAVED_QUERIES_SHARED=1 silences the warning.
5. Packaging of the new module (mcp_auth) lives in test_wave5_f4.py — the
   packaging regression suite; this file tests BEHAVIOR.

HTTP-level assertions drive server._build_http_app() through TestClient
(``with`` — the session-manager lifespan must run, see server.py's builder
docstring) with a stub client. Same shape as searxng's test_auth.py.
"""

import asyncio
import re
import shutil

import pytest
from starlette.testclient import TestClient

import mcp_auth
import saved_queries

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _SilentClient:
    """Stand-in PrometheusClient for app-level requests (/api/status)."""

    async def instant_query(self, query, ts=None):
        return {"resultType": "vector", "result": []}

    async def rules(self):
        return []

    async def alerts(self):
        return []


MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}
PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}


@pytest.fixture()
def app(monkeypatch):
    """The auth-wrapped app exactly as _build_http_app() assembles it, with
    clean key env (tests set what they need) and MCP_HOSTNAME pinned to the
    TestClient's host — i.e. the CHART-fronted posture (the deployment
    template renders MCP_HOSTNAME from ezua.virtualService.endpoint; without
    it the SDK's implicit loopback-only protection applies — covered by the
    subprocess smoke test in test_prometheus_mcp.py, which binds real
    127.0.0.1 and passes)."""
    import server

    for var in ("PROMETHEUS_API_KEYS", "MCP_API_KEYS", "MCP_HOSTNAME", "MCP_EXTRA_ALLOWED_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCP_HOSTNAME", "testserver")
    saved_queries.reset_store()
    try:
        with TestClient(server._build_http_app()) as client:
            yield client
    finally:
        saved_queries.reset_store()


def _ping(client, headers=None):
    return client.post("/mcp", json=PING, headers={**MCP_ACCEPT, **(headers or {})})


# ---------------------------------------------------------------------------
# 1. API-key auth — optional by design, /mcp-only scope
# ---------------------------------------------------------------------------


def test_mcp_open_when_no_keys_configured(app):
    assert app.get("/health").status_code == 200
    # No keys → the gate is open (dev mode): the ping proceeds into the MCP
    # app and answers 200, NOT the middleware's 401.
    assert _ping(app).status_code == 200


def test_mcp_401_without_key_when_configured(app, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_API_KEYS", "secret-a,secret-b")
    r = _ping(app)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert b"unauthorized" in r.content.lower()


def test_mcp_key_via_either_env_or_header(app, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_API_KEYS", "secret-a")
    assert _ping(app, {"Authorization": "Bearer secret-a"}).status_code == 200
    assert _ping(app, {"X-API-Key": "secret-a"}).status_code == 200
    assert _ping(app, {"Authorization": "Bearer nope"}).status_code == 401
    # fleet-universal env honored too (either var authenticates)
    monkeypatch.delenv("PROMETHEUS_API_KEYS")
    monkeypatch.setenv("MCP_API_KEYS", "universal-key")
    assert _ping(app, {"X-API-Key": "universal-key"}).status_code == 200


def test_public_surface_stays_key_free_when_configured(app, monkeypatch):
    """The whole point of `protected=`: /health, /healthz, the console and
    /api/* never require a key — even with keys configured (the ServiceMonitor
    scrape and the kubelet probes must keep working)."""
    monkeypatch.setenv("PROMETHEUS_API_KEYS", "secret-a")
    for path in ("/health", "/healthz", "/", "/ui", "/api/status"):
        r = app.get(path)
        assert r.status_code == 200, f"{path} must stay key-free (got {r.status_code})"
    # /metrics is chart-gated off by default — the important part is that the
    # auth gate never applies to it when it exists (not "/mcp...").
    assert app.get("/metrics").status_code != 401


def test_auth_middleware_wraps_the_assembled_app():
    """The builder returns the AUTH-WRAPPED app (fleet convention: wrap in
    the builder so every consumer of _build_http_app gets the gate)."""
    import server

    wrapped = server._build_http_app()
    assert isinstance(wrapped, mcp_auth.ApiKeyAuthMiddleware)
    paths = {getattr(r, "path", None) for r in wrapped.routes}
    assert {"/", "/api/status", "/health", "/mcp"} <= paths


def test_warn_if_open_reported_in_http_main(monkeypatch, capsys):
    """HTTP main() must scream when no keys are configured (stdio dev use
    never warns). We do not boot uvicorn — main() runs with a stubbed
    uvicorn module and port 0."""
    import server

    calls: list[dict] = []

    class _StubUvicorn:
        def run(self, app, host, port):
            calls.append({"app": app, "host": host, "port": port})

    import sys

    monkeypatch.setitem(sys.modules, "uvicorn", _StubUvicorn())
    monkeypatch.setattr("sys.argv", ["prometheus-mcp", "--transport", "streamable-http", "--port", "0"])
    server.main()
    captured = capsys.readouterr()
    out = captured.err + captured.out
    assert calls, "main() must still start uvicorn (stubbed)"
    assert "PROMETHEUS_API_KEYS" in out and "MCP_API_KEYS" in out, "warn_if_open must name both env vars"
    assert "OPEN" in out

    # and it stays SILENT once a key is configured
    calls.clear()
    monkeypatch.setenv("PROMETHEUS_API_KEYS", "k")
    server.main()
    captured = capsys.readouterr()
    out = captured.err + captured.out
    assert calls and "OPEN" not in out


# ---------------------------------------------------------------------------
# 2. Transport security from env (shared helper semantics)
# ---------------------------------------------------------------------------


def test_transport_security_none_without_env(monkeypatch):
    import server

    for var in ("MCP_HOSTNAME", "MCP_EXTRA_ALLOWED_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    # Pin the seam the app actually uses (per-BUILD evaluation, no module
    # snapshot) — see server._build_transport_security.
    assert server._build_transport_security() is None
    # and the shared helper agrees (None = dev mode, implicit SDK protection)
    assert mcp_auth.transport_security_from_env() is None


def test_transport_security_from_hostname_env(monkeypatch):
    from mcp.server.transport_security import TransportSecuritySettings

    monkeypatch.setenv("MCP_HOSTNAME", "prometheus-mcp.example.hpe.com")
    ts = mcp_auth.transport_security_from_env()
    assert isinstance(ts, TransportSecuritySettings)
    assert ts.enable_dns_rebinding_protection is True
    assert "prometheus-mcp.example.hpe.com" in ts.allowed_hosts
    assert "https://prometheus-mcp.example.hpe.com" in ts.allowed_origins
    assert "localhost:*" in ts.allowed_hosts and "127.0.0.1:*" in ts.allowed_hosts
    # extra hosts join verbatim (or host:* form) — in-cluster svc-DNS callers
    monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "prom-mcp.ns.svc.cluster.local, other.svc:*")
    ts = mcp_auth.transport_security_from_env()
    assert "prom-mcp.ns.svc.cluster.local" in ts.allowed_hosts
    assert "other.svc:*" in ts.allowed_hosts


def test_chart_renders_hostname_and_optional_key_envs():
    """Chart wiring: values → deployment env. Default render has NO key env
    (optional by design); setting the knobs renders them (and the pinned
    MCP_HOSTNAME comes from ezua.virtualService.endpoint).

    Transport-security default (the own-svc chart fix): the DEFAULT render
    (ezua enabled with an endpoint) carries MCP_EXTRA_ALLOWED_HOSTS whose
    FIRST entry is the chart-derived own service DNS
    <deployment.name>-service.<ns>.svc.cluster.local:* — the LLM-gateway
    relay hop's Host header (421 Misdirected Request without it). Site
    extraAllowedHosts APPEND after it; disabling ezua entirely (and clearing
    extras) removes both envs again (dev posture)."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if not (root / "helm" / "Chart.yaml").is_file() or shutil.which("helm") is None:
        pytest.skip("helm binary not available")
    base = subprocess_helm(root, [])
    rendered = base
    assert "PROMETHEUS_API_KEYS" not in rendered, "default render must not wire a key env"
    assert "PROMETHEUS_SAVED_QUERIES_SHARED" not in rendered, "default render must not declare shared"
    # MCP_HOSTNAME rides on the default ezua endpoint (VS enabled by default)
    assert "name: MCP_HOSTNAME" in rendered
    # ...and transport security being active means the own svc DNS is
    # ALWAYS in the allowlist (svc host first, then any site extras):
    m = re.search(r'name: MCP_EXTRA_ALLOWED_HOSTS\s*\n\s*value: "([^"]+)"', rendered)
    assert m, "default render (ezua on) must carry MCP_EXTRA_ALLOWED_HOSTS"
    assert m.group(1) == "prometheus-mcp-service.default.svc.cluster.local:*", (
        "first allowlist entry must be the chart-derived own service DNS, got: " + m.group(1)
    )

    rendered = subprocess_helm(
        root,
        [
            "--set", "apiKey.existingSecret=fleet-keys",
            "--set", "extraAllowedHosts={a.svc,b.svc:*}",
            "--set", "persistence.enabled=true",
            "--set", "persistence.shared=true",
        ],
    )
    assert "name: PROMETHEUS_API_KEYS" in rendered
    assert "name: fleet-keys" in rendered  # secretKeyRef, never a literal key
    assert "name: MCP_EXTRA_ALLOWED_HOSTS" in rendered
    # svc host FIRST, site extras appended comma-joined (a site setting
    # extraAllowedHosts must NOT lose the own-svc entry):
    m = re.search(r'name: MCP_EXTRA_ALLOWED_HOSTS\s*\n\s*value: "([^"]+)"', rendered)
    assert m, "extras render must carry MCP_EXTRA_ALLOWED_HOSTS"
    assert m.group(1) == (
        "prometheus-mcp-service.default.svc.cluster.local:*,a.svc,b.svc:*"
    ), f"svc host must lead the allowlist, site extras appended; got: {m.group(1)}"

    # ezua disabled entirely (no endpoint, no extras) → dev posture: BOTH
    # transport envs absent (the SDK's implicit loopback-only protection).
    # `--set extraAllowedHosts=null` clears the values-file list (helm's
    # `--set k={}` COERCES to a one-empty-string slice, which would leave a
    # trailing comma — the values-file [] default is the honest empty).
    rendered = subprocess_helm(
        root,
        [
            "--set", "ezua.enabled=false",
            "--set", "extraAllowedHosts=null",
        ],
    )
    assert "name: MCP_HOSTNAME" not in rendered
    assert "name: MCP_EXTRA_ALLOWED_HOSTS" not in rendered


def subprocess_helm(root, extra_args):
    import subprocess

    proc = subprocess.run(
        ["helm", "template", "t", str(root / "helm"), *extra_args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# ---------------------------------------------------------------------------
# 3. prom_rules max_rules cap
# ---------------------------------------------------------------------------


class _ManyRulesClient:
    """300+ rules in one group — the kube-prometheus-stack flood shape."""

    def __init__(self, n: int = 320):
        self.n = n

    async def rules(self):
        return [
            {
                "name": "kubernetes-apps",
                "rules": [
                    {
                        "type": "alerting",
                        "name": f"Rule{i:04d}",
                        "state": "firing" if i % 5 == 0 else "inactive",
                        "query": f"up{{pod=\"p{i}\"}} == 0",
                        "duration": 900,
                        "health": "ok",
                        "labels": {"severity": "warning"},
                        "annotations": {"summary": f"rule {i} summary"},
                    }
                    for i in range(self.n)
                ],
            }
        ]


def _rules_call(client, args):
    import server

    original = server.client
    server.client = client
    try:
        from mcp.client._memory import InMemoryTransport
        from mcp.client.session import ClientSession

        async def run():
            async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await session.call_tool("prom_rules", args)

        return asyncio.run(run()).content[0].text
    finally:
        server.client = original


def test_prom_rules_default_cap_truncates_with_footer():
    out = _rules_call(_ManyRulesClient(), {})
    assert "320 rule(s) (showing up to 50):" in out
    assert "…[truncated 270 more rules]" in out
    # exactly 50 rendered rule lines, sequential (first 50 shown)
    rule_lines = [ln for ln in out.splitlines() if "] alert:" in ln or "] record:" in ln]
    assert len(rule_lines) == 50
    assert "Rule0049" in out and "Rule0050" not in out


def test_prom_rules_max_rules_override_and_footer_arithmetic():
    out = _rules_call(_ManyRulesClient(), {"max_rules": 5})
    assert "…[truncated 315 more rules]" in out
    out = _rules_call(_ManyRulesClient(), {"max_rules": 400})
    assert "320 rule(s):" in out  # under the cap → no truncation header/footer
    assert "truncated" not in out


def test_prom_rules_bad_max_rules_is_a_clear_error():
    out = _rules_call(_ManyRulesClient(), {"max_rules": 0})
    assert out.startswith("Error:") and "max_rules" in out
    out = _rules_call(_ManyRulesClient(), {"max_rules": -3})
    assert out.startswith("Error:")
    # a float fails schema validation upstream (pydantic) — still an Error:* reply
    out = _rules_call(_ManyRulesClient(), {"max_rules": 1.5})
    assert out.startswith("Error")


def test_prom_rules_cap_counts_matched_not_shown():
    """The total counts MATCHED rules (post-filter), not rendered ones."""
    out = _rules_call(_ManyRulesClient(), {"state": "firing", "max_rules": 3})
    # 320/5 = 64 firing rules matched, 3 rendered
    assert "64 rule(s) (showing up to 3):" in out
    assert "…[truncated 61 more rules]" in out


def test_prom_rules_small_ruleset_header_unchanged():
    """The existing header/footer contract for small rule sets is untouched."""

    class _Two:
        async def rules(self):
            return [
                {
                    "name": "g",
                    "rules": [
                        {"type": "alerting", "name": "A", "state": "firing", "query": "up == 0", "health": "ok"},
                        {"type": "recording", "name": "R", "query": "sum(up)", "health": "ok"},
                    ],
                }
            ]

    out = _rules_call(_Two(), {})
    assert out.startswith("2 rule(s):")
    assert "truncated" not in out


# ---------------------------------------------------------------------------
# 4. Saved-store per-replica honesty
# ---------------------------------------------------------------------------


def test_storage_facts_default_warns_and_shared_silences():
    facts = saved_queries.storage_facts({})
    assert facts == {
        "path": None,
        "shared": False,
        "warning": "saved queries live on this replica only (PROMETHEUS_SAVED_QUERIES_SHARED=0)"
        " — multi-replica deployments fragment stores; see README",
    }
    for raw in ("1", "true", "YES", "on"):
        assert saved_queries.storage_facts({"PROMETHEUS_SAVED_QUERIES_SHARED": raw}) == {
            "path": None,
            "shared": True,
        }, raw
    # garbage stays false (fail-honest)
    assert saved_queries.storage_shared({"PROMETHEUS_SAVED_QUERIES_SHARED": "0"}) is False
    assert saved_queries.storage_shared({"PROMETHEUS_SAVED_QUERIES_SHARED": "junk"}) is False


def test_query_save_result_carries_storage_block(monkeypatch, app):
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_SHARED", raising=False)
    saved_queries.reset_store()
    try:
        out = _save(app)
        assert "Storage: shared=false — in-memory" in out
        assert "Warning: saved queries live on this replica only" in out
        assert "PROMETHEUS_SAVED_QUERIES_SHARED" in out and "README" in out

        # durable path shows the path; shared=1 silences the warning
        monkeypatch.setenv("PROMETHEUS_SAVED_QUERIES_PATH", "/tmp/prom-mcp-test-store.json")
        monkeypatch.setenv("PROMETHEUS_SAVED_QUERIES_SHARED", "1")
        saved_queries.reset_store()
        out = _save(app)
        assert "Storage: shared=true — /tmp/prom-mcp-test-store.json" in out
        assert "Warning:" not in out
    finally:
        saved_queries.reset_store()


def _save(client):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query_save", "arguments": {"name": "s", "query": "up"}}},
        headers=MCP_ACCEPT,
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]["content"][0]["text"]


def test_shared_env_is_read_per_call(monkeypatch):
    """The fleet env pattern: flip the env without reimporting."""
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_SHARED", raising=False)
    assert saved_queries.storage_shared() is False
    monkeypatch.setenv("PROMETHEUS_SAVED_QUERIES_SHARED", "1")
    assert saved_queries.storage_shared() is True
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_SHARED")
    assert saved_queries.storage_shared() is False


# ---------------------------------------------------------------------------
# 5. CORS removal — the wildcard middleware is gone
# ---------------------------------------------------------------------------


def test_no_wildcard_cors_middleware():
    """S-6: allow_origins=['*'] let any website read query results
    cross-origin. The block is DELETED, not configured away — pin the
    absence of the middleware AND its import."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    assert "CORSMiddleware(" not in source, "the wildcard CORS block must stay deleted (fleet S-6)"
    assert "from starlette.middleware.cors" not in source
    assert "add_middleware" not in source, "no CORS middleware is re-added anywhere"
    assert "No CORSMiddleware" in source, "keep the S-6 precedent comment"
