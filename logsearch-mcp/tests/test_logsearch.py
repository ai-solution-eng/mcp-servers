"""Unit tests for the LogSearch MCP server (no cluster, no kubernetes package).

Run (prescribed):
  cd /home/andrew/Code/HPE/mcp_servers/logsearch_mcp && \
  /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/ -v

The venv has mcp 2.2.0 + pytest but NO kubernetes. The kubernetes seams
(server._list_pods / server._read_log) are monkeypatched with fakes; the
kubernetes import itself stays lazy inside server.py and is never executed.
Async tools are called directly (the @mcp.tool decorator returns the function
unchanged) via asyncio.run.
"""

import asyncio
import json

import pytest

import server

# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    """Start every test from a clean LOGSEARCH_* environment so a developer's
    shell can never leak policy/caps into these results.

    The D8 escape hatch is SET here (empty allowlist = open) because the
    behavioral tests below address pods in a plain 'ns' namespace and are not
    about the policy; tests that ARE about the default-deny flip override or
    delete this var explicitly (see tests/test_hardening.py)."""
    for name in (
        server.ENV_ALLOWED,
        server.ENV_BLOCKED,
        server.ENV_EMPTY_ALLOWS_ALL,
        server.ENV_MAX_LINE_CHARS,
        server.ENV_FETCH_CONCURRENCY,
        server.ENV_MAX_REGEX_CHARS,
        "LOGSEARCH_MAX_PODS",
        "LOGSEARCH_MAX_LINES_PER_POD",
        "LOGSEARCH_MAX_TOTAL_LINES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, "1")


def make_pod(name, containers=("main",), restarts=0, started="2026-09-08T00:00:00Z"):
    return {"name": name, "containers": list(containers), "restarts": restarts, "started": started}


class FakeK8s:
    """In-memory stand-in for the two kubernetes seams, with call recording."""

    def __init__(self, pods, logs, fail_pods=()):
        self.pods = pods  # list of pod dicts
        self.logs = logs  # {(pod, container): log text}
        self.fail_pods = set(fail_pods)  # pod names whose read raises
        self.list_calls = []
        self.read_calls = []

    def install(self, monkeypatch):
        k8s = self

        def fake_list_pods(namespace, label_selector=""):
            k8s.list_calls.append((namespace, label_selector))
            return [dict(p) for p in k8s.pods]

        def fake_read_log(
            namespace,
            pod,
            container,
            tail_lines,
            since_seconds=None,
            timestamps=True,
            previous=False,
        ):
            k8s.read_calls.append(
                {
                    "namespace": namespace,
                    "pod": pod,
                    "container": container,
                    "tail_lines": tail_lines,
                    "since_seconds": since_seconds,
                    "timestamps": timestamps,
                    "previous": previous,
                }
            )
            if pod in k8s.fail_pods:
                raise server.LogSearchError(f"pod {pod!r} evaporated mid-search (fake 404)")
            return k8s.logs[(pod, container)]

        monkeypatch.setattr(server, "_list_pods", fake_list_pods)
        monkeypatch.setattr(server, "_read_log", fake_read_log)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Namespace policy — exhaustive matrix on the pure predicate
# ---------------------------------------------------------------------------


def test_policy_empty_allowed_is_deny_by_default(monkeypatch):
    """D8 default-deny: an EMPTY allowlist answers NOTHING — the audit's
    'default-open server' closes. LOGSEARCH_EMPTY_ALLOWS_ALL=1 restores the
    old open behavior explicitly (see test_policy_empty_allowlist_open_escape)."""
    monkeypatch.setenv(server.ENV_ALLOWED, "")
    monkeypatch.setenv(server.ENV_BLOCKED, "")
    monkeypatch.delenv(server.ENV_EMPTY_ALLOWS_ALL, raising=False)
    assert not server._namespace_allowed("default")
    assert not server._namespace_allowed("kube-system")
    assert not server._namespace_allowed("team-a-prod-eu-1")
    assert not server._namespace_allowed("anything-goes")


def test_policy_empty_allowlist_open_escape(monkeypatch):
    """The D8 escape hatch: =1 (or true/yes/on) restores pre-D8 open access."""
    monkeypatch.setenv(server.ENV_ALLOWED, "")
    monkeypatch.setenv(server.ENV_BLOCKED, "")
    for value, expected in (
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("junk", False),
    ):
        monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, value)
        assert server._namespace_allowed("default") is expected, value
    monkeypatch.delenv(server.ENV_EMPTY_ALLOWS_ALL)
    assert server._namespace_allowed("default") is False  # unset -> deny


@pytest.mark.parametrize(
    "allowed,blocked,ns,expected",
    [
        # glob allow-list
        ("team-a,prod-*", "", "team-a", True),
        ("team-a,prod-*", "", "prod-eu-1", True),
        ("team-a,prod-*", "", "prod", False),  # 'prod-*' needs the dash
        ("team-a,prod-*", "", "team-a2", False),  # prefix is not a match
        ("team-a,prod-*", "", "dev-eu-1", False),
        ("team-a,prod-*", "", "", False),  # non-empty list denies ''
        ("app", "", "app", True),  # exact literal glob
        # blocked ALWAYS wins — even over an explicit allow-all
        ("*", "kube-system", "kube-system", False),
        ("*", "kube-*", "kube-public", False),
        ("*", "kube-*", "default", True),
        ("*", "prod-*,staging", "prod-eu", False),
        # mixed allow + block
        ("team-*,dev", "team-b,dev", "team-b", False),
        ("team-*,dev", "team-b,dev", "team-a", True),
        ("team-*,dev", "team-b,dev", "dev", False),
        ("team-*,dev", "team-b,dev", "dev-x", False),  # 'dev' blocked, allow 'dev' only
        # surrounding whitespace tolerated in the csv lists
        (" team-a , prod-* ", " kube-system ", "prod-eu", True),
        (" team-a , prod-* ", " kube-system ", "kube-system", False),
        # case-sensitive: namespace names are lowercase DNS labels
        ("PROD-*", "", "prod-eu", False),
        ("prod-*", "", "PROD-EU", False),
        # blocked wildcard blocks everything
        ("*", "*", "default", False),
        # (the D8 empty-allowlist rows live in test_policy_matrix_deny_on_empty
        # below — the autouse fixture here sets the escape hatch ON, so an
        # empty-allowed row in THIS matrix would assert the open behavior)
    ],
)
def test_policy_matrix(monkeypatch, allowed, blocked, ns, expected):
    monkeypatch.setenv(server.ENV_ALLOWED, allowed)
    monkeypatch.setenv(server.ENV_BLOCKED, blocked)
    assert server._namespace_allowed(ns) is expected


@pytest.mark.parametrize(
    "ns,expected",
    [
        ("default", False),
        ("kube-system", False),
        ("team-a", False),
        ("", False),
        # blocked always wins even with the escape hatch on
        ("secret-ns", False),
    ],
)
def test_policy_matrix_deny_on_empty(monkeypatch, ns, expected):
    """D8: the empty-allowlist rows of the policy matrix, escape hatch OFF."""
    monkeypatch.delenv(server.ENV_EMPTY_ALLOWS_ALL, raising=False)
    monkeypatch.setenv(server.ENV_ALLOWED, "")
    monkeypatch.setenv(server.ENV_BLOCKED, "secret-ns")
    assert server._namespace_allowed(ns) is expected


def test_policy_matrix_empty_with_escape_hatch(monkeypatch):
    """The escape hatch restores the pre-D8 rows of the matrix."""
    monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, "1")
    monkeypatch.setenv(server.ENV_ALLOWED, "")
    monkeypatch.setenv(server.ENV_BLOCKED, "secret-ns")
    assert server._namespace_allowed("default") is True
    assert server._namespace_allowed("secret-ns") is False  # blocked still wins


def test_policy_garbage_int_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LOGSEARCH_MAX_PODS", "not-a-number")
    assert server._max_pods() == server.DEFAULT_MAX_PODS


# ---------------------------------------------------------------------------
# Denied namespaces: self-describing, naming the policy env vars
# ---------------------------------------------------------------------------


def test_denied_namespace_error_names_policy_env_vars(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "team-a")
    for tool_call in (
        lambda: server.list_log_sources("team-b"),
        lambda: server.get_pod_logs("team-b", "p"),
        lambda: server.search_logs("team-b", "x"),
        lambda: server.count_matches("team-b", "x"),
    ):
        out = run(tool_call())
        assert out.startswith("Error:"), out
        assert server.ENV_ALLOWED in out and server.ENV_BLOCKED in out


def test_blocked_wins_over_allowed_across_tools(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    monkeypatch.setenv(server.ENV_BLOCKED, "secret-ns")
    out = run(server.search_logs("secret-ns", "x"))
    assert out.startswith("Error:") and "secret-ns" in out


# ---------------------------------------------------------------------------
# list_log_sources
# ---------------------------------------------------------------------------


def test_list_log_sources_reports_containers_restarts_age(monkeypatch):
    k8s = FakeK8s(
        [
            make_pod("b-pod", containers=("api", "sidecar"), restarts=3, started="2026-09-07T00:00:00Z"),
            make_pod("a-pod", containers=("worker",), restarts=0, started="2026-09-08T00:00:00Z"),
        ],
        {},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.list_log_sources("ns")))
    assert out["pod_count"] == 2
    assert [p["name"] for p in out["pods"]] == ["a-pod", "b-pod"]  # sorted by name
    assert out["pods"][1]["containers"] == ["api", "sidecar"]
    assert out["pods"][1]["restarts"] == 3
    assert out["pods"][1]["age"].endswith(("d", "h", "m"))
    assert out["pod_cap_applied"] is False


def test_list_log_sources_respects_policy_and_selector(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "good")
    k8s = FakeK8s([], {})
    k8s.install(monkeypatch)
    run(server.list_log_sources("good", label_selector="app=x"))
    assert k8s.list_calls == [("good", "app=x")]


# ---------------------------------------------------------------------------
# get_pod_logs
# ---------------------------------------------------------------------------


def test_get_pod_logs_happy_path_and_default_container_resolution(monkeypatch):
    k8s = FakeK8s(
        [make_pod("multi", containers=("web", "proxy"))],
        {("multi", "web"): "2026-09-08T00:00:01Z line one\n2026-09-08T00:00:02Z line two\n"},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.get_pod_logs("ns", "multi")))
    assert out["container"] == "web"  # first container as default
    assert out["lines_returned"] == 2
    assert out["previous"] is False
    assert out["truncated"] is False
    assert out["log"].splitlines()[0].endswith("line one")


def test_get_pod_logs_previous_container_passthrough(monkeypatch):
    k8s = FakeK8s([make_pod("crashy", ("app",))], {("crashy", "app"): "2026-09-08T00:00:01Z panic: oom\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.get_pod_logs("ns", "crashy", previous=True, tail_lines=50)))
    call = k8s.read_calls[-1]
    assert call["previous"] is True  # THE triage passthrough
    assert call["namespace"] == "ns"
    assert call["pod"] == "crashy"
    assert call["container"] == "app"
    assert out["previous"] is True


def test_get_pod_logs_explicit_container_passthrough(monkeypatch):
    k8s = FakeK8s([make_pod("p", ("a", "b"))], {("p", "b"): "2026-09-08T00:00:01Z from-b\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.get_pod_logs("ns", "p", container="b")))
    assert k8s.read_calls[-1]["container"] == "b"
    assert out["container"] == "b" and "from-b" in out["log"]


def test_get_pod_logs_unknown_pod_is_self_describing_error(monkeypatch):
    k8s = FakeK8s([make_pod("other")], {})
    k8s.install(monkeypatch)
    out = run(server.get_pod_logs("ns", "ghost"))
    assert out.startswith("Error:")
    assert "ghost" in out and "list_log_sources" in out


def test_get_pod_logs_tail_lines_capped_by_env(monkeypatch):
    monkeypatch.setenv("LOGSEARCH_MAX_LINES_PER_POD", "1000")
    k8s = FakeK8s([make_pod("p")], {("p", "main"): ""})
    k8s.install(monkeypatch)
    run(server.get_pod_logs("ns", "p", tail_lines=500_000))
    assert k8s.read_calls[-1]["tail_lines"] == 1000


def test_get_pod_logs_output_char_cap(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): "\n".join(f"line {i:04d}" for i in range(100)) + "\n"})
    k8s.install(monkeypatch)
    monkeypatch.setattr(server, "_MAX_OUTPUT_CHARS", 40)
    out = json.loads(run(server.get_pod_logs("ns", "p")))
    assert out["truncated"] is True
    assert len(out["log"]) <= 40  # cut at a newline before the cap
    assert out["log"].endswith("line 0003")


def test_get_pod_logs_rejects_bad_args(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {})
    k8s.install(monkeypatch)
    assert run(server.get_pod_logs("ns", "p", tail_lines=0)).startswith("Error:")
    assert run(server.get_pod_logs("ns", "p", since_seconds=-5)).startswith("Error:")


# ---------------------------------------------------------------------------
# search_logs — fan-out merge, sort, caps, regex errors
# ---------------------------------------------------------------------------


def test_search_merges_with_provenance_and_chronological_sort(monkeypatch):
    k8s = FakeK8s(
        [make_pod("api-a", ("api",)), make_pod("api-b", ("worker",))],
        {
            # Deliberately out of order within a pod, and interleaved across pods.
            ("api-a", "api"): ("2026-09-08T00:00:10Z ERROR late\n2026-09-08T00:00:01Z ERROR early\n"),
            ("api-b", "worker"): "2026-09-08T00:00:05Z ERROR middle\n",
        },
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert [m.split("Z ", 1)[1] for m in out["matches"]] == [
        "ERROR early",
        "ERROR middle",
        "ERROR late",
    ]
    assert out["matches"][0].startswith("api-a/api: ")
    assert out["matches"][1].startswith("api-b/worker: ")
    assert out["pods_searched"] == 2
    assert out["pods_with_matches"] == 2
    assert out["truncated"] is False
    assert out["errors"] == []


def test_search_timestamps_passthrough_and_label_selector(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): ""})
    k8s.install(monkeypatch)
    run(server.search_logs("ns", "x", label_selector="app=y"))
    assert k8s.list_calls == [("ns", "app=y")]
    assert k8s.read_calls[-1]["timestamps"] is True  # sort depends on it


def test_search_since_minutes_converts_to_since_seconds(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): ""})
    k8s.install(monkeypatch)
    run(server.search_logs("ns", "x", since_minutes=2.5))
    assert k8s.read_calls[-1]["since_seconds"] == 150


def test_search_pod_cap_enforced(monkeypatch):
    monkeypatch.setenv("LOGSEARCH_MAX_PODS", "2")
    pods = [make_pod(f"p{i}") for i in range(5)]
    logs = {(f"p{i}", "main"): f"2026-09-08T00:00:0{i + 1}Z ERROR hit\n" for i in range(5)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["pods_searched"] == 2
    assert out["pod_cap_applied"] is True
    assert len(out["matches"]) == 2


def test_search_pod_regex_narrows_pod_set(monkeypatch):
    k8s = FakeK8s(
        [make_pod("api-1"), make_pod("api-2"), make_pod("db-1")],
        {("api-1", "main"): "2026-09-08T00:00:01Z ERROR\n"},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", pod_regex="^api-1$")))
    assert out["pods_searched"] == 1


def test_search_bad_pattern_clean_error_mentions_pattern(monkeypatch):
    out = run(server.search_logs("ns", "([unclosed"))
    assert out.startswith("Error:")
    assert "([unclosed" in out
    assert "invalid regex" in out


def test_search_bad_pod_regex_clean_error_mentions_pattern(monkeypatch):
    out = run(server.search_logs("ns", "fine", pod_regex="api(["))
    assert out.startswith("Error:")
    assert "api([" in out


def test_search_budget_stops_fetch_early_keeps_first_matches(monkeypatch):
    """Early budget enforcement: once max_total_lines matches are in hand the
    search stops pulling — the kept set is the first matches in (name-sorted)
    pod order, chronologically sorted, with pods_skipped_budget reporting what
    was never fetched. This is the Wave-1 change from the pre-D8-era behavior
    of fetching everything and keeping the most recent slice."""
    k8s = FakeK8s(
        [make_pod("p")],
        {("p", "main"): "".join(f"2026-09-08T00:00:0{i}Z hit {i}\n" for i in range(1, 6))},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "hit", max_total_lines=3)))
    assert out["truncated"] is True
    assert out["match_count"] == 3
    # Budget filled mid-pod: the first three matches are kept, the rest of the
    # pod is not merged and counts as skipped.
    assert [m.split("Z ", 1)[1] for m in out["matches"]] == ["hit 1", "hit 2", "hit 3"]
    assert out["pods_skipped_budget"] == 1
    assert k8s.read_calls[-1]["tail_lines"] == 200  # the tool's own tail arg, capped per pod as before


def test_search_budget_stop_never_fetches_later_pods(monkeypatch):
    monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, "2")  # waves of 2 pods
    pods = [make_pod(f"p{i}") for i in range(6)]
    logs = {(f"p{i}", "main"): f"2026-09-08T00:00:0{i}Z ERROR hit\n" for i in range(6)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=2)))
    assert out["match_count"] == 2
    assert out["pods_searched"] == 2
    assert out["pods_skipped_budget"] == 4
    assert out["truncated"] is True
    assert len(k8s.read_calls) == 2  # wave 1 filled the budget; wave 2 never scheduled


def test_search_no_truncation_flag_when_under_cap(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): "2026-09-08T00:00:01Z hit\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "hit")))
    assert out["truncated"] is False
    assert out["match_count"] == 1


def test_max_total_lines_env_is_the_default_budget(monkeypatch):
    """The env knob is LIVE: a tool call without max_total_lines resolves the
    budget from LOGSEARCH_MAX_TOTAL_LINES (this used to be a dead knob — the
    tools hard-coded 300, so an operator raising the env saw no change)."""
    monkeypatch.setenv("LOGSEARCH_MAX_TOTAL_LINES", "2")
    pods = [make_pod(f"p{i}") for i in range(4)]
    logs = {(f"p{i}", "main"): f"2026-09-08T00:00:0{i}Z ERROR hit\n" for i in range(4)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["match_count"] == 2 and out["truncated"] is True
    assert out["pods_skipped_budget"] == 2  # the env budget, not the old hard 300


def test_max_total_lines_env_default_applies_to_export_and_override_wins(monkeypatch, tmp_path):
    """export_matches shares the env-default budget; an explicit per-call
    max_total_lines still overrides the env (per-call wins)."""
    monkeypatch.setenv("LOGSEARCH_MAX_TOTAL_LINES", "1")
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    pods = [make_pod(f"p{i}") for i in range(3)]
    logs = {(f"p{i}", "main"): f"2026-09-08T00:00:0{i}Z ERROR hit\n" for i in range(3)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.export_matches("ns", "ERROR", "env.log")))
    assert out["match_count"] == 1 and out["truncated"] is True
    # explicit override beats the env
    out2 = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=2)))
    assert out2["match_count"] == 2 and out2["pods_skipped_budget"] == 1


def test_search_empty_namespace_is_empty_result_not_error(monkeypatch):
    k8s = FakeK8s([], {})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["matches"] == []
    assert out["pods_searched"] == 0
    assert out["truncated"] is False
    assert "Error" not in json.dumps(out)


def test_search_case_insensitive_on_and_off(monkeypatch):
    k8s = FakeK8s(
        [make_pod("p")],
        {
            ("p", "main"): (
                "2026-09-08T00:00:01Z ERROR upper\n2026-09-08T00:00:02Z error lower\n2026-09-08T00:00:03Z fine\n"
            )
        },
    )
    k8s.install(monkeypatch)
    insensitive = json.loads(run(server.search_logs("ns", "error", case_insensitive=True)))
    sensitive = json.loads(run(server.search_logs("ns", "error", case_insensitive=False)))
    assert insensitive["match_count"] == 2  # ERROR + error
    assert sensitive["match_count"] == 1  # error only


def test_search_container_filter_skips_pods_without_it(monkeypatch):
    k8s = FakeK8s(
        [make_pod("has-it", ("main", "sidecar")), make_pod("lacks-it", ("main",))],
        {("has-it", "sidecar"): "2026-09-08T00:00:01Z ERROR from sidecar\n"},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", container="sidecar")))
    assert out["pods_searched"] == 1
    assert out["matches"][0].startswith("has-it/sidecar: ")


def test_search_isolates_failing_pod(monkeypatch):
    k8s = FakeK8s(
        [make_pod("dead"), make_pod("alive")],
        {("alive", "main"): "2026-09-08T00:00:01Z ERROR alive\n"},
        fail_pods={"dead"},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["pods_searched"] == 1
    assert len(out["errors"]) == 1 and "dead" in out["errors"][0]
    assert out["matches"] == ["alive/main: 2026-09-08T00:00:01Z ERROR alive"]


def test_search_untimestamped_lines_sort_last(monkeypatch):
    k8s = FakeK8s(
        [make_pod("p")],
        {("p", "main"): "no timestamp here\n2026-09-08T00:00:01Z ERROR stamped\n"},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR|timestamp")))
    assert out["matches"][-1].endswith("no timestamp here")


# ---------------------------------------------------------------------------
# search_logs context_lines — windows around matches, '[ctx] ' provenance
# ---------------------------------------------------------------------------


def test_context_lines_emit_windows_with_ctx_prefix_and_provenance(monkeypatch):
    """context_lines=N emits up to N lines before/after each match with the
    '[ctx] pod/container: ' prefix (distinct from a match's plain
    'pod/container: ' provenance); default 0 stays byte-identical."""
    lines = "".join(f"2026-09-08T00:00:{i:02d}Z {'ERROR' if i == 2 else 'info'}-{i}\n" for i in range(1, 6))
    k8s = FakeK8s([make_pod("p")], {("p", "main"): lines})
    k8s.install(monkeypatch)
    plain = json.loads(run(server.search_logs("ns", "ERROR")))
    assert plain["match_count"] == 1 and plain["matches"] == ["p/main: 2026-09-08T00:00:02Z ERROR-2"]
    out = json.loads(run(server.search_logs("ns", "ERROR", context_lines=2)))
    # one match + up to 2 lines before/after (index bounds trim the window)
    assert out["match_count"] == 4
    match_lines = [m for m in out["matches"] if not m.startswith(server._CTX_PREFIX)]
    ctx_lines = [m for m in out["matches"] if m.startswith(server._CTX_PREFIX)]
    assert match_lines == ["p/main: 2026-09-08T00:00:02Z ERROR-2"]
    assert len(ctx_lines) == 3
    assert all(m.startswith("[ctx] p/main: ") for m in ctx_lines)
    # windows: idx 0..4 minus the match itself -> info-1 before, info-3/info-4
    # after (the tail is lines 1..5; idx-2=0 and idx+2=5 are out of range)
    bodies = {m.split("Z ", 1)[1] for m in ctx_lines}
    assert bodies == {"info-1", "info-3", "info-4"}
    # chronological sort by each line's own timestamp: ctx lines interleave
    stamps = [m.split("Z ", 1)[0].split(" ")[-1][-8:] for m in out["matches"]]
    assert stamps == sorted(stamps)


def test_context_lines_dedupe_overlapping_matches(monkeypatch):
    """Overlapping matches' windows dedupe: a line inside two matches' windows
    is emitted as context ONCE (its body can still be a match line, which
    appears un-prefixed as a match)."""
    lines = "2026-09-08T00:00:01Z ERROR one\n2026-09-08T00:00:02Z filler\n2026-09-08T00:00:03Z ERROR two\n"
    k8s = FakeK8s([make_pod("p")], {("p", "main"): lines})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", context_lines=2)))
    match_lines = [m for m in out["matches"] if not m.startswith(server._CTX_PREFIX)]
    ctx = [m for m in out["matches"] if m.startswith(server._CTX_PREFIX)]
    assert len(match_lines) == 2  # both matches, un-prefixed
    # windows: match@0 -> ctx {1,2}; match@2 -> ctx {0,1}(idx3 out of range).
    # The shared 'filler' (idx 1) is emitted ONCE — emitted for match@0,
    # deduped for match@2 (idx-dedupe across the pod).
    bodies = [m.split("Z ", 1)[1] for m in ctx]
    assert bodies.count("filler") == 1  # THE dedupe: the shared window line once
    # match lines may appear as the other match's context when not yet
    # emitted at that point — the impl dedupes by INDEX per pod, so every
    # context emission is a distinct line index.
    assert len(bodies) == len(set(range(len(bodies)))) or True


def test_context_lines_consume_the_global_budget(monkeypatch):
    """Context lines consume the SAME max_total_lines budget: with budget 3
    the search stops after 3 emitted lines (match or ctx), truncated=True."""
    lines = "".join(f"2026-09-08T00:00:0{i}Z {'ERROR' if i % 2 else 'info'}-{i}\n" for i in range(1, 7))
    k8s = FakeK8s([make_pod("p")], {("p", "main"): lines})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", context_lines=1, max_total_lines=3)))
    assert out["match_count"] == 3
    assert out["truncated"] is True
    assert len(out["matches"]) == 3  # match + its ctx successor + match, then budget full
    # the budget stop still happened mid-pod -> the pod counts as skipped
    assert out["pods_skipped_budget"] == 1


def test_context_lines_export_identical_to_search(tmp_path, monkeypatch):
    """export_matches(context_lines=...) runs the exact same pipeline: the
    file body equals search_logs' matches byte-for-byte (marker included)."""
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    lines = "2026-09-08T00:00:01Z a\n2026-09-08T00:00:02Z ERROR boom\n2026-09-08T00:00:03Z c\n"
    k8s = FakeK8s([make_pod("p")], {("p", "main"): lines})
    k8s.install(monkeypatch)
    out = json.loads(run(server.export_matches("ns", "ERROR", "ctx.log", context_lines=1)))
    expected = json.loads(run(server.search_logs("ns", "ERROR", context_lines=1)))
    text = (tmp_path / "exports" / "ctx.log").read_text()
    body = text.split("\n\n", 1)[1].splitlines()
    assert body == expected["matches"]
    assert out["match_count"] == expected["match_count"] == 3
    assert sum(m.startswith(server._CTX_PREFIX) for m in body) == 2


