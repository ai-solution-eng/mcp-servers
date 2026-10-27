"""Wave-5 tests: the triage(namespace, app) composite tool.

triage composes the server's OWN governed read paths (namespaced pod/
workload/event/PVC reads behind the namespace policy) into a one-call
namespace summary — attention list first, then pods, workloads, Warning
events, and PVCs — capped by K8S_MCP_TRIAGE_MAX_LINES. These tests pin:

  - the attention list EXACTLY on a mixed-health namespace (one
    CrashLoopBackOff pod, one Pending pod, healthy rest);
  - the app filter narrows pods + workloads (label-first, substring
    fallback — the list_pods convention);
  - the composite cap (default 150, >= 1, malformed → default) with a
    truncation marker naming the env;
  - policy refusals are byte-identical to list_pods' refusals and precede
    every underlying read;
  - the composite never bypasses: only the six namespaced reads fire —
    never a cluster-wide list and never kubectl.
"""

import asyncio
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ── Mock namespace objects (same shape the wave3 suite uses) ───────────────


def _container(name="app", restarts=0, waiting=None):
    state = types.SimpleNamespace(
        waiting=types.SimpleNamespace(reason=waiting) if waiting else None,
        running=None if waiting else types.SimpleNamespace(),
        terminated=None,
    )
    return types.SimpleNamespace(name=name, restart_count=restarts, state=state)


def _pod(
    ns,
    name,
    phase="Running",
    restarts=0,
    waiting=None,
    ready=None,
    node="worker-01",
    labels=None,
    container_statuses="default",
):
    """A pod. container_statuses=None models an unscheduled/pending pod."""
    if container_statuses == "default":
        container_statuses = [_container(restarts=restarts, waiting=waiting)]
    conditions = None
    if ready is not None:
        conditions = [types.SimpleNamespace(type="Ready", status=ready)]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=ns, name=name, labels=labels or {}),
        spec=types.SimpleNamespace(node_name=node),
        status=types.SimpleNamespace(
            phase=phase, container_statuses=container_statuses, start_time=None, conditions=conditions
        ),
    )


def _event(kind, name, ns, reason, message, etype="Warning"):
    return types.SimpleNamespace(
        type=etype,
        reason=reason,
        message=message,
        last_timestamp=None,
        event_time=None,
        involved_object=types.SimpleNamespace(kind=kind, name=name, namespace=ns),
        metadata=types.SimpleNamespace(namespace=ns),
    )


def _dep(ns, name, ready, desired, labels=None):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=ns, name=name, labels=labels or {}),
        status=types.SimpleNamespace(ready_replicas=ready),
        spec=types.SimpleNamespace(replicas=desired),
    )


def _sts(ns, name, ready, desired, labels=None):
    return _dep(ns, name, ready, desired, labels)


def _ds(ns, name, nready, ndesired, labels=None):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=ns, name=name, labels=labels or {}),
        status=types.SimpleNamespace(number_ready=nready, desired_number_scheduled=ndesired),
    )


def _pvc(ns, name, phase="Bound", cap="10Gi", sc="standard"):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=ns, name=name),
        spec=types.SimpleNamespace(storage_class_name=sc),
        status=types.SimpleNamespace(phase=phase, capacity={"storage": cap}),
    )


def _api_exc(reason="forbidden", status=403):
    cls = sys.modules["kubernetes.client.rest"].ApiException
    return cls(reason=reason, status=status)


