"""Wave-3 hardening tests: impersonation-flag denylist, parallelized -A
fan-out, cluster-wide list caps, and the opt-in /metrics endpoint.

Companion to the ported 168-check adversarial suite
(test_namespace_policy.py) — these cover the NEW server behaviors, which
the original suite predates.
"""

import asyncio
import importlib
import os
import sys
import types

import pytest

import test_namespace_policy as _suite
from test_namespace_policy import _asgi_collect

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ══════════════════════════════════════════════════════════════════════════
# 1. Impersonation-flag denylist (defense in depth; =-forms + adjacent)
# ══════════════════════════════════════════════════════════════════════════

_IMPERSONATION_COMMANDS = (
    # space-separated forms
    "get pods --as evil",
    "get pods --as-group system:masters",
    "get pods --as-uid 1234",
    # every =-joined form (the denylist splits on the first '=')
    "get pods --as=evil",
    "get pods --as-group=system:masters",
    "get pods --as-uid=1234",
    # empty-value =-forms are still the impersonation flag
    "get pods --as=",
    "get pods --as-group=",
    "get pods --as-uid=",
    # impersonation on other read verbs / mid-argv positions
    "describe pod x --as=evil",
    "top pods --as-group=system:masters -A",
    "get pods -o wide --as-uid=1234 --sort-by=.metadata.name",
)


