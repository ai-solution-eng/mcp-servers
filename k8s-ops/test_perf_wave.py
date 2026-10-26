"""Performance-wave behaviors (wave-6 remediation):

1. get_resource un-named listing fast path — in-process dynamic client,
   compact render, capped, policy-filtered; kubectl stays the contract for
   named gets and every non-yaml output mode; any dynamic-path exception
   falls back to kubectl.
2. _expand_allowed 30s TTL cache — one live lookup per policy window,
   error bypasses the cache and behaves as before, exact whitelists skip
   the lookup entirely (pre-existing test covers that; here we prove the
   caching itself).
3. New env knobs: K8S_MCP_MAX_EVENTS (get_events + triage warnings),
   K8S_MCP_MAX_NS_REWRITE (cluster-wide rewrite limit).
4. get_pod_logs namespace is REQUIRED — the old silent "default" fallback
   is a refusal now.
5. get_resource fast path respects the namespace policy on both sides:
   blacklist-filtered, whitelist-filtered, and it never fires for a
   namespaced or named call.
"""

import asyncio
import os
import types

import pytest

from test_namespace_policy import _pop_policy_envs


def _dyn_item(name, ns=None, creation=None):
    """Dynamic-client shaped listing item (has .to_dict)."""
    return types.SimpleNamespace(
        to_dict=lambda name=name, ns=ns, creation=creation: {
            "metadata": {"name": name, **({"namespace": ns} if ns else {}), **({"creationTimestamp": creation} if creation else {})},
            "spec": {},
        }
    )


class _FakeDynResource:
    """resource.get() returns a listing whose items carry .to_dict()."""

    name = "pods"

    def __init__(self, items, exc=None):
        self._items = items
        self._exc = exc

    def get(self, **kw):
        if self._exc is not None:
            raise self._exc
        return types.SimpleNamespace(items=self._items)


class _FakeDiscovery:
    """dyn_client.resources.get(name=...) → the fake resource (or raises)."""

    def __init__(self, resource, discovery_exc=None):
        self._resource = resource
        self._discovery_exc = discovery_exc
        self.calls = 0

    def get(self, name="", **kw):
        self.calls += 1
        if self._discovery_exc is not None:
            raise self._discovery_exc
        return self._resource


class _CountingListNamespace:
    """Fake v1.list_namespace: counts calls, returns the stub list shape."""

    def __init__(self, names):
        self.calls = 0
        self._names = names

    def __call__(self):
        self.calls += 1
        items = []
        for n in self._names:
            items.append(
                types.SimpleNamespace(
                    metadata=types.SimpleNamespace(name=n, creation_timestamp=None),
                    status=types.SimpleNamespace(phase="Active"),
                )
            )
        return types.SimpleNamespace(items=items)


@pytest.fixture(autouse=True)
def _clean(server, monkeypatch):
    _pop_policy_envs()
    monkeypatch.delenv("K8S_MCP_MAX_EVENTS", raising=False)
    monkeypatch.delenv("K8S_MCP_MAX_NS_REWRITE", raising=False)
    monkeypatch.delenv("K8S_MCP_MAX_LIST_ITEMS", raising=False)
    server._fastpath_resources.clear()
    server._expansion_cache.clear()
    yield
    server._fastpath_resources.clear()
    server._expansion_cache.clear()


# ══════════════════════════════════════════════════════════════════════════
# 1. get_resource fast path
# ══════════════════════════════════════════════════════════════════════════