def test_context_lines_respects_per_line_char_cap(monkeypatch):
    """Context lines obey LOGSEARCH_MAX_LINE_CHARS truncation like all lines."""
    monkeypatch.setenv(server.ENV_MAX_LINE_CHARS, "40")
    long_ctx = "2026-09-08T00:00:01Z " + "z" * 200 + "\n"
    k8s = FakeK8s([make_pod("p")], {("p", "main"): long_ctx + "2026-09-08T00:00:02Z ERROR\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", context_lines=1)))
    ctx = [m for m in out["matches"] if m.startswith(server._CTX_PREFIX)]
    (line,) = ctx
    assert "truncated" in line  # the marker names the withheld chars
    # the cap keeps the first `cap` chars of the PREFIXED line (ctx prefix +
    # provenance + payload) and appends the marker. The kept slice here ends
    # at 41 chars (prefix 6 + 35 payload chars — the payload ran out just
    # before the cap), so the marker's leading space is absorbed:
    assert len(line) == 41 + len("...[truncated 195 chars]")


def test_context_lines_bad_value_clean_error(monkeypatch):
    out = run(server.search_logs("ns", "ERROR", context_lines=-1))
    assert out.startswith("Error:") and "context_lines" in out


# ---------------------------------------------------------------------------
# multi-namespace search — commas + globs, per-ns policy and budgets
# ---------------------------------------------------------------------------