class TestImpersonationDenylist:
    @pytest.mark.parametrize("command", _IMPERSONATION_COMMANDS)
    def test_impersonation_flag_rejected(self, server, command):
        with pytest.raises(server.KubectlError, match="is not allowed"):
            server._parse_kubectl_command(command)

    def test_denylist_members_exact(self, server):
        for flag in ("--as", "--as-group", "--as-uid"):
            assert flag in server.KUBECTL_UNSAFE_FLAGS, f"{flag} must be denylisted"

    def test_flag_adjacent_tokens_are_not_false_denied(self, server):
        # Only the exact flag names are denied — a lookalike token passes the
        # guard (and is then rejected by kubectl's own unknown-flag parsing).
        assert server._parse_kubectl_command("get pods --as-evil") == ["get", "pods", "--as-evil"]
        assert server._parse_kubectl_command("get pods --assistant=x") == ["get", "pods", "--assistant=x"]

    def test_legitimate_equals_joined_flags_still_pass(self, server):
        argv = server._parse_kubectl_command(
            "get pods --field-selector=spec.nodeName=n1 -o custom-columns=A:.metadata.name --sort-by=.metadata.name"
        )
        assert argv[0] == "get" and "--field-selector=spec.nodeName=n1" in argv

    def test_run_kubectl_tool_surfaces_the_deny(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        out = asyncio.run(server.run_kubectl("get pods --as=evil"))
        assert out.startswith("Error:") and "--as" in out, (
            "run_kubectl rejects the impersonation attempt before any kubectl spawn"
        )

    def test_impersonation_still_denied_under_cluster_wide_rewrite(self, server, monkeypatch):
        # Even when the namespace policy would rewrite -A per namespace, the
        # flag denylist fires first (parse time, before any plan/spawn).
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-a")
        with pytest.raises(server.KubectlError, match="is not allowed"):
            server._parse_kubectl_command("get pods -A --as=evil")


# ══════════════════════════════════════════════════════════════════════════
# 2. -A fan-out: asyncio.gather + bounded semaphore, byte-identical merge
# ══════════════════════════════════════════════════════════════════════════

_NS = ("team-alpha", "team-bravo", "team-charlie", "team-delta", "team-echo")


class _FanoutRecorder:
    """Fake _kubectl_run: tracks peak concurrency, delays per namespace so
    completions finish in REVERSE plan order (proves the merge order comes
    from the plan, not from completion timing)."""

    def __init__(self, fail_namespace=None):
        self.active = 0
        self.max_active = 0
        self.fail_namespace = fail_namespace

    async def __call__(self, argv):
        ns = argv[argv.index("-n") + 1]
        delay = 0.002 * (_NS.index(ns) + 1)  # first namespace sleeps longest
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(delay)
            if ns == self.fail_namespace:
                return 1, "", f"exit code 1 for {ns}"
            return 0, f"PODS-IN-{ns}", ""
        finally:
            self.active -= 1


def _expected_sections(order):
    blocks = []
    for ns in order:
        body = f"Error: exit code 1 for {ns}" if ns == getattr(_FanoutRecorder, "_failed", None) else f"PODS-IN-{ns}"
        blocks.append(f"=== namespace {ns} ===\n{body}")
    return "\n".join(blocks)


class TestClusterWideFanout:
    @pytest.fixture(autouse=True)
    def _setup(self, server, monkeypatch):
        self.server = server
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", ",".join(_NS))

    def _run(self, recorder, concurrency):
        self.monkeypatch.setenv("K8S_MCP_LIST_CONCURRENCY", str(concurrency))
        self.monkeypatch.setattr(self.server, "_kubectl_run", recorder)
        return asyncio.run(self.server.run_kubectl("get pods -A"))

    def test_concurrency_env_default_is_8(self):
        os.environ.pop("K8S_MCP_LIST_CONCURRENCY", None)
        assert self.server._list_concurrency() == 8, "default fan-out width is 8"
        for garbage in ("banana", "", "  ", "3.5"):
            os.environ["K8S_MCP_LIST_CONCURRENCY"] = garbage
            assert self.server._list_concurrency() == 8, f"malformed {garbage!r} falls back to default"
        os.environ["K8S_MCP_LIST_CONCURRENCY"] = "0"
        assert self.server._list_concurrency() == 1, "clamped to >= 1"
        os.environ["K8S_MCP_LIST_CONCURRENCY"] = "4"
        assert self.server._list_concurrency() == 4, "honors a valid width"
        os.environ.pop("K8S_MCP_LIST_CONCURRENCY", None)

    def test_output_byte_identical_concurrency_1_vs_8(self, monkeypatch):
        rec1, rec8 = _FanoutRecorder(), _FanoutRecorder()
        out1 = self._run(rec1, 1)
        out8 = self._run(rec8, 8)
        assert out1 == out8, "merged output must be byte-identical at concurrency 1 and 8"
        for ns in _NS:
            assert f"=== namespace {ns} ===\nPODS-IN-{ns}" in out8, f"section for {ns} present"
        # namespace-sorted section order (== the plan's order)
        positions = [out8.index(f"=== namespace {ns} ===") for ns in _NS]
        assert positions == sorted(positions), "sections stay in namespace order"

    def test_concurrency_8_actually_parallelizes(self, monkeypatch):
        rec = _FanoutRecorder()
        self._run(rec, 8)
        assert rec.max_active > 1, f"fan-out must overlap kubectl spawns (peak active={rec.max_active})"

    def test_concurrency_1_is_sequential(self, monkeypatch):
        rec = _FanoutRecorder()
        self._run(rec, 1)
        assert rec.max_active == 1, "semaphore width 1 = strictly sequential"

    def test_bounded_semaphore_caps_overlap(self, monkeypatch):
        rec = _FanoutRecorder()
        self._run(rec, 2)
        assert rec.max_active <= 2, "overlap never exceeds the configured width"

    def test_error_sections_identical_across_concurrency(self, monkeypatch):
        rec1, rec8 = _FanoutRecorder(fail_namespace="team-charlie"), _FanoutRecorder(fail_namespace="team-charlie")
        out1 = self._run(rec1, 1)
        out8 = self._run(rec8, 8)
        assert out1 == out8, "error-bearing merges are byte-identical too"
        assert "=== namespace team-charlie ===\nError:" in out8, "error isolated to its section"
        assert "PODS-IN-team-delta" in out8, "other namespaces unaffected by one failure"

    def test_out_of_order_completion_still_sorted(self, monkeypatch):
        # The recorder delays EARLY namespaces longest, so at concurrency 8 the
        # later namespaces complete first — the merge must still be plan-ordered.
        rec = _FanoutRecorder()
        out8 = self._run(rec, 8)
        positions = [out8.index(f"=== namespace {ns} ===") for ns in _NS]
        assert positions == sorted(positions), "completion order does not leak into output order"


# ══════════════════════════════════════════════════════════════════════════
# 3. Cluster-wide list caps (D9): K8S_MCP_MAX_LIST_ITEMS, default 500
# ══════════════════════════════════════════════════════════════════════════


def _pod(ns, name, phase="Running"):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=ns, name=name),
        status=types.SimpleNamespace(phase=phase, container_statuses=None, start_time=None),
    )