class _Scene:
    """A mock namespace: six namespaced reads, each call recorded.

    Cluster-wide list paths and the kubectl path are replaced with BYPASS
    RECORDERS (they record and return empty — they never raise, because an
    exception would be swallowed by triage's gather(return_exceptions=True)
    and merely degrade a section); tests assert the recorders stayed empty,
    which is the mechanical proof that the composite never bypasses.
    """

    BYPASS_METHODS = (
        "list_pod_for_all_namespaces",
        "list_event_for_all_namespaces",
        "list_persistent_volume_claim_for_all_namespaces",
        "list_deployment_for_all_namespaces",
        "list_stateful_set_for_all_namespaces",
        "list_daemon_set_for_all_namespaces",
        "list_namespace",
    )

    def __init__(self, namespace="team-a", pods=(), deps=(), sts=(), dss=(), events=(), pvcs=()):
        self.namespace = namespace
        self.pods, self.deps, self.sts, self.dss = pods, deps, sts, dss
        self.events, self.pvcs = events, pvcs
        self.calls = []  # (method, args, kwargs) of namespaced reads
        self.bypass_calls = []  # cluster-wide / kubectl paths (must stay empty)
        self.fail = {}  # method name -> exception to raise
        self.overrides = {}  # method name -> replacement return value

    def install(self, server, monkeypatch):
        mapping = [
            (server.v1, "list_namespaced_pod", self.pods),
            (server.v1, "list_namespaced_event", self.events),
            (server.v1, "list_namespaced_persistent_volume_claim", self.pvcs),
            (server.apps_v1, "list_namespaced_deployment", self.deps),
            (server.apps_v1, "list_namespaced_stateful_set", self.sts),
            (server.apps_v1, "list_namespaced_daemon_set", self.dss),
        ]
        for obj, method, payload in mapping:

            def call(*args, _method=method, _payload=payload, **kwargs):
                self.calls.append((_method, args, kwargs))
                if _method in self.fail:
                    raise self.fail[_method]
                if _method in self.overrides:
                    return self.overrides[_method]
                return types.SimpleNamespace(items=list(_payload))

            monkeypatch.setattr(obj, method, call, raising=False)
        for obj in (server.v1, server.apps_v1):
            for method in self.BYPASS_METHODS:

                def _bypass(*args, _method=method, **kwargs):
                    self.bypass_calls.append(_method)
                    return types.SimpleNamespace(items=[])

                monkeypatch.setattr(obj, method, _bypass, raising=False)

        async def _no_kubectl(argv):
            self.bypass_calls.append(f"kubectl:{argv[0] if argv else '?'}")
            return ""

        monkeypatch.setattr(server, "_kubectl_plan_execute", _no_kubectl, raising=False)
        return self

    def namespaced_calls(self, method):
        return [(a, k) for (m, a, k) in self.calls if m == method]


HEALTHY = {"phase": "Running", "restarts": 0, "ready": "True"}


def _mixed_health_scene():
    """team-a: one CrashLoopBackOff pod, one Pending pod, healthy rest."""
    return _Scene(
        namespace="team-a",
        pods=(
            _pod("team-a", "api-7d9f", restarts=12, waiting="CrashLoopBackOff", ready="False"),
            _pod("team-a", "indexer-c9d8", phase="Pending", node=None, container_statuses=None),
            _pod("team-a", "worker-1", **HEALTHY),
            _pod("team-a", "worker-2", **HEALTHY),
        ),
        deps=(_dep("team-a", "api", 1, 2, labels={"app": "api"}), _dep("team-a", "worker", 2, 2)),
        events=(_event("Pod", "api-7d9f", "team-a", "BackOff", "Back-off restarting failed container api"),),
        pvcs=(_pvc("team-a", "api-data"),),
    )


# ══════════════════════════════════════════════════════════════════════════
# 1. Mixed health → attention list exact
# ══════════════════════════════════════════════════════════════════════════