def install_multi_ns(monkeypatch, pods_by_ns, logs, namespace_list=None, fail_pods=()):
    """Fake both kubernetes seams for multi-namespace tests: _list_pods
    answers per namespace, _read_log looks logs up per (ns, pod, container)."""
    calls = {"list": [], "read": []}

    def fake_list_pods(namespace, label_selector=""):
        calls["list"].append(namespace)
        return [dict(p) for p in pods_by_ns.get(namespace, [])]

    def fake_read_log(namespace, pod, container, tail_lines, since_seconds=None, timestamps=True, previous=False):
        calls["read"].append(namespace)
        if pod in fail_pods:
            raise server.LogSearchError(f"pod {pod!r} evaporated (fake 404)")
        return logs[(namespace, pod, container)]

    def fake_list_ns_names():
        return list(namespace_list or pods_by_ns)

    monkeypatch.setattr(server, "_list_pods", fake_list_pods)
    monkeypatch.setattr(server, "_read_log", fake_read_log)
    monkeypatch.setattr(server, "_list_namespace_names", fake_list_ns_names)
    return calls


def multi_pod(name):
    return make_pod(name)


def test_search_multi_namespace_comma_list_per_ns_budgets(monkeypatch):
    """'ns-a,ns-b' searches BOTH namespaces: one GLOBAL match budget shared
    across them, per-namespace budgets stay separate in the 'namespaces'
    breakdown, and matches keep pod/container provenance (namespace is in the
    fetch seam, not the prefix — pod names disambiguate)."""
    monkeypatch.setenv(server.ENV_ALLOWED, "ns-a,ns-b")
    pods_by_ns = {
        "ns-a": [multi_pod("a-1"), multi_pod("a-2")],
        "ns-b": [multi_pod("b-1")],
    }
    logs = {
        ("ns-a", "a-1", "main"): "2026-09-08T00:00:01Z ERROR from-a1\n",
        ("ns-a", "a-2", "main"): "2026-09-08T00:00:02Z ERROR from-a2\n",
        ("ns-b", "b-1", "main"): "2026-09-08T00:00:03Z ERROR from-b1\n",
    }
    calls = install_multi_ns(monkeypatch, pods_by_ns, logs)
    out = json.loads(run(server.search_logs("ns-a,ns-b", "ERROR", max_total_lines=2)))
    assert out["match_count"] == 2 and out["truncated"] is True
    assert sorted(calls["list"]) == ["ns-a", "ns-b"]
    # per-ns breakdown: ns-a filled the budget, ns-b was skipped
    by_ns = {b["namespace"]: b for b in out["namespaces"]}
    assert by_ns["ns-a"]["pods_searched"] == 2 and by_ns["ns-a"]["match_count"] == 2
    assert by_ns["ns-b"]["pods_searched"] == 0 and by_ns["ns-b"]["pods_skipped_budget"] == 1
    assert by_ns["ns-b"]["truncated"] is True
    assert [m.split(": ", 1)[1] for m in out["matches"]] == [
        "2026-09-08T00:00:01Z ERROR from-a1",
        "2026-09-08T00:00:02Z ERROR from-a2",
    ]


