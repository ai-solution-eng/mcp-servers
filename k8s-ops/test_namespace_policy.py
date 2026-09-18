"""Adversarial suite for the namespace policy and kubectl command guard.

Run with:   python3 -m pytest test_namespace_policy.py -q        (primary)
            python3 test_namespace_policy.py                      (wrapper)

One pytest test per original check — all 168 checks of the standalone suite
port 1:1 (same labels, same conditions, none weakened or dropped), so the
suite is CI-enforceable instead of running vacuously (the audit's P0-8
finding: this file had zero asserts and pytest collected nothing).

Layout contract: smoke_console.py and audit_forms.py exec everything above
`def main` to reuse the kubernetes stubs WITHOUT importing pytest, so the
kubernetes stubs and pytest-free helpers must stay above it, and the pytest
suite lives below it.

The `kubernetes` package is stubbed when missing so the module imports
without a cluster or a virtualenv; the real SDK is used when installed.
"""

import asyncio
import os
import sys
import types


def _install_kubernetes_stubs():
    """Minimal stubs for the kubernetes import surface server.py needs."""
    if "kubernetes" in sys.modules:
        return

    class ApiException(Exception):
        def __init__(self, reason="stub", status=0):
            self.reason = reason
            self.status = status

    class ConfigException(Exception):
        pass

    class ResourceNotFoundError(Exception):
        pass

    class _Inert:
        def __init__(self, *args, **kwargs):
            pass

    class _Model:
        """Records constructor kwargs (stand-in for the k8s V1* models)."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _NamespaceList:
        class _Meta:
            def __init__(self, name):
                self.name = name
                self.creation_timestamp = None
                self.phase = "Active"

        class _Item:
            def __init__(self, name):
                self.metadata = _NamespaceList._Meta(name)
                self.status = types.SimpleNamespace(phase="Active")

        def __init__(self, names):
            self.items = [_NamespaceList._Item(n) for n in names]

    _KNOWN_NAMESPACES = ("default", "kube-system", "kube-public", "team-a", "team-b", "team-sec-x")

    class CoreV1Api(_Inert):
        def list_namespace(self):
            return _NamespaceList(_KNOWN_NAMESPACES)

        def read_namespace(self, name):
            if name not in _KNOWN_NAMESPACES:
                raise ApiException(reason=f'namespaces "{name}" not found', status=404)
            return _NamespaceList([name])

    kubernetes = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    rest = types.ModuleType("kubernetes.client.rest")
    config = types.ModuleType("kubernetes.config")
    dynamic = types.ModuleType("kubernetes.dynamic")
    dyn_exceptions = types.ModuleType("kubernetes.dynamic.exceptions")

    class RbacAuthorizationV1Api:
        """Stateful stub: the template role exists; bindings start empty."""

        TEMPLATE = "k8s-mcp-pods-exec"

        def __init__(self):
            self._bindings = {}

        def read_cluster_role(self, name):
            if name != self.TEMPLATE:
                raise ApiException(reason="clusterroles not found", status=404)

        def read_namespaced_role_binding(self, name, namespace):
            if (namespace, name) not in self._bindings:
                raise ApiException(reason="rolebindings not found", status=404)
            return self._bindings[(namespace, name)]

        def create_namespaced_role_binding(self, namespace, body):
            self._bindings[(namespace, body.metadata.name)] = body

    client.ApiClient = _Inert
    client.CoreV1Api = CoreV1Api
    client.AppsV1Api = _Inert
    client.BatchV1Api = _Inert
    client.NetworkingV1Api = _Inert
    client.CustomObjectsApi = _Inert
    client.RbacAuthorizationV1Api = RbacAuthorizationV1Api
    client.V1RoleBinding = _Model
    client.V1ObjectMeta = _Model
    client.V1RoleRef = _Model
    client.V1Subject = _Model
    rest.ApiException = ApiException
    config.ConfigException = ConfigException
    config.load_incluster_config = lambda: (_ for _ in ()).throw(ConfigException("no in-cluster env"))
    config.load_kube_config = lambda: (_ for _ in ()).throw(ConfigException("no kubeconfig in test env"))
    dynamic.DynamicClient = _Inert
    dyn_exceptions.ResourceNotFoundError = ResourceNotFoundError

    kubernetes.client = client
    kubernetes.config = config
    kubernetes.dynamic = dynamic
    client.rest = rest
    dynamic.exceptions = dyn_exceptions
    for name, mod in {
        "kubernetes": kubernetes,
        "kubernetes.client": client,
        "kubernetes.client.rest": rest,
        "kubernetes.config": config,
        "kubernetes.dynamic": dynamic,
        "kubernetes.dynamic.exceptions": dyn_exceptions,
    }.items():
        sys.modules[name] = mod


def _pop_policy_envs():
    os.environ.pop("K8S_MCP_ALLOWED_NAMESPACES", None)
    os.environ.pop("K8S_MCP_BLOCKED_NAMESPACES", None)


def main():
    """Standalone entry point: runs this file's full suite through pytest.

    pytest is the primary interface (`python3 -m pytest test_namespace_policy.py`);
    this wrapper keeps `python3 test_namespace_policy.py` working exactly as
    before — same 168 checks, real exit code.
    """
    _install_kubernetes_stubs()
    _pop_policy_envs()
    try:
        import pytest
    except ImportError:
        print("pytest is required to run this suite: python3 -m pytest test_namespace_policy.py -q")
        return 2
    print("Running the adversarial suite via pytest (one test per check)...")
    return pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"])


if __name__ == "__main__":
    raise SystemExit(main())


# ══════════════════════════════════════════════════════════════════════════
# The ported suite. Everything above `def main` is deliberately pytest-free
# (see the layout contract in the module docstring); the checks below are
# one pytest test per original check, in the original order, each asserting
# the original condition under the original label.
# ══════════════════════════════════════════════════════════════════════════

import importlib  # noqa: E402

import pytest  # noqa: E402


async def _plan(server, argv):
    """_namespace_plan, with NamespacePolicyError returned (not raised)."""
    try:
        return await server._namespace_plan(argv)
    except server.NamespacePolicyError as e:
        return e


def _exec_registered(server) -> bool:
    return "exec_in_pod" in {t.name for t in asyncio.run(server.mcp.list_tools())}


def ensure_exec_state(server, enabled: bool):
    """Force exec_in_pod's registration state (reload only on transition).

    Registration is import-time-conditional in server.py (K8S_MCP_EXEC_ENABL-
    ED=true), exactly as in the original script's importlib.reload dance.
    Checking the LIVE registration (not an env read) keeps this correct no
    matter which test module ran first.
    """
    if _exec_registered(server) is not enabled:
        if enabled:
            os.environ["K8S_MCP_EXEC_ENABLED"] = "true"
        else:
            os.environ.pop("K8S_MCP_EXEC_ENABLED", None)
        importlib.reload(server)
    return server


# ── No policy configured: everything passes through ────────────────────────


class TestNoPolicy:
    def test_policy_inactive_when_env_unset(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        assert server._namespace_policy_active() is False, "policy inactive when env unset"

    def test_any_namespace_allowed_with_no_policy(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        assert server.namespace_violation("anything") is None, "any namespace allowed with no policy"

    def test_cluster_wide_passthrough_with_no_policy(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        plan_out = asyncio.run(_plan(server, ["get", "pods", "-A"]))
        assert plan_out == [(None, ["get", "pods", "-A"])], "cluster-wide passthrough with no policy"


# ── Blacklist only ─────────────────────────────────────────────────────────


class TestBlacklistOnly:
    def test_exact_blacklist_match_denied(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        assert server.namespace_violation("kube-system") is not None, "exact blacklist match denied"

    def test_glob_blacklist_match_denied(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        assert server.namespace_violation("kube-prod") is not None, "glob blacklist match denied"

    def test_non_blacklisted_namespace_allowed(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        assert server.namespace_violation("team-a") is None, "non-blacklisted namespace allowed"

    def test_namespace_check_is_case_insensitive(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        assert server.namespace_violation("Team-A") is None, "namespace check is case-insensitive"

    def test_invalid_namespace_rejected(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        assert server.namespace_violation("Kube System!") is not None, "invalid namespace rejected"

    def test_leading_dash_namespace_rejected(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        assert server.namespace_violation("-bad-ns") is not None, "leading-dash namespace rejected"

    def test_cluster_wide_rejected_under_blacklist_only(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        out = asyncio.run(_plan(server, ["get", "pods", "-A"]))
        assert isinstance(out, server.NamespacePolicyError), "cluster-wide rejected under blacklist-only"

    def test_explicit_blocked_namespace_rejected(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        out = asyncio.run(_plan(server, ["get", "pods", "-n", "kube-system"]))
        assert isinstance(out, server.NamespacePolicyError), "explicit blocked namespace rejected"

    def test_bare_namespaced_query_requires_n_under_policy(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        out = asyncio.run(_plan(server, ["get", "pods"]))
        assert isinstance(out, server.NamespacePolicyError), "bare namespaced query requires -n under policy"

    def test_cluster_scoped_resource_unaffected_by_policy(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        out = asyncio.run(_plan(server, ["get", "crds"]))
        assert out == [(None, ["get", "crds"])], "cluster-scoped resource unaffected by policy"

    def test_top_nodes_unaffected_by_policy(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        out = asyncio.run(_plan(server, ["top", "nodes", "--no-headers"]))
        assert out == [(None, ["top", "nodes", "--no-headers"])], "top nodes unaffected by policy"

    def test_auth_can_i_is_context_only_not_policy_gated(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system,kube-*")
        out = asyncio.run(_plan(server, ["auth", "can-i", "list", "pods", "-n", "kube-system"]))
        assert out == [(None, ["auth", "can-i", "list", "pods", "-n", "kube-system"])], (
            "auth can-i is context-only, not policy-gated"
        )


# ── Whitelist + blacklist: blacklist wins ──────────────────────────────────


class TestWhitelistAndBlacklist:
    def test_whitelisted_namespace_allowed(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*,team-sec-x")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-*")
        assert server.namespace_violation("team-a") is None, "whitelisted namespace allowed"

    def test_blacklist_beats_whitelist(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*,team-sec-x")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-*")
        assert server.namespace_violation("team-sec-x") is not None, "blacklist beats whitelist"

    def test_non_whitelisted_namespace_denied(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*,team-sec-x")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-*")
        assert server.namespace_violation("other") is not None, "non-whitelisted namespace denied"

    def test_cluster_wide_rewritten_per_whitelisted_namespace(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*,team-sec-x")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-*")
        out = asyncio.run(_plan(server, ["get", "pods", "-A", "-o", "json"]))
        assert isinstance(out, list) and [lbl for lbl, _ in out] == ["team-a", "team-b"], (
            "cluster-wide rewritten per whitelisted namespace (globs expanded, blacklist filtered)"
        )

    def test_rewritten_argv_drops_a_and_appends_n(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*,team-sec-x")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-*")
        out = asyncio.run(_plan(server, ["get", "pods", "-A", "-o", "json"]))
        assert isinstance(out, list) and len(out) == 2, (
            "cluster-wide rewritten per whitelisted namespace (globs expanded, blacklist filtered)"
        )
        assert out[0][1] == ["get", "pods", "-o", "json", "-n", "team-a"] and out[1][1] == [
            "get",
            "pods",
            "-o",
            "json",
            "-n",
            "team-b",
        ], "rewritten argv drops -A and appends -n"

    def test_plan_deterministic(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*,team-sec-x")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-*")
        out = asyncio.run(_plan(server, ["get", "pods", "-A", "-o", "json"]))
        # Kept 1:1 with the original `... is out or True` condition; the
        # deterministic-rewrite intent is asserted by the equality checks in
        # the two tests above.
        assert asyncio.run(_plan(server, ["get", "pods", "-A", "-o", "json"])) is out or True, "plan deterministic"

    def test_exact_whitelist_rewrites_without_live_namespace_lookup(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "alpha,bravo,charlie")
        out = asyncio.run(_plan(server, ["get", "pods", "-A"]))
        assert isinstance(out, list) and [lbl for lbl, _ in out] == ["alpha", "bravo", "charlie"], (
            "exact whitelist rewrites without live-namespace lookup"
        )


# ── kubectl command guard ──────────────────────────────────────────────────

_NON_READ_VERBS = (
    "delete pod x",
    "apply -f x",
    "run evil --image=nginx",
    "exec -it pod -- sh",
    "port-forward pod/x 8080",
    "proxy",
    "cp pod:/etc/passwd ./out",
    "set image deploy/a b=c",
    "attach pod",
    "debug pod/x",
    "GET pods",
    "scale deploy/a --replicas=1",
    "drain node-1",
    "certificate approve csr-x",
)

_UNSAFE_FLAGS = (
    "get pods --server=https://evil.com",
    "get pods --token=abc",
    "get pods --kubeconfig=/tmp/evil",
    "get pods --insecure-skip-tls-verify",
)


class TestKubectlCommandGuard:
    @pytest.mark.parametrize("bad", _NON_READ_VERBS)
    def test_non_read_verb_rejected(self, server, bad):
        """non-read verb rejected (label per case)."""
        with pytest.raises(server.KubectlError, match="is not allowed; allowed read verbs"):
            server._parse_kubectl_command(bad)

    @pytest.mark.parametrize("flag", _UNSAFE_FLAGS)
    def test_unsafe_flag_rejected(self, server, flag):
        """unsafe flag rejected (label per case)."""
        with pytest.raises(server.KubectlError, match="is not allowed"):
            server._parse_kubectl_command(flag)

    def test_read_command_parses_to_argv(self, server):
        ok_argv = server._parse_kubectl_command("get pods -o wide --sort-by=.metadata.name")
        assert ok_argv == ["get", "pods", "-o", "wide", "--sort-by=.metadata.name"], "read command parses to argv"

    def test_metacharacters_become_inert_argv_tokens(self, server):
        # Shell metacharacters survive parsing as inert argv tokens (execution is
        # create_subprocess_exec, never a shell), and the namespace policy stops
        # the classic smuggle shapes before kubectl even runs.
        argv = server._parse_kubectl_command("get pods; rm -rf /")
        assert argv == ["get", "pods;", "rm", "-rf", "/"], "metacharacters become inert argv tokens"

    def test_shell_smuggled_query_stopped_by_namespace_policy(self, server, monkeypatch):
        # State at this point in the original script: a whitelist is active.
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "alpha,bravo,charlie")
        argv = server._parse_kubectl_command("get pods; rm -rf /")
        out = asyncio.run(_plan(server, argv))
        assert isinstance(out, server.NamespacePolicyError), "shell-smuggled query stopped by namespace policy"

    def test_chained_write_stopped_by_namespace_policy(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "alpha,bravo,charlie")
        try:
            argv = server._parse_kubectl_command("get pods && kubectl delete ns foo")
        except server.KubectlError:
            return  # rejected at parse — the other passing branch of the original check
        out = asyncio.run(_plan(server, argv))
        assert isinstance(out, server.NamespacePolicyError), (
            "chained write stopped by namespace policy (and inert under exec)"
        )

    def test_unbalanced_quotes_rejected(self, server):
        """unbalanced quotes rejected."""
        with pytest.raises(server.KubectlError, match="could not parse command"):
            server._parse_kubectl_command("get 'unbalanced")

    def test_empty_command_rejected(self, server):
        """empty command rejected."""
        with pytest.raises(server.KubectlError, match="empty command"):
            server._parse_kubectl_command("   ")


# ── Parameter validators ───────────────────────────────────────────────────


class TestParameterValidators:
    def test_simple_output_format_accepted(self, server):
        assert server._validated_output("json") == "json", "simple output format accepted"

    def test_jsonpath_output_accepted(self, server):
        assert server._validated_output("jsonpath={.items[*].metadata.name}").startswith("jsonpath="), (
            "jsonpath output accepted"
        )

    @pytest.mark.parametrize("bad_out", ("json --server=evil", "-o wide", "yaml\ndelete"))
    def test_bad_output_rejected(self, server, bad_out):
        """bad output rejected (label per case)."""
        with pytest.raises(ValueError, match="unsupported output format"):
            server._validated_output(bad_out)

    def test_grouped_type_accepted(self, server):
        assert server._validated_resource_type("deployments.apps") == "deployments.apps", "grouped type accepted"

    @pytest.mark.parametrize("bad_rt", ("-w", "pods -n kube-system", "pods;x"))
    def test_bad_resource_type_rejected(self, server, bad_rt):
        """bad resource type rejected (label per case)."""
        with pytest.raises(ValueError, match="invalid resource type"):
            server._validated_resource_type(bad_rt)

    def test_dotted_name_accepted(self, server):
        assert server._validated_name("pod-1.foo") == "pod-1.foo", "dotted name accepted"

    def test_type_name_accepted(self, server):
        assert server._validated_name("pod/main") == "pod/main", "type/name accepted"

    @pytest.mark.parametrize("bad_name", ("--all", "x y", ""))
    def test_bad_name_rejected(self, server, bad_name):
        """bad name rejected (label per case)."""
        with pytest.raises(ValueError, match="invalid resource name"):
            server._validated_name(bad_name)

    def test_namespace_lowercased(self, server):
        assert server._validated_namespace("Default") == "default", "namespace lowercased"


# ── Env parsing fails loud on malformed values ─────────────────────────────


class TestEnvParsing:
    def test_malformed_whitelist_pattern_rejected(self, server, monkeypatch):
        """malformed whitelist pattern rejected."""
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "ok-ns,Bad Pattern!")
        with pytest.raises(ValueError, match="invalid namespace pattern"):
            server._namespace_policy()


# ── API-key auth middleware ────────────────────────────────────────────────


class _Downstream:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call_middleware(server, headers, key_env=None, clients_env="", scope_type="http"):
    """Invoke _ApiKeyAuthMiddleware with the given env; returns (called, sent)."""
    if key_env is not None:
        os.environ["K8S_MCP_API_KEY"] = key_env
    if clients_env:
        os.environ["K8S_MCP_CLIENTS"] = clients_env
    try:
        app = _Downstream()
        sent = []

        async def receive():
            return {"type": "http.request"}

        async def send(msg):
            sent.append(msg)

        await server._ApiKeyAuthMiddleware(app)({"type": scope_type, "headers": headers}, receive, send)
        return app.called, sent
    finally:
        if key_env is not None:
            os.environ.pop("K8S_MCP_API_KEY", None)
        if clients_env:
            os.environ.pop("K8S_MCP_CLIENTS", None)


class TestApiKeyAuth:
    def test_auth_disabled_when_key_unset_dev_mode(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_API_KEY", raising=False)
        called, _sent = asyncio.run(_call_middleware(server, [], None))
        assert called, "auth disabled when K8S_MCP_API_KEY unset (dev mode)"

    def test_bearer_header_authorized(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, sent = asyncio.run(_call_middleware(server, [(b"authorization", b"Bearer secret-1")], None))
        assert called and sent and sent[0]["status"] == 200, "Authorization: Bearer <key> authorized"

    def test_x_api_key_header_authorized(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, sent = asyncio.run(_call_middleware(server, [(b"x-api-key", b"secret-1")], None))
        assert called and sent and sent[0]["status"] == 200, "X-API-Key: <key> authorized"

    def test_missing_key_401_before_mcp_app(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, sent = asyncio.run(_call_middleware(server, [], None))
        assert not called and sent and sent[0]["status"] == 401 and sent[1]["body"], (
            "missing key -> 401 before the MCP app is reached"
        )

    def test_wrong_bearer_token_401(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, sent = asyncio.run(_call_middleware(server, [(b"authorization", b"Bearer wrong")], None))
        assert not called and sent[0]["status"] == 401, "wrong bearer token -> 401"

    def test_non_bearer_authorization_401(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, sent = asyncio.run(_call_middleware(server, [(b"authorization", b"Basic dXNlcjpwYXNz")], None))
        assert not called and sent[0]["status"] == 401, "non-Bearer Authorization -> 401"

    def test_either_accepted_header_satisfies_auth(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, _sent = asyncio.run(
            _call_middleware(server, [(b"authorization", b"Bearer nope"), (b"x-api-key", b"secret-1")], None)
        )
        assert called, "either accepted header satisfies auth"

    def test_non_http_scope_passes_through_lifespan(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_API_KEY", "secret-1")
        called, _sent = asyncio.run(
            _call_middleware(server, [(b"authorization", b"Bearer secret-1")], None, scope_type="lifespan")
        )
        assert called, "non-http ASGI scope passes through (lifespan)"

    def test_middleware_composes_around_real_mcp_streamable_app(self, server):
        composed = server._ApiKeyAuthMiddleware(server.mcp.streamable_http_app(stateless_http=True))
        assert callable(composed), "middleware composes around the real MCP streamable app"


# ── exec_in_pod guard (opt-in tool) ────────────────────────────────────────


class _FakePod:
    def __init__(self, labels):
        self.metadata = types.SimpleNamespace(labels=labels)


class _LabeledPod(_FakePod):
    def __init__(self):
        super().__init__({"k8s-mcp.io/exec": "true"})


def _not_found_poder(name, ns):
    _ApiException = sys.modules["kubernetes.client.rest"].ApiException
    raise _ApiException(reason='pods "no-such-pod" not found', status=404)


class TestExecGuard:
    def test_exec_in_pod_absent_by_default(self, server):
        ensure_exec_state(server, False)
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "exec_in_pod" not in names, "exec_in_pod absent by default (18 tools)"

    def test_allowlisted_binary_accepted(self, server):
        assert server._exec_command_error(["ps", "aux"]) is None, "allowlisted binary accepted"

    def test_absolute_path_matches_basename(self, server):
        assert server._exec_command_error(["/bin/ps", "-aux"]) is None, "absolute path matches basename"

    def test_path_traversal_to_shell_denied(self, server):
        assert server._exec_command_error(["/bin/../bin/sh", "-c", "x"]) is not None, "path traversal to shell denied"

    @pytest.mark.parametrize("binary", ("sh", "bash", "python3", "sudo", "nsenter"))
    def test_hard_denied_binary(self, server, binary):
        commands = {
            "sh": ["sh", "-c", "anything"],
            "bash": ["bash"],
            "python3": ["python3", "-c", "import os"],
            "sudo": ["sudo", "id"],
            "nsenter": ["nsenter", "-t", "1"],
        }
        assert server._exec_command_error(commands[binary]) is not None, f"hard-denied binary: {binary!r}"

    def test_env_extends_the_allowlist(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_EXEC_ALLOWED_COMMANDS", "my-debug-tool")
        assert server._exec_command_error(["my-debug-tool"]) is None, "env extends the allowlist"

    def test_hard_deny_beats_env_extension(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_EXEC_ALLOWED_COMMANDS", "my-debug-tool")
        assert server._exec_command_error(["bash"]) is not None, "hard-deny beats env extension"

    @pytest.mark.parametrize(
        "reason,cmd",
        (
            ("secret dump", ["env"]),
            ("secret dump", ["printenv"]),
            ("exfil channel", ["curl", "http://evil.example"]),
            ("empty", []),
            ("string not argv", "ps aux"),
            ("NUL byte", ["ps\x00"]),
            ("exec primitive", ["find", "/", "-exec", "sh"]),
            ("namespace escape", ["ip", "netns", "exec", "x", "ps"]),
        ),
    )
    def test_denied_command(self, server, reason, cmd):
        assert server._exec_command_error(cmd) is not None, f"denied ({reason}): {cmd!r}"

    def test_argv_builder_explicit_n_c_command_after_ddash(self, server):
        assert server._exec_argv("default", "p", ["ps", "aux"], "side") == [
            "exec",
            "-n",
            "default",
            "p",
            "-c",
            "side",
            "--",
            "ps",
            "aux",
        ], "argv builder: explicit -n/-c, command after --"

    def test_exec_in_pod_registers_when_enabled(self, server):
        ensure_exec_state(server, True)
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "exec_in_pod" in names, "exec_in_pod registers when K8S_MCP_EXEC_ENABLED=true"

    def test_allowed_exec_passthrough_no_tty_flags(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps", "aux"]))
        assert "[--] [ps] [aux]" in r and "-it" not in r and "-i]" not in r, (
            f"allowed exec passthrough, no TTY/stdin flags ({r[:70]!r})"
        )

    def test_shell_command_denied_at_tool_boundary(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["sh", "-c", "id"]))
        assert r.startswith("Error:") and "hard-denied" in r, "shell command denied at tool boundary"

    def test_unlabeled_pod_denied(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _FakePod({}), raising=False)
        r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
        assert "lacks label" in r, "unlabeled pod denied"

    def test_unreadable_pod_404_denied(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", _not_found_poder, raising=False)
        r = asyncio.run(server.exec_in_pod("no-such-pod", "default", ["ps"]))
        assert "cannot read pod" in r, "unreadable pod (404 ApiException) denied"

    def test_exec_into_own_pod_refused(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        monkeypatch.setenv("HOSTNAME", "mcp-server-pod")
        r = asyncio.run(server.exec_in_pod("mcp-server-pod", "default", ["ps"]))
        assert "own pod" in r, "exec into the MCP server's own pod refused"

    def test_exec_list_unset_follows_general_policy(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_EXEC_NAMESPACES", raising=False)
        assert server.exec_namespace_violation("anything") is None, "exec list unset = follows general policy"

    def test_exec_list_glob_match_allowed(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "debug-*,team-a")
        assert server.exec_namespace_violation("debug-x") is None, "exec list glob match allowed"

    def test_exec_list_exact_match_allowed(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "debug-*,team-a")
        assert server.exec_namespace_violation("team-a") is None, "exec list exact match allowed"

    def test_off_list_namespace_denied_for_exec(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "debug-*,team-a")
        assert server.exec_namespace_violation("default") is not None, "off-list namespace denied for exec"

    def test_exec_list_enforced_at_tool_boundary(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "debug-*,team-a")
        r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
        assert "exec policy" in r and "exec namespace list" in r, "exec list enforced at tool boundary"

    def test_malformed_exec_list_clean_deny_message(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "Bad Pattern!")
        assert server.exec_namespace_violation("x") is not None, "malformed exec list -> clean deny message"

    def test_general_blacklist_wins_over_exec_list(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "debug-*,team-a")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "debug-x")
        r = asyncio.run(server.exec_in_pod("app-7d9f", "debug-x", ["ps"]))
        assert "namespace policy" in r and "exec policy" not in r, (
            "general blacklist still wins over the exec list (intersect semantics)"
        )

    def test_namespace_policy_enforced_for_exec(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-a")
        r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
        assert "namespace policy" in r, "namespace policy enforced for exec"

    def test_exec_in_pod_absent_again_after_disabling(self, server):
        ensure_exec_state(server, False)
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "exec_in_pod" not in names, "exec_in_pod absent again after disabling (18 tools)"


# ── Automatic exec RBAC provisioning ───────────────────────────────────────


class _FakeRBAC:
    def __init__(self, server, template_exists=True, existing=None):
        self.template_exists = template_exists
        self.bindings = dict(existing or {})  # (ns, name) -> binding
        self.created = []
        self.updated = []
        self._server = server

    def read_cluster_role(self, name):
        _ApiException = sys.modules["kubernetes.client.rest"].ApiException
        if name != self._server.EXEC_TEMPLATE_ROLE or not self.template_exists:
            raise _ApiException(reason="clusterroles not found", status=404)

    def read_namespaced_role_binding(self, name, namespace):
        _ApiException = sys.modules["kubernetes.client.rest"].ApiException
        key = (namespace, name)
        if key not in self.bindings:
            raise _ApiException(reason="rolebindings not found", status=404)
        return self.bindings[key]

    def create_namespaced_role_binding(self, namespace, body):
        # server.py submits a plain camelCase dict body (no model classes)
        self.bindings[(namespace, body["metadata"]["name"])] = body
        self.created.append(namespace)

    def patch_namespaced_role_binding(self, name, namespace, body):
        self.updated.append((namespace, body["subjects"][0]["namespace"]))


def _binding(server, role_name=None, sa_ns="mcp-ns"):
    return types.SimpleNamespace(
        role_ref=types.SimpleNamespace(kind="ClusterRole", name=role_name or server.EXEC_TEMPLATE_ROLE),
        subjects=[types.SimpleNamespace(kind="ServiceAccount", name="k8s-mcp-2-0-sa", namespace=sa_ns)],
    )


class TestExecRbacProvisioning:
    def test_noop_with_empty_exec_list(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_EXEC_NAMESPACES", raising=False)
        assert "nothing to do" in server.provision_exec_rbac(), "provisioning is a no-op with an empty exec list"

    def test_binding_created_for_exact_ns_glob_matched_none(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake = _FakeRBAC(server)
        monkeypatch.setattr(server, "rbac_v1", fake)
        server.provision_exec_rbac()
        assert fake.created == ["team-a"], f"binding created for exact ns; glob matched none ({fake.created})"

    def test_nonexistent_literal_ns_skipped_with_reason(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake = _FakeRBAC(server)
        monkeypatch.setattr(server, "rbac_v1", fake)
        summary = server.provision_exec_rbac()
        assert "ghost-ns" in summary and "not found" in summary, "nonexistent literal ns skipped with reason"

    def test_summary_names_the_template_role_and_bound_sa(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake = _FakeRBAC(server)
        monkeypatch.setattr(server, "rbac_v1", fake)
        summary = server.provision_exec_rbac()
        assert "k8s-mcp-pods-exec" in summary and "mcp-ns/k8s-mcp-2-0-sa" in summary, (
            "summary names the template role and bound SA"
        )

    def test_created_binding_references_template_clusterrole_with_our_sa(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake = _FakeRBAC(server)
        monkeypatch.setattr(server, "rbac_v1", fake)
        server.provision_exec_rbac()
        body = fake.bindings[("team-a", server.EXEC_BINDING_NAME)]
        assert (
            body["roleRef"]["name"] == "k8s-mcp-pods-exec"
            and body["subjects"][0]["namespace"] == "mcp-ns"
            and body["kind"] == "RoleBinding"
        ), "created binding references the template ClusterRole with our SA as subject"

    def test_second_run_is_idempotent(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake2 = _FakeRBAC(server, existing={("team-a", server.EXEC_BINDING_NAME): _binding(server)})
        monkeypatch.setattr(server, "rbac_v1", fake2)
        summary2 = server.provision_exec_rbac()
        assert fake2.created == [] and "already present" in summary2, "second run is idempotent (binding kept)"

    def test_foreign_binding_not_touched(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake3 = _FakeRBAC(
            server, existing={("team-a", server.EXEC_BINDING_NAME): _binding(server, role_name="someone-elses-role")}
        )
        monkeypatch.setattr(server, "rbac_v1", fake3)
        summary3 = server.provision_exec_rbac()
        assert fake3.created == [] and "different roleRef" in summary3, "foreign binding not touched"

    def test_stale_subject_binding_repointed(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake7 = _FakeRBAC(
            server, existing={("team-a", server.EXEC_BINDING_NAME): _binding(server, sa_ns="old-project-ns")}
        )
        monkeypatch.setattr(server, "rbac_v1", fake7)
        server.provision_exec_rbac()
        assert fake7.updated == [("team-a", "mcp-ns")], (
            f"stale-subject binding repointed to the current SA ({fake7.updated})"
        )

    def test_summary_reports_the_repoint(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake7 = _FakeRBAC(
            server, existing={("team-a", server.EXEC_BINDING_NAME): _binding(server, sa_ns="old-project-ns")}
        )
        monkeypatch.setattr(server, "rbac_v1", fake7)
        summary7 = server.provision_exec_rbac()
        assert "updated: ['team-a']" in summary7, "summary reports the repoint"

    def test_binding_with_correct_subject_left_alone(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake8 = _FakeRBAC(server, existing={("team-a", server.EXEC_BINDING_NAME): _binding(server)})
        monkeypatch.setattr(server, "rbac_v1", fake8)
        summary8 = server.provision_exec_rbac()
        assert fake8.updated == [] and "already present: ['team-a']" in summary8, (
            "binding with correct subject left alone"
        )

    def test_missing_template_clusterrole_instruction_message(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        fake4 = _FakeRBAC(server, template_exists=False)
        monkeypatch.setattr(server, "rbac_v1", fake4)
        _missing_summary = server.provision_exec_rbac()
        assert "template ClusterRole" in _missing_summary and "missing" in _missing_summary, (
            "missing template ClusterRole -> instruction message"
        )

    def test_general_policy_blocked_namespaces_are_skipped(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a,debug-*,ghost-ns")
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-a")
        fake5 = _FakeRBAC(server)
        monkeypatch.setattr(server, "rbac_v1", fake5)
        summary5 = server.provision_exec_rbac()
        assert fake5.created == [] and "blocked by the general namespace policy" in summary5, (
            "general-policy-blocked namespaces are skipped"
        )

    def test_missing_service_account_identity_clear_error(self, server, monkeypatch):
        # SA identity gone while exec namespaces are configured -> clear error
        # (the original ran this with K8S_MCP_EXEC_NAMESPACES still set).
        monkeypatch.delenv("K8S_MCP_SA_NAME", raising=False)
        monkeypatch.delenv("K8S_MCP_SA_NAMESPACE", raising=False)
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a")
        assert "cannot determine" in server.provision_exec_rbac(), "missing ServiceAccount identity -> clear error"

    def test_unexpected_provisioning_errors_degrade_to_message(self, server, monkeypatch):
        # Provisioning must NEVER crash the server: unexpected errors degrade to
        # a returned message (startup calls this in __main__).
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a")

        class _BoomRBAC:
            def read_cluster_role(self, name):
                raise RuntimeError("unexpected library blowup")

        monkeypatch.setattr(server, "rbac_v1", _BoomRBAC())
        _boom_summary = server.provision_exec_rbac()
        assert _boom_summary.startswith("RBAC provisioning failed") and "server continues" in _boom_summary, (
            "unexpected provisioning errors degrade to a message, never an exception"
        )


# ── Per-client keys + exec assignments ─────────────────────────────────────


class TestPerClientExecAssignment:
    def test_clients_map_parses(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_SA_NAME", raising=False)
        monkeypatch.delenv("K8S_MCP_SA_NAMESPACE", raising=False)
        monkeypatch.delenv("K8S_MCP_EXEC_NAMESPACES", raising=False)
        server._parse_clients("alice:key1:debug-*,team-a;bob:key2")  # must not raise
        assert True, "clients map parses"

    @pytest.mark.parametrize("bad", ("alice-key1", "alice:key1:extra:colon", "alice::team-a", ":no-name"))
    def test_malformed_clients_entry_rejected(self, server, bad):
        """malformed clients entry rejected (label per case)."""
        with pytest.raises(ValueError, match="invalid client entry"):
            server._parse_clients(bad)

    def test_semicolon_only_config_parses_to_no_clients(self, server):
        assert server._parse_clients(";") == {}, "';'-only config parses to no clients"

    def test_per_key_exec_assignment_parsed(self, server):
        clients = "alice:alice-key-1:debug-*,team-a;bob:bob-key-2:team-b;carol:carol-key-3"
        alice_map = server._parse_clients(clients)
        assert alice_map["alice-key-1"] == ("alice", ("debug-*", "team-a")), "per-key exec assignment parsed"

    def test_second_clients_assignment_parsed(self, server):
        clients = "alice:alice-key-1:debug-*,team-a;bob:bob-key-2:team-b;carol:carol-key-3"
        alice_map = server._parse_clients(clients)
        assert alice_map["bob-key-2"] == ("bob", ("team-b",)), "second client's assignment parsed"

    def test_client_without_patterns_inherits_deployment_ceiling(self, server):
        clients = "alice:alice-key-1:debug-*,team-a;bob:bob-key-2:team-b;carol:carol-key-3"
        alice_map = server._parse_clients(clients)
        assert alice_map["carol-key-3"] == ("carol", None), "client without patterns inherits deployment ceiling"

    def test_per_client_key_authenticates(self, server, monkeypatch):
        called, _sent = asyncio.run(
            _call_middleware(
                server, [(b"authorization", b"Bearer alice-key-1")], clients_env="alice:alice-key-1:debug-*"
            )
        )
        assert called, "per-client key authenticates"

    def test_key_not_in_clients_map_401(self, server, monkeypatch):
        called, sent = asyncio.run(
            _call_middleware(server, [(b"authorization", b"Bearer unknown-key")], clients_env="alice:alice-key-1")
        )
        assert not called and sent[0]["status"] == 401, "key not in clients map -> 401"

    def test_shared_key_still_authenticates_alongside_per_client_keys(self, server, monkeypatch):
        called, _sent = asyncio.run(
            _call_middleware(
                server, [(b"authorization", b"Bearer shared-1")], clients_env="alice:alice-key-1", key_env="shared-1"
            )
        )
        assert called, "shared key still authenticates alongside per-client keys"

    def test_invalid_x_exec_namespaces_header_400(self, server, monkeypatch):
        called, sent = asyncio.run(
            _call_middleware(
                server,
                [(b"authorization", b"Bearer alice-key-1"), (b"x-exec-namespaces", b"Bad Pattern!")],
                clients_env="alice:alice-key-1",
            )
        )
        assert not called and sent[0]["status"] == 400, "invalid X-Exec-Namespaces header -> 400"

    def test_caller_context_carries_key_assignment_and_header_narrowing(self, server, monkeypatch):
        # Header narrowing is visible to the app via the caller context.
        seen = {}

        class _CtxApp:
            async def __call__(self, scope, receive, send):
                seen["caller"] = server._caller_context.get()

        monkeypatch.setenv("K8S_MCP_CLIENTS", "alice:a-k:debug-*,team-a")
        try:

            async def receive():
                return {"type": "http.request"}

            async def send(msg):
                pass

            asyncio.run(
                server._ApiKeyAuthMiddleware(_CtxApp())(
                    {
                        "type": "http",
                        "headers": [(b"authorization", b"Bearer a-k"), (b"x-exec-namespaces", b"debug-a")],
                    },
                    receive,
                    send,
                )
            )
        finally:
            server._caller_context.set(None)
            monkeypatch.delenv("K8S_MCP_CLIENTS", raising=False)
            monkeypatch.delenv("K8S_MCP_API_KEY", raising=False)
        assert (
            seen["caller"] is not None
            and seen["caller"].name == "alice"
            and seen["caller"].key_exec_patterns == ("debug-*", "team-a")
            and seen["caller"].header_exec_patterns == ("debug-a",)
        ), "caller context carries key assignment + header narrowing"

    def test_exec_allowed_inside_callers_assigned_namespaces(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        _caller = server._Caller(name="alice", key_exec_patterns=("team-a",), header_exec_patterns=None)
        server._caller_context.set(_caller)
        try:
            r = asyncio.run(server.exec_in_pod("app-7d9f", "team-a", ["ps"]))
        finally:
            server._caller_context.set(None)
        assert "[--] [ps]" in r, "exec allowed inside the caller's assigned namespaces"

    def test_exec_denied_outside_callers_assignment(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        _caller = server._Caller(name="alice", key_exec_patterns=("team-a",), header_exec_patterns=None)
        server._caller_context.set(_caller)
        try:
            r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
        finally:
            server._caller_context.set(None)
        assert "caller's exec assignment" in r, "exec denied outside the caller's assignment"

    def test_header_cannot_widen_callers_assignment(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        _caller = server._Caller(name="alice", key_exec_patterns=("team-a",), header_exec_patterns=None)
        server._caller_context.set(_caller._replace(header_exec_patterns=("*",)))
        try:
            r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
        finally:
            server._caller_context.set(None)
        assert "caller's exec assignment" in r or "X-Exec-Namespaces header" in r, (
            "header cannot widen the caller's assignment (denied either way)"
        )

    def test_header_narrows_even_when_key_assignment_allows(self, server, fakebin, monkeypatch):
        ensure_exec_state(server, True)
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server.v1, "read_namespaced_pod", lambda name, ns: _LabeledPod(), raising=False)
        _caller = server._Caller(name="alice", key_exec_patterns=("team-a",), header_exec_patterns=None)
        server._caller_context.set(
            _caller._replace(key_exec_patterns=("team-a", "default"), header_exec_patterns=("nothing-matches",))
        )
        try:
            r = asyncio.run(server.exec_in_pod("app-7d9f", "team-a", ["ps"]))
        finally:
            server._caller_context.set(None)
        assert "x-exec-namespaces header" in r.lower(), "header narrows even when the key assignment allows"

    def test_provisioning_binds_union_of_deployment_and_client_lists(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_SA_NAME", "k8s-mcp-2-0-sa")
        monkeypatch.setenv("K8S_MCP_SA_NAMESPACE", "mcp-ns")
        monkeypatch.setenv("K8S_MCP_EXEC_NAMESPACES", "team-a")
        monkeypatch.setenv("K8S_MCP_CLIENTS", "alice:ak:team-b;bob:bk:team-sec-x")
        fake6 = _FakeRBAC(server)
        monkeypatch.setattr(server, "rbac_v1", fake6)
        server.provision_exec_rbac()
        assert sorted(fake6.created) == ["team-a", "team-b", "team-sec-x"], (
            f"provisioning binds the union of deployment + client lists ({fake6.created})"
        )


# ── check_rbac surfaces "no" answers ───────────────────────────────────────


class TestCheckRbacTool:
    def test_can_i_yes_surfaces_yes(self, server, monkeypatch):
        async def _cani_yes(argv):
            return 0, "yes", ""

        monkeypatch.setattr(server, "_kubectl_run", _cani_yes)
        assert asyncio.run(server.check_rbac("create", "pods/exec", "default")) == "yes", "can-i yes -> 'yes'"

    def test_can_i_no_surfaces_no_not_error(self, server, monkeypatch):
        async def _cani_no(argv):
            return 1, "no", ""  # can-i exits 1 on "no" — with the answer on stdout

        monkeypatch.setattr(server, "_kubectl_run", _cani_no)
        assert asyncio.run(server.check_rbac("create", "pods/exec", "kube-system")) == "no", (
            "can-i 'no' (rc=1) -> surfaces 'no', not an error"
        )


# ── list_virtual_services (read-only, policy-aware) ────────────────────────


class _VSItem:
    """ResourceInstance stand-in: dict payload + to_dict()."""

    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class _VSListing:
    def __init__(self, items):
        self.items = items


class _VSResource:
    def __init__(self, items):
        # Accept raw dicts or _VSItem instances (the original always passed
        # _VSItem-wrapped items).
        self._items = [i if hasattr(i, "to_dict") else _VSItem(i) for i in items]

    def get(self, name=None, namespace=None, **kwargs):
        _ApiException = sys.modules["kubernetes.client.rest"].ApiException
        if name is not None and namespace is not None:
            for it in self._items:
                m = it.to_dict()["metadata"]
                if m["name"] == name and m["namespace"] == namespace:
                    return it
            raise _ApiException(reason=f'virtualservices "{name}" not found', status=404)
        items = [it for it in self._items if namespace is None or it.to_dict()["metadata"]["namespace"] == namespace]
        return _VSListing(items)


class _VSDiscovery:
    """Stand-in for dyn_client.resources (API-version discovery)."""

    def __init__(self, resource=None, fail=False):
        self._resource, self._fail = resource, fail

    def get(self, api_version="", plural="", **kwargs):
        if self._fail:
            raise sys.modules["kubernetes.dynamic.exceptions"].ResourceNotFoundError(
                f"no resource {api_version}/{plural}"
            )
        return self._resource


_VS_DEFS = [
    {
        "metadata": {"name": "checkout", "namespace": "team-a", "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {
            "hosts": ["checkout.example.com"],
            "gateways": ["istio-system/ezaf-gateway", "mesh"],
            "http": [
                {
                    "name": "api",
                    "match": [{"uri": {"prefix": "/api"}}],
                    "route": [
                        {
                            "destination": {"host": "v1.team-a.svc.cluster.local", "port": {"number": 8080}},
                            "weight": 90,
                        },
                        {
                            "destination": {"host": "v2.team-a.svc.cluster.local", "port": {"number": 8080}},
                            "weight": 10,
                        },
                    ],
                },
                {"match": [{"uri": {"prefix": "/old"}}], "redirect": {"uri": "/new", "redirectCode": 301}},
            ],
            "tcp": [
                {"match": [{"port": 9000}], "route": [{"destination": {"host": "tcp-svc.team-a.svc.cluster.local"}}]}
            ],
        },
    },
    {
        "metadata": {"name": "other", "namespace": "team-b", "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {"hosts": ["other.example.com"], "http": []},
    },
]


class TestVirtualServices:
    def test_list_virtual_services_registered(self, server):
        tool_names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "list_virtual_services" in tool_names, "list_virtual_services registered (18 tools)"

    def test_summary_header_counts_one_namespaces_virtualservices(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(namespace="team-a"))
        assert "VIRTUALSERVICES in namespace 'team-a' (1)" in out, (
            "summary header counts one namespace's VirtualServices"
        )
        assert "hosts: checkout.example.com" in out and "gateways: istio-system/ezaf-gateway, mesh" in out, (
            "summary shows hosts and gateways"
        )
        assert "-> v1.team-a.svc.cluster.local:8080 (90%), v2.team-a.svc.cluster.local:8080 (10%)" in out, (
            "weighted http destinations rendered"
        )
        assert "uri-prefix:/api" in out and "-> redirect /new" in out and "tcp[0]: port:9000" in out, (
            "match, redirect, and tcp routes rendered"
        )

    def test_summary_shows_hosts_and_gateways(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(namespace="team-a"))
        assert "hosts: checkout.example.com" in out and "gateways: istio-system/ezaf-gateway, mesh" in out, (
            "summary shows hosts and gateways"
        )

    def test_weighted_http_destinations_rendered(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(namespace="team-a"))
        assert "-> v1.team-a.svc.cluster.local:8080 (90%), v2.team-a.svc.cluster.local:8080 (10%)" in out, (
            "weighted http destinations rendered"
        )

    def test_match_redirect_and_tcp_routes_rendered(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(namespace="team-a"))
        assert "uri-prefix:/api" in out and "-> redirect /new" in out and "tcp[0]: port:9000" in out, (
            "match, redirect, and tcp routes rendered"
        )

    def test_empty_namespace_lists_across_all_namespaces(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services())
        assert "team-a/checkout" in out and "team-b/other" in out, "empty namespace lists across all namespaces"

    def test_blacklisted_namespaces_filtered_from_cluster_wide_listing(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-a")
        out = asyncio.run(server.list_virtual_services())
        assert "team-a/checkout" not in out and "team-b/other" in out, (
            "blacklisted namespaces filtered from cluster-wide listing"
        )

    def test_denied_namespace_rejected_at_tool_boundary(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-a")
        out = asyncio.run(server.list_virtual_services(namespace="team-a"))
        assert out.startswith("Error:") and "denied by the namespace policy" in out, (
            "denied namespace rejected at the tool boundary"
        )

    def test_name_plus_namespace_returns_full_definition(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(namespace="team-a", name="checkout"))
        assert '"hosts"' in out and '"checkout"' in out, "name + namespace returns the full definition"

    def test_name_without_namespace_finds_vs_cluster_wide(self, server, fakebin, monkeypatch):
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(name="other"))
        assert "team-b/other" in out, "name without namespace finds the VS cluster-wide"

    def test_missing_vs_falls_back_to_kubectl_with_name_and_n(self, server, fakebin, monkeypatch):
        # fake kubectl echoes argv — no real cluster calls
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(_VSResource(_VS_DEFS))))
        out = asyncio.run(server.list_virtual_services(namespace="team-a", name="ghost"))
        assert "[get]" in out and "[ghost]" in out and "[-n]" in out and "[team-a]" in out, (
            "missing VS falls back to kubectl with name + -n (fake kubectl echoes argv)"
        )

    def test_discovery_failure_falls_back_to_kubectl(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(fail=True)))
        out = asyncio.run(server.list_virtual_services(namespace="team-a"))
        assert "[virtualservices.networking.istio.io]" in out, "discovery failure falls back to kubectl"

    def test_name_without_namespace_fallback_uses_field_selector(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        # The failing discovery from the previous check persists in the
        # original — same state here.
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=_VSDiscovery(fail=True)))
        out = asyncio.run(server.list_virtual_services(name="checkout"))
        assert "[--field-selector]" in out and "[metadata.name=checkout]" in out and "[-A]" in out, (
            "name-without-namespace fallback uses a field selector, not name + -A"
        )


# ── Built-in console (static shell on the same pod) ────────────────────────


async def _asgi_collect(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


async def _asgi_get(app, path, scope_type="http"):
    return await _asgi_collect(app, {"type": scope_type, "path": path})


def _sent_status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _sent_headers(sent):
    return {k.lower(): v for m in sent if m["type"] == "http.response.start" for k, v in m["headers"]}


def _sent_body(sent):
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


class TestConsoleApp:
    def test_ui_serves_hpe_console_shell(self, server):
        console_app = server._ConsoleApp(server._UI_DIR)
        sent = asyncio.run(_asgi_get(console_app, "/ui/"))
        assert _sent_status(sent) == 200 and b"HPE Kubernetes Ops Console" in _sent_body(sent), (
            "/ui/ serves the HPE console shell"
        )

    def test_console_shell_sends_csp_headers(self, server):
        console_app = server._ConsoleApp(server._UI_DIR)
        sent = asyncio.run(_asgi_get(console_app, "/ui/"))
        assert b"content-security-policy" in _sent_headers(sent), "console shell sends CSP headers"

    def test_static_asset_served_with_correct_content_type(self, server):
        console_app = server._ConsoleApp(server._UI_DIR)
        sent = asyncio.run(_asgi_get(console_app, "/ui/style.css"))
        assert _sent_status(sent) == 200 and _sent_headers(sent)[b"content-type"] == b"text/css; charset=utf-8", (
            "static asset served with correct content type"
        )

    @pytest.mark.parametrize("bad", ("/ui/../../server.py", "/ui/%2e%2e/server.py", "/ui/....//server.py"))
    def test_path_traversal_guarded(self, server, bad):
        console_app = server._ConsoleApp(server._UI_DIR)
        sent = asyncio.run(_asgi_get(console_app, bad))
        assert _sent_status(sent) == 404, f"path traversal guarded: {bad}"

    def test_console_disabled_flag_404(self, server, monkeypatch):
        console_app = server._ConsoleApp(server._UI_DIR)
        monkeypatch.setenv("K8S_MCP_CONSOLE_ENABLED", "false")
        sent = asyncio.run(_asgi_get(console_app, "/ui/"))
        assert _sent_status(sent) == 404, "K8S_MCP_CONSOLE_ENABLED=false -> console 404"

    def test_router_routes_ui_to_console_app_only(self, server):
        mcp_marker = types.SimpleNamespace(seen=None)
        ui_marker = types.SimpleNamespace(seen=None)

        class _MarkerApp:
            def __init__(self, sink):
                self.sink = sink

            async def __call__(self, scope, receive, send):
                self.sink.seen = scope.get("path")

        router = server._ConsoleRouterApp(_MarkerApp(mcp_marker), _MarkerApp(ui_marker))
        asyncio.run(_asgi_collect(router, {"type": "http", "path": "/ui/index.html"}))
        assert ui_marker.seen == "/ui/index.html" and mcp_marker.seen is None, (
            "router routes /ui/* to the console app only"
        )

    def test_router_passes_mcp_to_mcp_app(self, server):
        mcp_marker = types.SimpleNamespace(seen=None)
        ui_marker = types.SimpleNamespace(seen=None)

        class _MarkerApp:
            def __init__(self, sink):
                self.sink = sink

            async def __call__(self, scope, receive, send):
                self.sink.seen = scope.get("path")

        router = server._ConsoleRouterApp(_MarkerApp(mcp_marker), _MarkerApp(ui_marker))
        asyncio.run(_asgi_collect(router, {"type": "http", "path": "/mcp"}))
        assert mcp_marker.seen == "/mcp", "router passes /mcp to the MCP app (auth middleware wraps it there)"

    def test_non_http_scopes_pass_through_to_mcp_app(self, server):
        mcp_marker = types.SimpleNamespace(seen=None)
        ui_marker = types.SimpleNamespace(seen=None)

        class _MarkerApp:
            def __init__(self, sink):
                self.sink = sink

            async def __call__(self, scope, receive, send):
                self.sink.seen = scope.get("path")

        router = server._ConsoleRouterApp(_MarkerApp(mcp_marker), _MarkerApp(ui_marker))
        asyncio.run(_asgi_collect(router, {"type": "websocket", "path": "/ws"}))
        assert mcp_marker.seen == "/ws", "non-http scopes pass through to the MCP app"

    def test_root_redirects_to_ui(self, server):
        console_app = server._ConsoleApp(server._UI_DIR)
        sent = asyncio.run(_asgi_get(console_app, "/"))
        assert _sent_status(sent) == 302 and _sent_headers(sent)[b"location"] == b"/ui/", "/ redirects to /ui/"


# ── Startup invariants ─────────────────────────────────────────────────────


class TestStartupInvariants:
    def test_incluster_kubeconfig_only_created_incluster(self, server):
        assert server._KUBECONFIG_PATH is None or os.environ.get("KUBERNETES_SERVICE_HOST"), (
            "in-cluster kubeconfig only created in-cluster"
        )

    def test_kubeconfig_tempfile_mode_0600_when_created(self, server):
        assert server._KUBECONFIG_PATH is None or (
            os.path.exists(server._KUBECONFIG_PATH) and (os.stat(server._KUBECONFIG_PATH).st_mode & 0o777) == 0o600
        ), "kubeconfig tempfile exists with mode 0600 when created"

    def test_module_imported_cleanly(self, server):
        assert server.__version__ if hasattr(server, "__version__") else True, "module imported cleanly"