class TestMixedHealthExact:
    def test_attention_list_exact(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)
        _mixed_health_scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert out == (
            "TRIAGE team-a: pods=4 workloads=2 warnings=1 pvcs=1 attention=2\n"
            "ATTENTION (2):\n"
            "  api-7d9f: CrashLoopBackOff — not Ready — 12 restarts\n"
            "  indexer-c9d8: Pending\n"
            "PODS (4):\n"
            "  team-a/api-7d9f: Running | Restarts: 12 | Age: Unknown | Node: worker-01\n"
            "  team-a/indexer-c9d8: Pending | Restarts: 0 | Age: Unknown | Node: unscheduled\n"
            "  team-a/worker-1: Running | Restarts: 0 | Age: Unknown | Node: worker-01\n"
            "  team-a/worker-2: Running | Restarts: 0 | Age: Unknown | Node: worker-01\n"
            "WORKLOADS:\n"
            "  Deployment team-a/api: 1/2 ready\n"
            "  Deployment team-a/worker: 2/2 ready\n"
            "EVENTS (Warning) (1):\n"
            "  [Warning] Pod/api-7d9f in team-a: BackOff - Back-off restarting "
            "failed container api (Age: Unknown)\n"
            "PVCs (1):\n"
            "  team-a/api-data: Bound | 10Gi | SC: standard"
        ), "mixed-health triage renders the exact documented shape"

    def test_empty_namespace_exact_shape(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)
        _Scene(namespace="empty-ns").install(server, monkeypatch)
        out = asyncio.run(server.triage("empty-ns"))
        assert out == (
            "TRIAGE empty-ns: pods=0 workloads=0 warnings=0 pvcs=0 attention=0\n"
            "ATTENTION (0):\n"
            "  (none) — all clear\n"
            "PODS: none\n"
            "WORKLOADS: none\n"
            "EVENTS (Warning): none\n"
            "PVCs: none"
        ), "an empty namespace renders a stable all-clear shape"


# ══════════════════════════════════════════════════════════════════════════
# 2. Attention criteria (restarts, not-Ready, OOMKilled/CrashLoopBackOff,
#    failing probes) — one line each, deterministic order
# ══════════════════════════════════════════════════════════════════════════


class TestAttentionCriteria:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)

    def _attention(self, server, monkeypatch, pods, events=()):
        _Scene(pods=pods, events=events).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        lines = out.split("\n")
        start = next(i for i, l in enumerate(lines) if l.startswith("ATTENTION"))
        end = next((i for i, l in enumerate(lines[start + 1 :], start + 1) if l and not l.startswith("  ")), len(lines))
        return out, lines[start + 1 : end]

    def test_oomkilled_pod_flagged_with_restarts(self, server, monkeypatch):
        _out, lines = self._attention(
            server, monkeypatch, (_pod("team-a", "web-9f8e", restarts=3, waiting="OOMKilled"),)
        )
        assert lines == ["  web-9f8e: OOMKilled — 3 restarts"], (
            f"OOMKilled flagged with its restart count (got {lines!r})"
        )

    def test_crashloop_without_restarts_reported_yet(self, server, monkeypatch):
        _out, lines = self._attention(
            server, monkeypatch, (_pod("team-a", "web-1", restarts=0, waiting="CrashLoopBackOff"),)
        )
        assert lines == ["  web-1: CrashLoopBackOff"], "a waiting reason flags the pod even before restarts accumulate"

    def test_not_ready_running_pod_flagged(self, server, monkeypatch):
        _out, lines = self._attention(server, monkeypatch, (_pod("team-a", "web-1", ready="False"),))
        assert lines == ["  web-1: not Ready"], "Ready=False on an otherwise-running pod is attention"

    def test_restarts_only_flagged(self, server, monkeypatch):
        _out, lines = self._attention(server, monkeypatch, (_pod("team-a", "web-1", restarts=2, ready="True"),))
        assert lines == ["  web-1: 2 restarts"], "restarts>0 alone is attention (the mission's first criterion)"

    def test_failed_phase_flagged_succeeded_not(self, server, monkeypatch):
        _out, lines = self._attention(
            server,
            monkeypatch,
            (
                _pod("team-a", "job-runner", phase="Failed", ready="False"),
                _pod("team-a", "migrate-done", phase="Succeeded", ready="False"),
            ),
        )
        assert lines == ["  job-runner: Failed"], (
            "Failed flagged; a Succeeded (completed Job) pod is normal, not attention"
        )

    def test_imagepull_flagged(self, server, monkeypatch):
        _out, lines = self._attention(server, monkeypatch, (_pod("team-a", "api-x1", waiting="ImagePullBackOff"),))
        assert lines == ["  api-x1: ImagePullBackOff"], "non-running waiting reasons surface (they need attention too)"

    def test_healthy_pod_never_flagged(self, server, monkeypatch):
        _out, lines = self._attention(server, monkeypatch, (_pod("team-a", "worker-1", **HEALTHY),))
        assert lines == ["  (none) — all clear"], "healthy pod → all clear"

    def test_probe_failure_event_becomes_attention_line(self, server, monkeypatch):
        _out, lines = self._attention(
            server,
            monkeypatch,
            (_pod("team-a", "api-7d9f", **HEALTHY),),
            events=(
                _event(
                    "Pod",
                    "api-7d9f",
                    "team-a",
                    "Unhealthy",
                    "Readiness probe failed: HTTP probe failed with statuscode: 500",
                ),
            ),
        )
        assert lines == [
            "  api-7d9f: probe failing — Readiness probe failed: HTTP probe failed with statuscode: 500"
        ], "an Unhealthy Warning event renders as a probe-failing attention line"

    def test_non_unhealthy_warning_is_not_a_probe_line(self, server, monkeypatch):
        _out, lines = self._attention(
            server,
            monkeypatch,
            (_pod("team-a", "worker-1", **HEALTHY),),
            events=(_event("Pod", "worker-1", "team-a", "BackOff", "Back-off restarting…"),),
        )
        assert lines == ["  (none) — all clear"], "only reason=Unhealthy events count as failing probes"

    def test_probe_line_filtered_by_app(self, server, monkeypatch):
        _Scene(
            pods=(_pod("team-a", "api-7d9f", **HEALTHY),),
            events=(
                _event("Pod", "api-7d9f", "team-a", "Unhealthy", "Readiness probe failed"),
                _event("Pod", "other-1", "team-a", "Unhealthy", "Liveness probe failed"),
            ),
        ).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a", app="api"))
        assert "other-1: probe failing" not in out and "api-7d9f: probe failing" in out, (
            "with app= the probe lines narrow with the pods"
        )