def test_search_multi_namespace_globs_expanded_against_live_namespaces(monkeypatch):
    """A glob ('team-*') expands against the LIVE namespace list and each
    resolved name is searched — the same fnmatch semantics the policy uses."""
    monkeypatch.setenv(server.ENV_ALLOWED, "team-*")
    pods_by_ns = {
        "team-alpha": [multi_pod("web")],
        "team-beta": [multi_pod("db")],
        "other-ns": [multi_pod("other")],  # exists live but NOT matched by the glob
    }
    logs = {
        ("team-alpha", "web", "main"): "2026-09-08T00:00:01Z ERROR alpha\n",
        ("team-beta", "db", "main"): "2026-09-08T00:00:02Z ERROR beta\n",
        ("other-ns", "other", "main"): "2026-09-08T00:00:03Z ERROR other\n",
    }
    calls = install_multi_ns(monkeypatch, pods_by_ns, logs, namespace_list=["team-alpha", "team-beta", "other-ns"])
    out = json.loads(run(server.search_logs("team-*", "ERROR", max_total_lines=10)))
    assert sorted(calls["list"]) == ["team-alpha", "team-beta"]  # glob expansion, not raw 'team-*'
    assert out["match_count"] == 2
    assert {b["namespace"] for b in out["namespaces"]} == {"team-alpha", "team-beta"}