def _svc(ns, name):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=ns, name=name),
        spec=types.SimpleNamespace(type="ClusterIP", cluster_ip="10.0.0.1", ports=[]),
    )


def _cr(ns, name):
    return types.SimpleNamespace(
        to_dict=lambda ns=ns, name=name: {"metadata": {"name": name, "namespace": ns}, "spec": {}}
    )


class TestClusterWideListCaps:
    @pytest.fixture(autouse=True)
    def _setup(self, server, monkeypatch):
        self.server = server
        self.monkeypatch = monkeypatch
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)

    def _cap(self, value):
        if value is None:
            self.monkeypatch.delenv("K8S_MCP_MAX_LIST_ITEMS", raising=False)
        else:
            self.monkeypatch.setenv("K8S_MCP_MAX_LIST_ITEMS", str(value))

    def test_default_cap_is_500(self):
        os.environ.pop("K8S_MCP_MAX_LIST_ITEMS", None)
        assert self.server._max_list_items() == 500, "default cluster-wide cap is 500"

    def test_cap_hit_marker_present(self, monkeypatch):
        self._cap(2)
        monkeypatch.setattr(
            self.server.v1,
            "list_pod_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[_pod("ns-a", f"p{i}") for i in (1, 2, 3, 4, 5)]),
            raising=False,
        )
        out = asyncio.run(self.server.list_pods())
        assert "K8S_MCP_MAX_LIST_ITEMS" in out, "marker names the env"
        assert "3 more items" in out, "marker says how many items were withheld"
        assert "PODS (2):" in out, "header counts the capped items"
        assert "ns-a/p2" in out and "ns-a/p3" not in out, "exactly the first cap items are kept"

    def test_below_cap_unchanged(self, monkeypatch):
        self._cap(None)
        monkeypatch.setattr(
            self.server.v1,
            "list_pod_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[_pod("ns-a", f"p{i}") for i in (1, 2, 3)]),
            raising=False,
        )
        out = asyncio.run(self.server.list_pods())
        assert out == (
            "PODS (3):\n"
            "  ns-a/p1: Running | Restarts: 0 | Age: Unknown\n"
            "  ns-a/p2: Running | Restarts: 0 | Age: Unknown\n"
            "  ns-a/p3: Running | Restarts: 0 | Age: Unknown"
        ), "below the cap the output is byte-identical to the uncapped form"

    def test_namespaced_listings_are_never_capped(self, monkeypatch):
        self._cap(1)
        monkeypatch.setattr(
            self.server.v1,
            "list_namespaced_pod",
            lambda ns, **kw: types.SimpleNamespace(items=[_pod("team-a", "p1"), _pod("team-a", "p2")]),
            raising=False,
        )
        out = asyncio.run(self.server.list_pods(namespace="team-a"))
        assert "PODS (2):" in out and "team-a/p2" in out and "K8S_MCP_MAX_LIST_ITEMS" not in out, (
            "the cap applies to cluster-wide listings only"
        )

    def test_services_cap_hit(self, monkeypatch):
        self._cap(1)
        monkeypatch.setattr(
            self.server.v1,
            "list_service_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[_svc("ns-a", f"s{i}") for i in (1, 2, 3)]),
            raising=False,
        )
        out = asyncio.run(self.server.list_services())
        assert "SERVICES (1):" in out and "K8S_MCP_MAX_LIST_ITEMS" in out, "services cap + marker"

    def test_workloads_cap_hit(self, monkeypatch):
        self._cap(1)
        dep = lambda i: types.SimpleNamespace(
            metadata=types.SimpleNamespace(namespace="ns-a", name=f"d{i}"),
            status=types.SimpleNamespace(ready_replicas=1),
            spec=types.SimpleNamespace(replicas=1),
        )
        monkeypatch.setattr(
            self.server.apps_v1,
            "list_deployment_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[dep(i) for i in (1, 2)]),
            raising=False,
        )
        monkeypatch.setattr(
            self.server.apps_v1,
            "list_stateful_set_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[]),
            raising=False,
        )
        monkeypatch.setattr(
            self.server.apps_v1,
            "list_daemon_set_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[]),
            raising=False,
        )
        monkeypatch.setattr(
            self.server.batch_v1,
            "list_job_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=[]),
            raising=False,
        )
        out = asyncio.run(self.server.list_workloads())
        assert "Deployment ns-a/d1:" in out and "d2" not in out, "each workload section is capped"
        assert "K8S_MCP_MAX_LIST_ITEMS" in out, "marker present"

    def test_virtualservices_cap_keeps_sorted_head(self, monkeypatch):
        self._cap(1)
        vs = [
            {
                "metadata": {"name": "other", "namespace": "team-b", "creationTimestamp": None},
                "spec": {"hosts": ["b.example.com"], "http": []},
            },
            {
                "metadata": {"name": "checkout", "namespace": "team-a", "creationTimestamp": None},
                "spec": {"hosts": ["a.example.com"], "http": []},
            },
        ]
        monkeypatch.setattr(
            self.server, "dyn_client", types.SimpleNamespace(resources=_suite._VSDiscovery(_suite._VSResource(vs)))
        )
        out = asyncio.run(self.server.list_virtual_services())
        assert "VIRTUALSERVICES across all namespaces (1):" in out, "header counts the capped items"
        assert "team-a/checkout" in out, "the sorted head is kept (cap applies after the sort)"
        assert "team-b/other" not in out, "items beyond the cap are withheld"
        assert "K8S_MCP_MAX_LIST_ITEMS" in out, "marker present"

    def test_custom_resource_cap_hit(self, monkeypatch):
        self._cap(2)

        class _FakeCRResource:
            """Serves dyn_client.resources.get (discovery) and resource.get (listing)."""

            def __init__(self, items):
                self._items = items

            def get(self, name=None, namespace=None, api_version="", plural="", **kw):
                if api_version or plural:
                    return self  # discovery call
                return types.SimpleNamespace(items=self._items)  # listing call

        monkeypatch.setattr(
            self.server,
            "dyn_client",
            types.SimpleNamespace(
                resources=types.SimpleNamespace(
                    get=lambda api_version="", plural="", **kw: _FakeCRResource(
                        [_cr("ns-a", f"c{i}") for i in (1, 2, 3)]
                    )
                )
            ),
        )
        out = asyncio.run(self.server.get_custom_resource("g.example.com", "v1", "things"))
        assert '"name": "c1"' in out and '"name": "c3"' not in out, "custom resources capped"
        assert "K8S_MCP_MAX_LIST_ITEMS" in out, "marker present"

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        os.environ["K8S_MCP_MAX_LIST_ITEMS"] = "banana"
        try:
            assert self.server._max_list_items() == 500, "malformed value keeps the default cap"
        finally:
            os.environ.pop("K8S_MCP_MAX_LIST_ITEMS", None)

    def test_nonpositive_cap_clamped_to_one(self, monkeypatch):
        for bogus in ("0", "-5"):
            os.environ["K8S_MCP_MAX_LIST_ITEMS"] = bogus
            try:
                assert self.server._max_list_items() == 1, f"{bogus} clamps to 1 (never uncapped)"
            finally:
                os.environ.pop("K8S_MCP_MAX_LIST_ITEMS", None)