class TestGetResourceFastPath:
    def test_unnamed_listing_uses_compact_render(self, server, monkeypatch):
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(
                resources=_FakeDiscovery(
                    _FakeDynResource(
                        [
                            _dyn_item("p1", "team-a", "2026-10-05T00:00:00Z"),
                            _dyn_item("p2", "team-b"),
                        ]
                    )
                )
            ),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert out.startswith("PODS (2):"), "compact header with count"
        assert "  team-a/p1 (Age:" in out and "  team-b/p2 (Age: Unknown)" in out, "one ns/name line per item"
        assert "kind:" not in out, "no full YAML documents"

    def test_named_get_still_kubectl(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        marker = types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([])))
        monkeypatch.setattr(server, "dyn_client", marker)
        out = asyncio.run(server.get_resource("pods", name="my-pod", namespace="team-a"))
        assert "[get]" in out and "[my-pod]" in out and "[-o]" in out and "[yaml]" in out, (
            "named get goes through kubectl argv unchanged"
        )
        assert marker.resources.calls == 0, "discovery never consulted for a named get"

    def test_jsonpath_output_mode_still_kubectl(self, server, fakebin, monkeypatch):
        # The console container-picker parses jsonpath output — contract stays.
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        marker = types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([])))
        monkeypatch.setattr(server, "dyn_client", marker)
        out = asyncio.run(
            server.get_resource("pods", name="my-pod", namespace="team-a", output="jsonpath={.spec.containers[*].name}")
        )
        assert "[jsonpath={.spec.containers[*].name}]" in out, "jsonpath rides kubectl unchanged"
        assert marker.resources.calls == 0

    def test_namespaced_listing_still_kubectl(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        marker = types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([])))
        monkeypatch.setattr(server, "dyn_client", marker)
        out = asyncio.run(server.get_resource("pods", namespace="team-a"))
        assert "[team-a]" in out and "[-A]" not in out, "namespaced listing keeps kubectl path"
        assert marker.resources.calls == 0, "fast path fires for cluster-wide un-named listings only"

    def test_discovery_failure_falls_back_to_kubectl(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(resources=_FakeDiscovery(None, discovery_exc=server.ResourceNotFoundError("nope"))),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert "[get]" in out and "[pods]" in out and "[--all-namespaces]" in out, (
            "unresolvable token falls back to kubectl argv (old behavior)"
        )

    def test_listing_failure_falls_back_to_kubectl(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([], exc=RuntimeError("api blip")))),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert "[get]" in out and "[--all-namespaces]" in out, "any listing exception falls back to kubectl"

    def test_unknown_token_falls_back_and_is_cached_negative(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        discovery = _FakeDiscovery(None, discovery_exc=server.ResourceNotFoundError("nope"))
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=discovery))
        asyncio.run(server.get_resource("widgets"))
        asyncio.run(server.get_resource("widgets"))
        assert discovery.calls == 1, "negative resolution cached (one discovery attempt, not one per call)"

    def test_resource_resolution_cached(self, server, monkeypatch):
        discovery = _FakeDiscovery(_FakeDynResource([_dyn_item("p1", "team-a")]))
        monkeypatch.setattr(server, "dyn_client", types.SimpleNamespace(resources=discovery))
        asyncio.run(server.get_resource("pods"))
        asyncio.run(server.get_resource("pods"))
        assert discovery.calls == 1, "resolved Resource cached across calls"

    def test_fastpath_cap_and_marker(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_LIST_ITEMS", "2")
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(
                resources=_FakeDiscovery(_FakeDynResource([_dyn_item(f"p{i}", "team-a") for i in range(1, 6)]))
            ),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert "PODS (2):" in out, "header counts capped items"
        assert "p2" in out and "p3" not in out, "first cap items kept"
        assert "K8S_MCP_MAX_LIST_ITEMS" in out and "3 more items" in out, "same marker convention"

    def test_fastpath_blacklist_filtered(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-a")
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(
                resources=_FakeDiscovery([_FakeDynResource([_dyn_item("p1", "team-a"), _dyn_item("p2", "team-b")])][0])
            ),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert "team-a/p1" not in out and "team-b/p2" in out, "blacklisted namespaces filtered (same as list_pods)"

    def test_fastpath_whitelist_filtered(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-b")
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(
                resources=_FakeDiscovery(_FakeDynResource([_dyn_item("p1", "team-a"), _dyn_item("p2", "team-b")]))
            ),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert "team-a/p1" not in out and "team-b/p2" in out, "whitelist filters the compact listing (policy FILTER, not -A refusal)"

    def test_blacklist_only_policy_still_answers_on_fastpath(self, server, monkeypatch):
        # kubectl -A refuses under blacklist-only policy; the filtered
        # in-process listing can answer safely (same posture as list_pods).
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-system")
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([_dyn_item("p1", "kube-system"), _dyn_item("p2", "team-b")]))),
        )
        out = asyncio.run(server.get_resource("pods"))
        assert "kube-system/p1" not in out and "team-b/p2" in out, "blacklist-only policy answers with filtered data"

    def test_alias_token_uses_fastpath(self, server, monkeypatch):
        # 'deploy' maps to 'deployments', whose discovery Resource carries
        # name='deployments' — the stub serves whatever plural was resolved.
        monkeypatch.setattr(
            server,
            "dyn_client",
            types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([_dyn_item("d1", "team-a")]))),
        )
        out = asyncio.run(server.get_resource("deploy"))
        assert out.startswith("PODS (1):") and "team-a/d1" in out, (
            "'deploy' alias resolves through discovery (stub serves the resolved plural's listing)"
        )

    def test_dotted_or_slashed_token_falls_back(self, server, fakebin, monkeypatch):
        monkeypatch.setenv("PATH", fakebin + ":" + os.environ["PATH"])
        marker = types.SimpleNamespace(resources=_FakeDiscovery(_FakeDynResource([])))
        monkeypatch.setattr(server, "dyn_client", marker)
        out = asyncio.run(server.get_resource("deployments.apps"))
        assert "[deployments.apps]" in out, "group-qualified token rides kubectl"
        out = asyncio.run(server.get_resource("serving.kserve.io/v1beta1"))
        assert "[serving.kserve.io/v1beta1]" in out, "slashed token rides kubectl"
        assert marker.resources.calls == 0