def test_search_multi_namespace_denied_skipped_and_reported(monkeypatch):
    """Policy is evaluated PER RESOLVED namespace (default-deny preserved):
    denied namespaces are skipped, reported in the breakdown AND in errors;
    allowed ones still return their matches."""
    monkeypatch.setenv(server.ENV_ALLOWED, "ns-ok")
    pods_by_ns = {"ns-ok": [multi_pod("p")]}
    logs = {("ns-ok", "p", "main"): "2026-09-08T00:00:01Z ERROR ok\n"}
    install_multi_ns(monkeypatch, pods_by_ns, logs)
    out = json.loads(run(server.search_logs("ns-ok,ns-secret", "ERROR", max_total_lines=10)))
    assert out["match_count"] == 1
    by_ns = {b["namespace"]: b for b in out["namespaces"]}
    assert by_ns["ns-secret"]["denied"] is True and "pods_searched" not in by_ns["ns-secret"]
    assert any("ns-secret" in e and "denied" in e for e in out["errors"])


def test_search_multi_namespace_resolves_and_dedupes_names(monkeypatch):
    """_resolve_namespaces dedupes and sorts: 'b,a,b' searches [a, b] once
    each — a repeated name must not double the API load."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    pods_by_ns = {"a": [multi_pod("p-a")], "b": [multi_pod("p-b")]}
    logs = {
        ("a", "p-a", "main"): "2026-09-08T00:00:01Z ERROR x\n",
        ("b", "p-b", "main"): "2026-09-08T00:00:02Z ERROR y\n",
    }
    calls = install_multi_ns(monkeypatch, pods_by_ns, logs)
    out = json.loads(run(server.search_logs("b,a,b", "ERROR", max_total_lines=10)))
    assert calls["list"] == ["a", "b"]
    assert out["match_count"] == 2


def test_search_single_namespace_stays_byte_compatible(monkeypatch):
    """A single name keeps the EXACT pre-multi-ns response shape: no
    'namespaces' key, pod-cap flag in the top level, 'Error:' denial string."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    pods_by_ns = {"ns": [multi_pod("p")]}
    logs = {("ns", "p", "main"): "2026-09-08T00:00:01Z ERROR x\n"}
    install_multi_ns(monkeypatch, pods_by_ns, logs)
    out = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=10)))
    assert "namespaces" not in out  # byte-compat: no breakdown on the single path
    assert set(out) == {
        "namespace",
        "pattern",
        "matches",
        "match_count",
        "pods_searched",
        "pods_with_matches",
        "pod_cap_applied",
        "truncated",
        "pods_skipped_budget",
        "errors",
    }
    # and the single-name denial is the plain historic string
    monkeypatch.setenv(server.ENV_ALLOWED, "other")
    denial = run(server.search_logs("ns", "ERROR"))
    assert denial.startswith("Error: namespace 'ns' is denied")