# ══════════════════════════════════════════════════════════════════════════
# 3. app filter narrows pods + workloads (label-first, then substring)
# ══════════════════════════════════════════════════════════════════════════


class TestAppFilter:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)

    def _scene(self):
        return _Scene(
            namespace="team-a",
            pods=(
                _pod("team-a", "api-7d9f", labels={"app": "api"}, **HEALTHY),
                _pod("team-a", "api-by-k8s-label", labels={"app.kubernetes.io/name": "api"}, **HEALTHY),
                _pod("team-a", "name-contains-api", **HEALTHY),
                _pod("team-a", "unrelated", labels={"app": "other"}, **HEALTHY),
            ),
            deps=(
                _dep("team-a", "api", 2, 2, labels={"app": "api"}),
                _dep("team-a", "unrelated", 1, 1, labels={"app": "other"}),
            ),
            sts=(_sts("team-a", "api-db", 1, 1),),
            dss=(_ds("team-a", "api-agent", 3, 3),),
            events=(_event("Pod", "api-7d9f", "team-a", "BackOff", "Back-off…"),),
            pvcs=(_pvc("team-a", "api-data"),),
        )

    def test_app_narrows_pods_and_workloads(self, server, monkeypatch):
        self._scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a", app="api"))
        assert "pods=3 workloads=3" in out.split("\n")[0], "header counts only the narrowed pods and workloads"
        assert "team-a/unrelated" not in out, "other-app pod withheld"
        assert "Deployment team-a/unrelated" not in out, "other-app workload withheld"
        for kept in (
            "team-a/api-7d9f",
            "team-a/api-by-k8s-label",
            "team-a/name-contains-api",
            "Deployment team-a/api",
            "StatefulSet team-a/api-db",
            "DaemonSet team-a/api-agent",
        ):
            assert kept in out, f"narrowed triage keeps {kept}"

    def test_label_match_wins_over_substring_false_positives(self, server, monkeypatch):
        _Scene(
            pods=(
                _pod("team-a", "rapid-scheduler", labels={"app": "api"}, **HEALTHY),
                _pod("team-a", "other", labels={"app": "other"}, **HEALTHY),
            )
        ).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a", app="api"))
        assert "rapid-scheduler" in out, "label match picks the app"
        assert "\n  team-a/other:" not in out, "'api' is a substring of 'rapid' — only label or name matches qualify"

    def test_without_app_everything_shown(self, server, monkeypatch):
        self._scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "pods=4 workloads=4" in out.split("\n")[0], "no app → full namespace counts"
        assert "team-a/unrelated" in out and "Deployment team-a/unrelated" in out

    def test_app_filter_follows_into_attention(self, server, monkeypatch):
        _Scene(
            pods=(
                _pod("team-a", "api-7d9f", labels={"app": "api"}, **HEALTHY),
                _pod(
                    "team-a", "other-1", labels={"app": "other"}, restarts=7, waiting="CrashLoopBackOff", ready="False"
                ),
            )
        ).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a", app="api"))
        assert "ATTENTION (0):" in out, "the other app's crashlooping pod is out of scope for app='api'"

    def test_invalid_app_rejected(self, server, monkeypatch):
        self._scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a", app="bad app!"))
        assert out.startswith("Error:") and "invalid resource name" in out, (
            "app is name-validated like every other server string parameter"
        )