# ══════════════════════════════════════════════════════════════════════════
# 4. /metrics (opt-in) + counters
# ══════════════════════════════════════════════════════════════════════════


class _MarkerApp:
    def __init__(self, sink):
        self.sink = sink

    async def __call__(self, scope, receive, send):
        self.sink.seen = scope.get("path")


class TestMetricsEndpoint:
    @pytest.fixture(autouse=True)
    def _setup(self, server, monkeypatch):
        self.server = server
        self.monkeypatch = monkeypatch

    def test_metrics_off_by_default(self):
        self.monkeypatch.delenv("K8S_MCP_METRICS_ENABLED", raising=False)
        assert self.server._metrics_enabled() is False, "metrics default OFF (additive, default-off)"

    def test_router_passes_metrics_through_when_disabled(self):
        self.monkeypatch.delenv("K8S_MCP_METRICS_ENABLED", raising=False)
        mcp_marker, ui_marker = types.SimpleNamespace(seen=None), types.SimpleNamespace(seen=None)
        router = self.server._ConsoleRouterApp(_MarkerApp(mcp_marker), _MarkerApp(ui_marker))
        asyncio.run(_asgi_collect(router, {"type": "http", "path": "/metrics"}))
        assert mcp_marker.seen == "/metrics" and ui_marker.seen is None, (
            "with metrics disabled, /metrics routes exactly as before (to the MCP app)"
        )

    def test_router_serves_metrics_when_enabled(self):
        self.monkeypatch.setenv("K8S_MCP_METRICS_ENABLED", "true")
        mcp_marker, ui_marker = types.SimpleNamespace(seen=None), types.SimpleNamespace(seen=None)
        router = self.server._ConsoleRouterApp(_MarkerApp(mcp_marker), _MarkerApp(ui_marker))
        sent = asyncio.run(_asgi_collect(router, {"type": "http", "path": "/metrics"}))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert start["status"] == 200, "GET /metrics answers 200 when enabled"
        assert dict(start["headers"])[b"content-type"].startswith(b"text/plain; version=0.0.4"), (
            "Prometheus text exposition content type"
        )
        assert b"k8s_mcp_server_events_total" in body, "counter family rendered"
        # the console still wins its own paths while metrics is on
        asyncio.run(_asgi_collect(router, {"type": "http", "path": "/ui/index.html"}))
        assert ui_marker.seen == "/ui/index.html", "/ui/ routing unchanged by metrics"

    def test_counters_increment_and_render(self):
        unique = "probe_unique_outcome"
        self.server._metrics_inc("kubectl_batch", unique)
        self.server._metrics_inc("kubectl_batch", unique)
        rendered = self.server._metrics_render().decode()
        line = [
            l
            for l in rendered.splitlines()
            if l.startswith(f'k8s_mcp_server_events_total{{kind="kubectl_batch",outcome="{unique}"}}')
        ]
        assert line, "incremented counter series present in the exposition"
        value = float(line[0].rsplit(" ", 1)[1])
        assert value >= 2, f"counter incremented (got {value})"

    def test_prometheus_client_backend_when_importable(self):
        try:
            import prometheus_client  # noqa: F401

            available = True
        except ImportError:
            available = False
        if not available:
            pytest.skip("prometheus_client not installed in this environment")
        reloaded = importlib_reload(self.server)  # self-heal any prior backend state
        assert reloaded._METRICS_BACKEND == "prometheus_client", "prometheus-client used when importable"

    def test_fallback_backend_without_prometheus_client(self):
        # Simulate the no-prometheus-client environment for a full-branch test:
        # sys.modules[name] = None makes the import raise ImportError.
        saved = sys.modules.get("prometheus_client")
        sys.modules["prometheus_client"] = None
        try:
            reloaded = importlib.reload(self.server)
            assert reloaded._METRICS_BACKEND == "stdlib", "dependency-free fallback engages"
            reloaded._metrics_inc("probe_kind", "probe_fallback", 3)
            text = reloaded._metrics_render().decode()
            assert "# HELP k8s_mcp_server_events_total" in text, "valid exposition"
            assert 'k8s_mcp_server_events_total{kind="probe_kind",outcome="probe_fallback"} 3' in text, (
                "fallback counters render their values"
            )
        finally:
            if saved is None:
                sys.modules.pop("prometheus_client", None)
            else:
                sys.modules["prometheus_client"] = saved
            importlib.reload(self.server)  # restore the original backend


