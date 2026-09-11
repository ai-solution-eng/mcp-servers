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
    shell can never leak policy/caps into these results."""
    for name in (
        server.ENV_ALLOWED,
        server.ENV_BLOCKED,
        "LOGSEARCH_MAX_PODS",
        "LOGSEARCH_MAX_LINES_PER_POD",
        "LOGSEARCH_MAX_TOTAL_LINES",
    ):
        monkeypatch.delenv(name, raising=False)


def make_pod(name, containers=("main",), restarts=0, started="2026-09-08T00:00:00Z"):
    return {"name": name, "containers": list(containers), "restarts": restarts, "started": started}


class FakeK8s:
    """In-memory stand-in for the two kubernetes seams, with call recording."""

    def __init__(self, pods, logs, fail_pods=()):
        self.pods = pods                      # list of pod dicts
        self.logs = logs                      # {(pod, container): log text}
        self.fail_pods = set(fail_pods)       # pod names whose read raises
        self.list_calls = []
        self.read_calls = []

    def install(self, monkeypatch):
        k8s = self

        def fake_list_pods(namespace, label_selector=""):
            k8s.list_calls.append((namespace, label_selector))
            return [dict(p) for p in k8s.pods]

        def fake_read_log(
            namespace, pod, container, tail_lines, since_seconds=None,
            timestamps=True, previous=False,
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


def test_policy_empty_allowed_means_all_namespaces(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "")
    monkeypatch.setenv(server.ENV_BLOCKED, "")
    assert server._namespace_allowed("default")
    assert server._namespace_allowed("kube-system")
    assert server._namespace_allowed("team-a-prod-eu-1")
    assert server._namespace_allowed("anything-goes")


@pytest.mark.parametrize(
    "allowed,blocked,ns,expected",
    [
        # glob allow-list
        ("team-a,prod-*", "", "team-a", True),
        ("team-a,prod-*", "", "prod-eu-1", True),
        ("team-a,prod-*", "", "prod", False),       # 'prod-*' needs the dash
        ("team-a,prod-*", "", "team-a2", False),    # prefix is not a match
        ("team-a,prod-*", "", "dev-eu-1", False),
        ("team-a,prod-*", "", "", False),           # non-empty list denies ''
        ("app", "", "app", True),                   # exact literal glob
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
    ],
)
def test_policy_matrix(monkeypatch, allowed, blocked, ns, expected):
    monkeypatch.setenv(server.ENV_ALLOWED, allowed)
    monkeypatch.setenv(server.ENV_BLOCKED, blocked)
    assert server._namespace_allowed(ns) is expected


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
            make_pod("b-pod", containers=("api", "sidecar"), restarts=3,
                     started="2026-09-07T00:00:00Z"),
            make_pod("a-pod", containers=("worker",), restarts=0,
                     started="2026-09-08T00:00:00Z"),
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
    assert out["container"] == "web"          # first container as default
    assert out["lines_returned"] == 2
    assert out["previous"] is False
    assert out["truncated"] is False
    assert out["log"].splitlines()[0].endswith("line one")


def test_get_pod_logs_previous_container_passthrough(monkeypatch):
    k8s = FakeK8s([make_pod("crashy", ("app",))], {("crashy", "app"): "2026-09-08T00:00:01Z panic: oom\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.get_pod_logs("ns", "crashy", previous=True, tail_lines=50)))
    call = k8s.read_calls[-1]
    assert call["previous"] is True                 # THE triage passthrough
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
    assert len(out["log"]) <= 40      # cut at a newline before the cap
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
            ("api-a", "api"): (
                "2026-09-08T00:00:10Z ERROR late\n"
                "2026-09-08T00:00:01Z ERROR early\n"
            ),
            ("api-b", "worker"): "2026-09-08T00:00:05Z ERROR middle\n",
        },
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert [m.split("Z ", 1)[1] for m in out["matches"]] == [
        "ERROR early", "ERROR middle", "ERROR late",
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
    assert k8s.read_calls[-1]["timestamps"] is True   # sort depends on it


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


def test_search_max_total_lines_truncation_flag_keeps_most_recent(monkeypatch):
    k8s = FakeK8s(
        [make_pod("p")],
        {("p", "main"): "".join(f"2026-09-08T00:00:0{i}Z hit {i}\n" for i in range(1, 6))},
    )
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "hit", max_total_lines=3)))
    assert out["truncated"] is True
    assert out["match_count"] == 3
    # Cap keeps the MOST RECENT matches (chronological order preserved):
    assert "hit 1" not in " ".join(out["matches"])
    assert "hit 5" in out["matches"][-1]


def test_search_no_truncation_flag_when_under_cap(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): "2026-09-08T00:00:01Z hit\n"})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "hit")))
    assert out["truncated"] is False
    assert out["match_count"] == 1


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
                "2026-09-08T00:00:01Z ERROR upper\n"
                "2026-09-08T00:00:02Z error lower\n"
                "2026-09-08T00:00:03Z fine\n"
            )
        },
    )
    k8s.install(monkeypatch)
    insensitive = json.loads(run(server.search_logs("ns", "error", case_insensitive=True)))
    sensitive = json.loads(run(server.search_logs("ns", "error", case_insensitive=False)))
    assert insensitive["match_count"] == 2   # ERROR + error
    assert sensitive["match_count"] == 1     # error only


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
            ("quiet", "main"): (
                "2026-09-08T00:00:01Z error a\n"
                "2026-09-08T00:00:02Z error b\n"
            ),
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


# ---------------------------------------------------------------------------
# Wire-level sanity: the tools are registered read-only over MCP
# ---------------------------------------------------------------------------


def test_all_tools_registered_read_only():
    tools = run(server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"list_log_sources", "get_pod_logs", "search_logs", "count_matches"}
    for tool in tools:
        # The SDK stores the hints snake_case; the tools declare camelCase.
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is True
        assert tool.title


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

    assert _decode_log_payload(b"\xff\xfe garbage") .encode().count(b"\xef\xbf\xbd") == 2  # U+FFFD
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