# ══════════════════════════════════════════════════════════════════════════
# 4. Composite cap: K8S_MCP_TRIAGE_MAX_LINES (default 150)
# ══════════════════════════════════════════════════════════════════════════


class TestCompositeCap:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)

    def test_default_cap_is_150(self, server):
        os.environ.pop("K8S_MCP_TRIAGE_MAX_LINES", None)
        assert server._triage_max_lines() == 150, "default composite cap is 150"

    def test_malformed_env_keeps_default(self, server, monkeypatch):
        for garbage in ("banana", "", "  ", "3.5"):
            monkeypatch.setenv("K8S_MCP_TRIAGE_MAX_LINES", garbage)
            assert server._triage_max_lines() == 150, f"malformed {garbage!r} keeps the default cap"

    def test_nonpositive_clamped_to_one(self, server, monkeypatch):
        for bogus in ("0", "-5"):
            monkeypatch.setenv("K8S_MCP_TRIAGE_MAX_LINES", bogus)
            assert server._triage_max_lines() == 1, f"{bogus} clamps to 1"

    def test_cap_truncates_with_marker(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_TRIAGE_MAX_LINES", "6")
        _mixed_health_scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        lines = out.split("\n")
        assert len(lines) == 7, "cap keeps exactly 6 lines + the marker line"
        assert lines[0].startswith("TRIAGE team-a:") and lines[1] == "ATTENTION (2):", (
            "summary header and the attention list are inside the kept head"
        )
        assert lines[5] == ("  team-a/api-7d9f: Running | Restarts: 12 | Age: Unknown | Node: worker-01"), (
            "the head of the output is kept in order"
        )
        assert lines[6] == (
            "  ... (10 more lines not shown — triage output truncated "
            "at 6 by K8S_MCP_TRIAGE_MAX_LINES; narrow with app= or "
            "raise the env)"
        ), "marker names the env and the omitted count"

    def test_attention_survives_the_cap(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_TRIAGE_MAX_LINES", "4")
        _mixed_health_scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "ATTENTION (2):" in out and "api-7d9f: CrashLoopBackOff" in out, (
            "ATTENTION renders first, so the cap can never hide the findings"
        )

    def test_below_cap_unchanged(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)
        _mixed_health_scene().install(server, monkeypatch)
        uncapped = asyncio.run(server.triage("team-a"))
        assert "more lines not shown" not in uncapped, "no marker below the cap"
        monkeypatch.setenv("K8S_MCP_TRIAGE_MAX_LINES", "150")
        capped = asyncio.run(server.triage("team-a"))
        assert uncapped == capped, "at the default cap the output is byte-identical"

    def test_app_narrowing_defeats_the_cap(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_TRIAGE_MAX_LINES", "30")
        big = _Scene(
            namespace="team-a",
            pods=(_pod("team-a", f"bulk-{i:03d}", **HEALTHY) for i in range(200)),
        )
        big.pods = list(big.pods)
        big.install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "more lines not shown" in out, "200 pods overflow the 30-line cap"
        out_narrow = asyncio.run(server.triage("team-a", app="api"))
        assert "more lines not shown" not in out_narrow, (
            "the marker's own advice works: app= narrowing fits under the cap"
        )


# ══════════════════════════════════════════════════════════════════════════
# 5. Namespace policy: refused like list_pods, before any read
# ══════════════════════════════════════════════════════════════════════════


class TestNamespacePolicy:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)

    def test_denied_namespace_refused_like_list_pods(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-a")
        scene = _Scene(namespace="team-b").install(server, monkeypatch)
        triage_out = asyncio.run(server.triage("team-b"))
        pods_out = asyncio.run(server.list_pods(namespace="team-b"))
        assert triage_out.startswith("Error: namespace 'team-b'"), "triage refuses a namespace outside the whitelist"
        assert triage_out == pods_out, "the refusal is byte-identical to list_pods' refusal"
        assert scene.calls == [], "the refusal precedes every underlying read"

    def test_blocked_namespace_refused(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.setenv("K8S_MCP_BLOCKED_NAMESPACES", "kube-*")
        scene = _Scene(namespace="kube-system").install(server, monkeypatch)
        out = asyncio.run(server.triage("kube-system"))
        assert out == asyncio.run(server.list_pods(namespace="kube-system")), (
            "blacklist refusal matches list_pods exactly (blacklist always wins)"
        )
        assert scene.calls == [], "no underlying read on a blocked namespace"

    def test_invalid_namespace_refused(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        out = asyncio.run(server.triage("Not_A_Namespace"))
        assert out.startswith("Error: invalid namespace"), "malformed namespace refused like list_pods"

    def test_empty_namespace_required(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        out = asyncio.run(server.triage(""))
        assert out.startswith("Error:") and "requires a namespace" in out, (
            "triage is namespaced-only; an empty namespace is refused up front"
        )

    def test_allowed_namespace_passes_and_hits_namespaced_reads(self, server, monkeypatch):
        monkeypatch.setenv("K8S_MCP_ALLOWED_NAMESPACES", "team-a,team-b")
        scene = _mixed_health_scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert out.startswith("TRIAGE team-a:"), "allowed namespace triages normally"
        pod_calls = scene.namespaced_calls("list_namespaced_pod")
        assert len(pod_calls) == 1 and pod_calls[0][0] == ("team-a",), (
            "the pod read is the NAMESPACED call with the target namespace"
        )

    def test_uppercase_namespace_normalized_like_list_pods(self, server, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        scene = _mixed_health_scene().install(server, monkeypatch)
        out = asyncio.run(server.triage("TEAM-A"))
        assert out.startswith("TRIAGE team-a:"), "namespace is lowercased exactly as the list_pods path does"
        assert scene.namespaced_calls("list_namespaced_pod")[0][0] == ("team-a",)


# ══════════════════════════════════════════════════════════════════════════
# 6. The composite composes — governed reads only, per-section degradation
# ══════════════════════════════════════════════════════════════════════════


class TestGovernedComposition:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("K8S_MCP_ALLOWED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_BLOCKED_NAMESPACES", raising=False)
        monkeypatch.delenv("K8S_MCP_TRIAGE_MAX_LINES", raising=False)

    def test_exactly_the_six_namespaced_reads(self, server, monkeypatch):
        scene = _mixed_health_scene().install(server, monkeypatch)
        asyncio.run(server.triage("team-a"))
        methods = [m for (m, _a, _k) in scene.calls]
        assert sorted(methods) == sorted(
            [
                "list_namespaced_pod",
                "list_namespaced_deployment",
                "list_namespaced_stateful_set",
                "list_namespaced_daemon_set",
                "list_namespaced_event",
                "list_namespaced_persistent_volume_claim",
            ]
        ), "triage performs exactly the six namespaced governed reads, once each"
        assert scene.bypass_calls == [], "no cluster-wide list and no kubectl call — the composite only composes"

    def test_warning_only_events_with_get_events_cap(self, server, monkeypatch):
        events = (
            _event("Pod", "api-1", "team-a", "BackOff", "w1"),
            _event("Pod", "api-2", "team-a", "Pulling", "informational", etype="Normal"),
            _event("Pod", "api-3", "team-a", "Unhealthy", "w3"),
        )
        _Scene(pods=(_pod("team-a", "api-1", **HEALTHY),), events=events).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "EVENTS (Warning) (2):" in out, "Normal events filtered like get_events"
        assert "BackOff" in out and "Unhealthy" in out and "Pulling" not in out, "only Warning events render"

    def test_events_capped_at_100_like_get_events(self, server, monkeypatch):
        events = tuple(_event("Pod", f"p{i}", "team-a", "BackOff", f"m{i}") for i in range(130))
        _Scene(events=events).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "EVENTS (Warning) (100):" in out, "the existing get_events 100-item cap is reused verbatim"

    def test_pvc_section_matches_list_pvcs_format(self, server, monkeypatch):
        _Scene(
            pvcs=(
                _pvc("team-a", "data", phase="Bound", cap="10Gi", sc="standard"),
                _pvc("team-a", "pending-pvc", phase="Pending", cap="?", sc=None),
            )
        ).install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert (
            "  team-a/data: Bound | 10Gi | SC: standard" in out
            and "  team-a/pending-pvc: Pending | ? | SC: None" in out
        ), "PVC lines carry the list_pvcs phase/capacity/storage-class fields"

    def test_workload_read_failure_degrades_its_section_only(self, server, monkeypatch):
        scene = _mixed_health_scene()
        scene.fail["list_namespaced_deployment"] = _api_exc()
        scene.install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "  Deployment: Error - forbidden (403)" in out, "a failed workload read is surfaced in its section"
        assert "workloads=0" in out.split("\n")[0], "header counts no phantom workloads"
        assert "PODS (4):" in out and "PVCs (1):" in out, "the other sections still render"

    def test_event_read_failure_degrades_its_section(self, server, monkeypatch):
        scene = _mixed_health_scene()
        scene.fail["list_namespaced_event"] = _api_exc(reason="Throttled", status=429)
        scene.install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "EVENTS (Warning): Error - Throttled (429)" in out, "a failed events read is surfaced honestly"
        assert "warnings=0" in out.split("\n")[0]

    def test_pvc_read_failure_degrades_its_section(self, server, monkeypatch):
        scene = _mixed_health_scene()
        scene.fail["list_namespaced_persistent_volume_claim"] = _api_exc()
        scene.install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        assert "PVCs: Error - forbidden (403)" in out and "PODS (4):" in out

    def test_pod_read_failure_fails_like_list_pods(self, server, monkeypatch):
        scene = _mixed_health_scene()
        scene.fail["list_namespaced_pod"] = _api_exc(reason="Forbidden", status=403)
        scene.install(server, monkeypatch)
        out = asyncio.run(server.triage("team-a"))
        pods_out = asyncio.run(server.list_pods(namespace="team-a"))
        assert out == "K8s API Error: Forbidden (403)", "the core read failing fails the triage"
        assert out.split(" (")[0] == pods_out.split(" (")[0], "same K8s API Error shape as list_pods"


# ══════════════════════════════════════════════════════════════════════════
# 7. Registration + schema contract
# ══════════════════════════════════════════════════════════════════════════


class TestRegistration:
    def test_triage_registered(self, server):
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "triage" in names, "triage is a registered MCP tool"

    def test_triage_schema(self, server):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        schema = getattr(tools["triage"], "input_schema", None) or tools["triage"].inputSchema
        assert schema.get("required") == ["namespace"], "namespace is required"
        props = schema.get("properties") or {}
        assert props["namespace"].get("type") == "string", "namespace is a string"
        assert props["app"].get("type") == "string", "app is an optional string"
        assert set(props) == {"namespace", "app"}, "exactly the two documented params"