def importlib_reload(module):
    return importlib.reload(module)


# ══════════════════════════════════════════════════════════════════════════
# In-cluster Host-header allowlist (MCP_EXTRA_ALLOWED_HOSTS)
# ══════════════════════════════════════════════════════════════════════════


class TestExtraAllowedHosts:
    """The MCP SDK's DNS-rebinding protection rejects every Host header but
    the pinned public FQDN (421 AFTER auth) — MCP_EXTRA_ALLOWED_HOSTS adds
    in-cluster svc-DNS hosts so local callers (Open WebUI) can connect."""

    def test_parse_strips_dedupes_keeps_order(self, server):
        assert server._parse_extra_allowed_hosts(" a:1 , b:2 ,,a:1,c:3 ") == ["a:1", "b:2", "c:3"], (
            "whitespace/empties dropped, order kept, dupes removed"
        )
        assert server._parse_extra_allowed_hosts("") == [], "empty env = no additions"
        assert server._parse_extra_allowed_hosts(" , , ") == [], "blank entries only = no additions"

    def test_no_envs_means_sdk_default(self, server, monkeypatch):
        monkeypatch.delenv("MCP_HOSTNAME", raising=False)
        monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
        assert server._build_transport_security() is None, (
            "unset = SDK implicit loopback protection, byte-identical to the old behaviour"
        )

    def test_hostname_pin_unchanged_without_extras(self, server, monkeypatch):
        monkeypatch.setenv("MCP_HOSTNAME", "mcp.example.com")
        monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
        ts = server._build_transport_security()
        assert ts.allowed_hosts == ["mcp.example.com", "localhost:*", "127.0.0.1:*"], (
            "pinned FQDN + loopback, exactly the pre-existing allowlist"
        )
        assert ts.allowed_origins == ["https://mcp.example.com"], "origins stay the https browser form"

    def test_extras_appended_after_pin(self, server, monkeypatch):
        monkeypatch.setenv("MCP_HOSTNAME", "mcp.example.com")
        monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "svc.a.svc.cluster.local:* , svc.b:9090")
        ts = server._build_transport_security()
        assert ts.allowed_hosts == [
            "mcp.example.com",
            "svc.a.svc.cluster.local:*",
            "svc.b:9090",
            "localhost:*",
            "127.0.0.1:*",
        ], "pinned FQDN first, extras in order, loopback last"
        assert ts.allowed_origins == ["https://mcp.example.com"], "extras never widen origins"
        assert ts.enable_dns_rebinding_protection is True, "protection stays ON"

    def test_extras_without_pin_still_protected(self, server, monkeypatch):
        monkeypatch.delenv("MCP_HOSTNAME", raising=False)
        monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "svc.a:9090")
        ts = server._build_transport_security()
        assert ts.allowed_hosts == ["svc.a:9090", "localhost:*", "127.0.0.1:*"]
        assert ts.allowed_origins == []
        assert ts.enable_dns_rebinding_protection is True, "explicit allowlist keeps protection ON"

    def test_svc_dns_host_passes_and_unknown_host_still_421(self, server, monkeypatch):
        # End-to-end through the real SDK transport security: a Host in the
        # extra allowlist must NOT yield 421; an unknown Host still must.
        monkeypatch.setenv("MCP_HOSTNAME", "mcp.example.com")
        monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "k8s-mcp-service.ops.svc.cluster.local:*")
        app = server.mcp.streamable_http_app(stateless_http=True, transport_security=server._build_transport_security())

        def _scope(host):
            return {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/mcp",
                "scheme": "http",
                "server": ("testserver", 9090),
                "client": ("testclient", 123),
                "query_string": b"",
                "headers": [
                    (b"host", host.encode()),
                    (b"content-type", b"application/json"),
                    (b"accept", b"application/json, text/event-stream"),
                ],
            }

        async def _run():
            # The SDK session manager only serves inside its lifespan.
            statuses = {}
            async with app.router.lifespan_context(app):
                for label, host in (
                    ("allowed", "k8s-mcp-service.ops.svc.cluster.local:9090"),
                    ("unknown", "evil.example.com"),
                    ("pinned", "mcp.example.com"),
                ):
                    sent = await _suite._asgi_collect(app, _scope(host))
                    statuses[label] = sent[0]["status"] if sent else None
            return statuses

        statuses = asyncio.run(_run())
        assert statuses["allowed"] != 421, "svc-DNS Host in the extra allowlist passes the host check"
        assert statuses["unknown"] == 421, "unknown Host still rejected with 421"
        assert statuses["pinned"] != 421, "pinned FQDN keeps working"