# ══════════════════════════════════════════════════════════════════════════
# 2. _expand_allowed TTL cache
# ══════════════════════════════════════════════════════════════════════════


class TestExpandAllowedCache:
    def test_glob_expansion_cached_within_ttl(self, server, monkeypatch):
        counter = _CountingListNamespace(["team-a", "team-b", "kube-system"])
        monkeypatch.setattr(server.v1, "list_namespace", counter, raising=False)
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*")
        out1 = asyncio.run(server._expand_allowed(("team-*",)))
        out2 = asyncio.run(server._expand_allowed(("team-*",)))
        assert out1 == ["team-a", "team-b"] and out2 == ["team-a", "team-b"], "expansion result unchanged"
        assert counter.calls == 1, "one live lookup per TTL window"

    def test_policy_change_invalidates_cache(self, server, monkeypatch):
        counter = _CountingListNamespace(["team-a", "team-b", "kube-system"])
        monkeypatch.setattr(server.v1, "list_namespace", counter, raising=False)
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*")
        asyncio.run(server._expand_allowed(("team-*",)))
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "kube-*")
        out = asyncio.run(server._expand_allowed(("kube-*",)))
        assert out == ["kube-system"], "a different policy pair recomputes"
        assert counter.calls == 2, "different key = fresh lookup"

    def test_list_error_bypasses_cache_and_fails_as_before(self, server, monkeypatch):
        _ApiException = __import__("sys").modules["kubernetes.client.rest"].ApiException

        class _Failing:
            def __init__(self):
                self.calls = 0

            def __call__(self):
                self.calls += 1
                raise _ApiException(reason="boom", status=500)

        failing = _Failing()
        monkeypatch.setattr(server.v1, "list_namespace", failing, raising=False)
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-*")
        for _ in range(2):
            with pytest.raises(server.NamespacePolicyError, match="could not expand namespace glob patterns"):
                asyncio.run(server._expand_allowed(("team-*",)))
        assert failing.calls == 2, "error path never cached — every call re-raises as before"

    def test_ttl_expiry_requeries(self, server, monkeypatch):
        counter = _CountingListNamespace(["team-a"])
        monkeypatch.setattr(server.v1, "list_namespace", counter, raising=False)
        # Shrink the TTL instead of sleeping 30s.
        monkeypatch.setattr(server, "_EXPANSION_CACHE_TTL_SECONDS", 0.0)
        asyncio.run(server._expand_allowed(("team-*",)))
        asyncio.run(server._expand_allowed(("team-*",)))
        assert counter.calls == 2, "expired entry requeries"