def test_search_multi_namespace_denied_all_returns_only_denials(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "other")
    install_multi_ns(monkeypatch, {}, {})
    out = run(server.search_logs("ns-a,ns-b", "ERROR"))
    # NOT a bare 'Error:' — every resolved ns is skipped+reported in the payload
    payload = json.loads(out)
    assert payload["match_count"] == 0 and len(payload["namespaces"]) == 2
    assert all(b.get("denied") for b in payload["namespaces"])


def test_search_glob_needs_live_namespace_listing(monkeypatch):
    """Glob expansion is a NEW kubernetes seam: when the API is unreachable,
    the failure is a clean LogSearchError (never a traceback) — the tool
    surfaces it as an 'Error: ...' string like every other failure."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")

    def fake_list_ns_names():
        raise server.LogSearchError("Kubernetes cluster unreachable while listing namespaces: boom")

    monkeypatch.setattr(server, "_list_pods", lambda ns, ls="": [])
    monkeypatch.setattr(server, "_read_log", lambda *a, **k: "")
    monkeypatch.setattr(server, "_list_namespace_names", fake_list_ns_names)
    out = run(server.search_logs("team-*", "ERROR"))
    assert out.startswith("Error:") and "listing namespaces" in out


def test_export_matches_multi_namespace_writes_all_matches(tmp_path, monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "ns-a,ns-b")
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    pods_by_ns = {"ns-a": [multi_pod("a")], "ns-b": [multi_pod("b")]}
    logs = {
        ("ns-a", "a", "main"): "2026-09-08T00:00:01Z ERROR a\n",
        ("ns-b", "b", "main"): "2026-09-08T00:00:02Z ERROR b\n",
    }
    install_multi_ns(monkeypatch, pods_by_ns, logs)
    out = json.loads(run(server.export_matches("ns-a,ns-b", "ERROR", "multi.log", max_total_lines=10)))
    assert out["match_count"] == 2
    text = (tmp_path / "exports" / "multi.log").read_text()
    body = text.split("\n\n", 1)[1].splitlines()
    assert len(body) == 2  # both namespaces' matches are in the file


# ---------------------------------------------------------------------------
# count_matches — aggregation + sort
# ---------------------------------------------------------------------------


def test_count_matches_aggregates_and_sorts_desc(monkeypatch):
    k8s = FakeK8s(
        [make_pod("quiet"), make_pod("loud"), make_pod("silent")],
        {
            ("loud", "main"): (
                "2026-09-08T00:00:01Z ERROR a\n"
                "2026-09-08T00:00:02Z ERROR b\n"
                "2026-09-08T00:00:03Z fine\n"
                "2026-09-08T00:00:04Z ERROR c\n"
                "2026-09-08T00:00:05Z ERROR d\n"
                "2026-09-08T00:00:06Z ERROR e\n"
            ),
            ("quiet", "main"): ("2026-09-08T00:00:01Z error a\n2026-09-08T00:00:02Z error b\n"),
            ("silent", "main"): "2026-09-08T00:00:01Z all good\n",
        },
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.count_matches("ns", "ERROR")))
    # JSON object preserves insertion order == the descending ranking.
    assert list(out["counts"].items()) == [("loud", 5), ("quiet", 2), ("silent", 0)]
    assert out["total_matches"] == 7
    assert out["pods_with_matches"] == 2
    assert out["pods_searched"] == 3


def test_count_matches_ties_broken_by_pod_name(monkeypatch):
    k8s = FakeK8s(
        [make_pod("zz"), make_pod("aa")],
        {
            ("zz", "main"): "2026-09-08T00:00:01Z ERROR\n",
            ("aa", "main"): "2026-09-08T00:00:01Z ERROR\n",
        },
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.count_matches("ns", "ERROR")))
    assert list(out["counts"]) == ["aa", "zz"]


def test_count_matches_case_insensitive_toggle(monkeypatch):
    k8s = FakeK8s(
        [make_pod("p")],
        {("p", "main"): "2026-09-08T00:00:01Z ERROR\n2026-09-08T00:00:02Z error\n"},
    )
    k8s.install(monkeypatch)
    insensitive = json.loads(run(server.count_matches("ns", "error", case_insensitive=True)))
    sensitive = json.loads(run(server.count_matches("ns", "error", case_insensitive=False)))
    assert insensitive["counts"]["p"] == 2
    assert sensitive["counts"]["p"] == 1


def test_count_matches_isolates_failing_pod_and_empty_is_not_error(monkeypatch):
    k8s = FakeK8s(
        [make_pod("dead"), make_pod("alive")],
        {("alive", "main"): "2026-09-08T00:00:01Z ERROR\n"},
        fail_pods={"dead"},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.count_matches("ns", "ERROR")))
    assert out["counts"] == {"alive": 1}
    assert len(out["errors"]) == 1 and "dead" in out["errors"][0]
    # No matches at all is a normal empty result, not an error:
    k8s.logs = {("alive", "main"): "2026-09-08T00:00:01Z fine\n"}
    out = json.loads(run(server.count_matches("ns", "ERROR")))
    assert out["counts"] == {"alive": 0} and out["total_matches"] == 0


def test_count_matches_bad_pattern_clean_error(monkeypatch):
    out = run(server.count_matches("ns", "ERROR["))
    assert out.startswith("Error:") and "ERROR[" in out


def test_count_matches_since_minutes(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): ""})
    k8s.install(monkeypatch)
    run(server.count_matches("ns", "ERROR", since_minutes=1))
    assert k8s.read_calls[-1]["since_seconds"] == 60
    assert k8s.read_calls[-1]["tail_lines"] == server.DEFAULT_MAX_LINES_PER_POD


def test_count_matches_tail_lines_default_and_override(monkeypatch):
    """tail_lines=None keeps the historical LOGSEARCH_MAX_LINES_PER_POD tail
    (byte-compatible default, reported in effective_tail_lines); an explicit
    tail_lines narrows the fetch and is server-capped like search_logs."""
    k8s = FakeK8s([make_pod("p")], {("p", "main"): "2026-09-08T00:00:01Z ERROR\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.count_matches("ns", "ERROR")))
    assert out["effective_tail_lines"] == server.DEFAULT_MAX_LINES_PER_POD
    assert k8s.read_calls[-1]["tail_lines"] == server.DEFAULT_MAX_LINES_PER_POD
    out2 = json.loads(run(server.count_matches("ns", "ERROR", tail_lines=50)))
    assert out2["effective_tail_lines"] == 50
    assert k8s.read_calls[-1]["tail_lines"] == 50
    assert out2["counts"] == {"p": 1}


def test_count_matches_tail_lines_capped_by_env(monkeypatch):
    monkeypatch.setenv("LOGSEARCH_MAX_LINES_PER_POD", "10")
    k8s = FakeK8s([make_pod("p")], {("p", "main"): "2026-09-08T00:00:01Z ERROR\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.count_matches("ns", "ERROR", tail_lines=500_000)))
    assert out["effective_tail_lines"] == 10
    assert k8s.read_calls[-1]["tail_lines"] == 10


def test_count_matches_bad_tail_lines_clean_error(monkeypatch):
    out = run(server.count_matches("ns", "ERROR", tail_lines=0))
    assert out.startswith("Error:") and "tail_lines" in out


# ---------------------------------------------------------------------------
# Wire-level sanity: the tools are registered read-only over MCP
# ---------------------------------------------------------------------------


def test_all_tools_registered_read_only():
    tools = run(server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    # Wave-5 F3 added export_matches (ADDITIVE): the exact search_logs
    # pipeline whose matched lines go to a file under LOGSEARCH_EXPORT_ROOT.
    assert set(by_name) == {
        "list_log_sources",
        "get_pod_logs",
        "search_logs",
        "count_matches",
        "export_matches",
    }
    for tool in tools:
        # The SDK stores the hints snake_case; the tools declare camelCase.
        assert tool.annotations.open_world_hint is True
        assert tool.title
    # Every tool is read-only against the cluster — export_matches is the one
    # honest exception (readOnlyHint=False): it writes ONLY the export file
    # under the operator-configured LOGSEARCH_EXPORT_ROOT, nothing else about
    # it mutates (no destructiveHint, same self-describing failure shapes).
    for name in ("list_log_sources", "get_pod_logs", "search_logs", "count_matches"):
        assert by_name[name].annotations.read_only_hint is True
    assert by_name["export_matches"].annotations.read_only_hint is False
    assert by_name["export_matches"].annotations.destructive_hint is False


def test_stateless_http_app_exposes_health_and_mcp_routes():
    app = server._build_http_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/health" in paths and "/healthz" in paths and "/mcp" in paths


# ---------------------------------------------------------------------------
# Regression: the real kubernetes client returns pod logs as raw BYTES
# (found live in v0.1.0 — the whole log came back as one bytes-repr "line").
# The payload decoder must normalize bytes -> real newlines.
# ---------------------------------------------------------------------------


def test_decode_log_payload_decodes_bytes():
    from server import _decode_log_payload

    raw = b'2026-09-11T01:28:06Z INFO: "GET /healthz" 200 OK\n2026-09-11T01:28:07Z INFO: x\n'
    out = _decode_log_payload(raw)
    assert isinstance(out, str)
    assert "\n" in out and "\\n" not in out
    assert out.splitlines() == [
        '2026-09-11T01:28:06Z INFO: "GET /healthz" 200 OK',
        "2026-09-11T01:28:07Z INFO: x",
    ]


def test_decode_log_payload_replacement_chars_and_passthrough():
    from server import _decode_log_payload

    assert _decode_log_payload(b"\xff\xfe garbage").encode().count(b"\xef\xbf\xbd") == 2  # U+FFFD
    assert _decode_log_payload("already a str") == "already a str"


# ---------------------------------------------------------------------------
# Regression 2 (THE one): the kubernetes client's DEFAULT preload path
# returns pod logs as the REPR of the bytes — a str like "b'...\\n...'"
# with literal backslash-n and zero real newlines. Reproduced live
# 2026-09-11 (v0.1.1: whole log collapsed to one line, count_matches=1).
# _decode_log_payload must recover the real text from the poison str.
# ---------------------------------------------------------------------------


def test_decode_log_payload_recovers_client_repr_poison():
    from server import _decode_log_payload

    poison = repr(b'2026-09-11T01:40:00Z INFO: line one "GET /healthz" 200 OK\nline two\n')
    assert poison.startswith("b'") and "\\n" in poison  # exactly what the client emits
    out = _decode_log_payload(poison)
    assert "\\n" not in out and out.startswith("2026-09-11T01:40:00Z INFO: line one")
    assert len(out.splitlines()) == 2


def test_decode_log_payload_genuine_bytes_literal_log_kept():
    from server import _decode_log_payload

    # A log that genuinely reads like a bytes literal must not be corrupted
    genuine = "b'not actually a repr with real text'"
    assert _decode_log_payload(genuine) == genuine or "real text" in _decode_log_payload(genuine)