# ══════════════════════════════════════════════════════════════════════════
# 3. K8S_MCP_MAX_EVENTS / K8S_MCP_MAX_NS_REWRITE
# ══════════════════════════════════════════════════════════════════════════


def _event(name, kind="Pod", etype="Warning", ns="team-a", ts=None):
    return types.SimpleNamespace(
        type=etype,
        involved_object=types.SimpleNamespace(kind=kind, name=name),
        metadata=types.SimpleNamespace(namespace=ns),
        reason="BackOff",
        message=f"message-{name}",
        last_timestamp=ts,
        event_time=None,
    )


class TestMaxEventsKnob:
    def _setup_events(self, server, monkeypatch, n):
        events = [_event(f"e{i}", ts=None) for i in range(n)]
        monkeypatch.setattr(
            server.v1,
            "list_event_for_all_namespaces",
            lambda **kw: types.SimpleNamespace(items=events),
            raising=False,
        )

    def test_default_events_cap_is_100(self, server):
        os.environ.pop("K8S_MCP_MAX_EVENTS", None)
        assert server._max_events() == 100, "default event cap stays 100"

    def test_get_events_cap_env(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_EVENTS", "3")
        self._setup_events(server, monkeypatch, 5)
        out = asyncio.run(server.get_events())
        assert "EVENTS (3):" in out and "message-e3" not in out, "get_events honors K8S_MCP_MAX_EVENTS"

    def test_get_events_malformed_env_keeps_default(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_EVENTS", "banana")
        self._setup_events(server, monkeypatch, 3)
        out = asyncio.run(server.get_events())
        assert "EVENTS (3):" in out, "malformed value keeps the default cap"

    def test_get_events_zero_clamped_to_one(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_EVENTS", "0")
        self._setup_events(server, monkeypatch, 3)
        out = asyncio.run(server.get_events())
        assert "EVENTS (1):" in out, "0 clamps to 1 (never uncapped)"

    def test_triage_warning_cap_env(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_EVENTS", "2")
        warning_events = [_event(f"probe-{i}", etype="Warning") for i in range(4)]
        monkeypatch.setattr(
            server.v1,
            "list_namespaced_event",
            lambda ns, **kw: types.SimpleNamespace(items=warning_events),
            raising=False,
        )
        monkeypatch.setattr(
            server.v1,
            "list_namespaced_pod",
            lambda ns, **kw: types.SimpleNamespace(items=[]),
            raising=False,
        )
        for api in ("list_namespaced_deployment", "list_namespaced_stateful_set", "list_namespaced_daemon_set"):
            monkeypatch.setattr(server.apps_v1, api, lambda ns, **kw: types.SimpleNamespace(items=[]), raising=False)
        monkeypatch.setattr(server.batch_v1, "list_namespaced_job", lambda ns, **kw: types.SimpleNamespace(items=[]), raising=False)
        monkeypatch.setattr(server.v1, "list_namespaced_persistent_volume_claim", lambda ns, **kw: types.SimpleNamespace(items=[]), raising=False)
        monkeypatch.setattr(server.v1, "list_namespaced_service", lambda ns, **kw: types.SimpleNamespace(items=[]), raising=False)
        out = asyncio.run(server.triage(namespace="team-a"))
        assert "warnings=2" in out, "triage's Warning section honors K8S_MCP_MAX_EVENTS (same cap as get_events)"


class TestMaxNsRewriteKnob:
    def _plan(self, server, monkeypatch, n_namespaces):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", ",".join(f"ns{i}" for i in range(n_namespaces)))

        async def _safe_plan(argv):
            try:
                return await server._namespace_plan(argv)
            except server.NamespacePolicyError as e:
                return e

        return asyncio.run(_safe_plan(["get", "pods", "-A"]))

    def test_default_rewrite_limit_is_20(self, server):
        os.environ.pop("K8S_MCP_MAX_NS_REWRITE", None)
        assert server._max_ns_rewrite() == 20, "default rewrite limit stays 20"

    def test_below_limit_rewrites(self, server, monkeypatch):
        out = self._plan(server, monkeypatch, 5)
        assert [lbl for lbl, _ in out] == [f"ns{i}" for i in range(5)], "below the limit the rewrite proceeds"

    def test_above_default_limit_refuses(self, server, monkeypatch):
        out = self._plan(server, monkeypatch, 21)
        assert isinstance(out, server.NamespacePolicyError), "21 namespaces refuse at the default limit"
        assert "21 allowed namespaces exceed the 20-namespace" in str(out), "refusal names both counts"

    def test_raised_limit_rewrites_more(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_NS_REWRITE", "30")
        out = self._plan(server, monkeypatch, 25)
        # Plans are namespace-SORTED (lexicographic: ns0, ns1, ns10, ns11, …).
        assert [lbl for lbl, _ in out] == sorted(f"ns{i}" for i in range(25)), "raised limit rewrites more namespaces"

    def test_lowered_limit_refuses_earlier(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_NS_REWRITE", "2")
        out = self._plan(server, monkeypatch, 3)
        assert isinstance(out, server.NamespacePolicyError) and "exceed the 2-namespace" in str(out), (
            "lowered limit refuses earlier, naming the configured limit"
        )

    def test_malformed_limit_keeps_default(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_MAX_NS_REWRITE", "banana")
        assert server._max_ns_rewrite() == 20, "malformed value keeps the default"

    def test_zero_or_negative_clamped_to_one(self, server, monkeypatch):
        for bogus in ("0", "-3"):
            monkeypatch.setenv("K8S_MCP_MAX_NS_REWRITE", bogus)
            assert server._max_ns_rewrite() == 1, f"{bogus} clamps to 1 (limit stays meaningful)"


# ══════════════════════════════════════════════════════════════════════════
# 4. get_pod_logs namespace is REQUIRED
# ══════════════════════════════════════════════════════════════════════════


class TestPodLogsNamespaceRequired:
    def test_namespace_is_required_parameter(self, server):
        # The MCP schema itself: namespace has no default, so a client that
        # omits it gets a protocol-level "Field required" refusal before any
        # Python runs (the class of silent-default bug this removes).
        import inspect

        fn = server.get_pod_logs.fn if hasattr(server.get_pod_logs, "fn") else server.get_pod_logs
        sig = inspect.signature(fn)
        assert sig.parameters["namespace"].default is inspect.Parameter.empty, (
            "namespace carries no default in the registered schema"
        )

    def test_empty_namespace_refuses(self, server, monkeypatch):
        called = []

        def _spy(**kw):
            called.append(kw)
            return "should not be reached"

        monkeypatch.setattr(server.v1, "read_namespaced_pod_log", _spy, raising=False)
        out = asyncio.run(server.get_pod_logs(pod_name="x", namespace=""))
        assert out.startswith("Error:") and "namespace is required" in out, "empty namespace refuses clearly"
        assert not called, "no API call is made on refusal"

    def test_blank_namespace_refuses(self, server, monkeypatch):
        out = asyncio.run(server.get_pod_logs(pod_name="x", namespace="   "))
        assert out.startswith("Error:") and "namespace is required" in out, "whitespace-only namespace refuses too"

    def test_explicit_namespace_still_works(self, server, monkeypatch):
        monkeypatch.setattr(
            server.v1,
            "read_namespaced_pod_log",
            lambda **kw: "log line" if kw.get("namespace") == "team-a" else (_ for _ in ()).throw(AssertionError(kw)),
            raising=False,
        )
        out = asyncio.run(server.get_pod_logs(pod_name="x", namespace="team-a"))
        assert out == "log line", "explicit namespace behaves exactly as before"

    def test_denied_namespace_still_policy_refused(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "team-sec-x")
        out = asyncio.run(server.get_pod_logs(pod_name="x", namespace="team-sec-x"))
        assert out.startswith("Error:") and "denied by the namespace policy" in out, (
            "policy refusal unchanged (required-namespace check runs first, both refuse)"
        )
