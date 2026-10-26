"""Wave-1 hardening tests for the LogSearch MCP server (no cluster, no network).

Covers the four A5 hardening fixes on top of the baseline suite:

1. ReDoS guard — catastrophic-backtracking patterns are REFUSED by the
   compile-time screen in milliseconds (never a GIL-holding backtrack), while
   every realistic log-search pattern still compiles. The optional re2 engine
   path is exercised with a fake module (re2 is not installed here and the
   code must not install anything).
2. Caps enforced early — the global line budget stops the fan-out mid-flight
   (pods are never fetched once the budget is full) and each output line is
   char-capped with an explicit truncation marker.
3. Namespace default-deny (fleet decision D8) — an empty allowlist denies ALL
   namespaces on every tool; LOGSEARCH_EMPTY_ALLOWS_ALL=1 restores the pre-D8
   open behavior explicitly.
4. Parallel fan-out — asyncio.gather under a bounded semaphore; results are
   byte-identical (same JSON string) whether the fan-out ran with
   concurrency 1 or concurrency 8.

Run:  cd mcp_servers/logsearch_mcp && SQLhandler/.venv312/bin/python -m pytest tests/ -v
"""

import asyncio
import json
import re
import time

import pytest

import server
import webui

# ---------------------------------------------------------------------------
# Fixtures & fakes (same shape as tests/test_logsearch.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    """Clean LOGSEARCH_* environment; NO escape hatch (tests here assert the
    D8 default-deny behavior explicitly and set what they need)."""
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


def make_pod(name, containers=("main",), restarts=0, started="2026-09-08T00:00:00Z"):
    return {"name": name, "containers": list(containers), "restarts": restarts, "started": started}


class FakeK8s:
    """In-memory stand-in for the two kubernetes seams, with call recording
    and an optional per-fetch delay (to make concurrency observable)."""

    def __init__(self, pods, logs, fail_pods=(), delay=0.0):
        self.pods = pods
        self.logs = logs
        self.fail_pods = set(fail_pods)
        self.delay = delay
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
            k8s.read_calls.append({"pod": pod, "container": container, "tail_lines": tail_lines})
            if k8s.delay:
                time.sleep(k8s.delay)
            if pod in k8s.fail_pods:
                raise server.LogSearchError(f"pod {pod!r} evaporated mid-search (fake 404)")
            return k8s.logs[(pod, container)]

        monkeypatch.setattr(server, "_list_pods", fake_list_pods)
        monkeypatch.setattr(server, "_read_log", fake_read_log)


def run(coro):
    return asyncio.run(coro)


def pod_log(pod, ts, text):
    return f"2026-09-08T00:00:{ts:02d}Z {text}\n"


# ---------------------------------------------------------------------------
# 1 · ReDoS guard: evil regexes refused FAST; good ones still compile
# ---------------------------------------------------------------------------


EVIL_PATTERNS = [
    "(a+)+",  # the classic
    "(a+)+$",  # anchored classic
    "(a*)*b",
    "(.*)*x",
    "(a|aa)+",  # overlapping alternation (parser factors to an empty branch)
    "(a?)+",  # empty-able branch under an unbounded quantifier
    "(x+x+)+y",
    r"(\w+\s)*x",  # variable group under unbounded quantifier
    r"(\d+\.)*x",
    "(a+){20}b",  # wide bounded quantifier over a variable one
    r"([0-9]{1,2}){3,}",
    "(x|)+",  # empty alternative under a quantifier
    "(ERROR|Error)+",  # overlapping branches UNDER IGNORECASE (the tool default)
    "((a+)b)+c",
]
EVIL_PATTERNS_CASE_SENSITIVE = [
    "(a+)+",
    "(a*)*b",
    "(a|aa)+",
    "(a?)+",
    "(x|)+",
    "(a+){20}b",
]


@pytest.mark.parametrize("pattern", EVIL_PATTERNS)
def test_evil_regex_rejected_fast_by_the_screen(pattern, monkeypatch):
    """Every catastrophic shape is refused by the compile-time screen in
    well under a second of wall time — a hang here IS the vulnerability."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    t0 = time.perf_counter()
    out = run(server.search_logs("ns", pattern))
    wall = time.perf_counter() - t0
    assert out.startswith("Error:"), out
    assert "unsafe regex" in out and "rejected" in out
    assert repr(pattern) in out
    assert wall < 1.0, f"screen took {wall:.3f}s for {pattern!r} — that is the bug"


@pytest.mark.parametrize("pattern", EVIL_PATTERNS_CASE_SENSITIVE)
def test_screen_rejects_case_sensitive_evil_too(pattern, monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    out = run(server.count_matches("ns", pattern))
    assert out.startswith("Error:") and "unsafe regex" in out


def test_evil_pod_regex_rejected_fast(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    t0 = time.perf_counter()
    out = run(server.search_logs("ns", "fine", pod_regex="(a+)+"))
    wall = time.perf_counter() - t0
    assert out.startswith("Error:") and "pod_regex" in out and "unsafe regex" in out
    assert wall < 1.0


def test_screen_rejects_overlong_pattern(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    long_pattern = "a" * 600
    out = run(server.search_logs("ns", long_pattern))
    assert "LOGSEARCH_MAX_REGEX_CHARS" in out
    monkeypatch.setenv(server.ENV_MAX_REGEX_CHARS, "0")  # 0 = cap disabled
    k8s = FakeK8s([make_pod("p")], {("p", "main"): pod_log("p", 1, "ERROR x")})
    k8s.install(monkeypatch)
    out2 = json.loads(run(server.search_logs("ns", long_pattern)))
    assert out2["errors"] == [] and out2["match_count"] == 0  # ran instead of refusing


def test_screen_reject_message_suggests_the_rewrite():
    with pytest.raises(server.LogSearchError) as ei:
        server._compile_regex("(a+)+")
    msg = str(ei.value)
    assert "rejected" in msg and "ReDoS" in msg
    assert "(?>" in msg or "atomic" in msg or "rewrite" in msg.lower()


REALISTIC_PATTERNS = [
    "ERROR|Traceback",
    "Traceback \\(most recent call last\\)",
    "^api-1$",
    "kube-*",
    "panic: .*",
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
    r"\d+(?:\.\d+)*",  # anchored nesting: '.' pins it, \d can't eat '.'
    r"(?:\d{1,3}\.){3}\d{1,3}",  # small bounded nesting (IP octets)
    r"(?:[a-f0-9]{2}){8}",  # fixed inner bound
    "OOM|CrashLoop|BackOff",
    r"ERROR:\s+\w+",
    "(?:INFO|WARN|ERROR):\\s+.*",
    r"192\.168\.\d+\.\d+",
    "[a-z]+(?:-[a-z]+)*",
    r"(?:\.\d+)?",
    r"(\w|\d)+",  # sre merges the branches -> single class, linear
    "(?:ab|cd)+",  # disjoint literal branches
    r"(\.|,)+",  # disjoint single chars
]


@pytest.mark.parametrize("pattern", REALISTIC_PATTERNS)
def test_realistic_log_patterns_still_compile(pattern):
    """The screen must not break day-to-day log-search regexes."""
    compiled = server._compile_regex(pattern, flags=re.IGNORECASE)
    assert compiled is not None


def test_genuinely_invalid_regex_keeps_clean_error(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    out = run(server.search_logs("ns", "([unclosed"))
    assert out.startswith("Error:") and "invalid regex" in out and "([unclosed" in out


class FakeRe2:
    """Minimal re2 stand-in: module-shaped, records compile calls, delegates
    matching to re (its semantics are close enough for a wiring test)."""

    error = re.error

    def __init__(self):
        self.compiled = []

    def compile(self, pattern, flags=0):
        self.compiled.append((pattern, flags))
        return re.compile(pattern, flags)


def test_re2_preferred_for_matching_when_importable(monkeypatch):
    fake = FakeRe2()
    monkeypatch.setattr(server, "_re2", fake)
    rx = server._compile_regex("ERROR", flags=re.IGNORECASE)
    assert fake.compiled == [("ERROR", re.IGNORECASE)]  # re2 got the compile
    assert rx.search("x ERROR x")


def test_falls_back_to_re_when_re2_refuses_syntax(monkeypatch):
    class PickyRe2(FakeRe2):
        def compile(self, pattern, flags=0):
            if "(" in pattern:
                raise re.error("re2: not supported")
            return super().compile(pattern, flags)

    fake = PickyRe2()
    monkeypatch.setattr(server, "_re2", fake)
    rx = server._compile_regex("(ERROR|WARN)")  # backref-style syntax re2 refuses
    assert rx.search("a WARN b") and not fake.compiled  # fell back to re


def test_re2_absent_by_default():
    assert server._re2 is None  # optional dep: nothing installed it


# ---------------------------------------------------------------------------
# 2 · Caps enforced early + per-line char cap
# ---------------------------------------------------------------------------


def test_line_cap_truncates_with_marker(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    long_line = "2026-09-08T00:00:01Z ERROR " + "x" * 5000
    k8s = FakeK8s([make_pod("p")], {("p", "main"): long_line})
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    (line,) = out["matches"]
    full = "p/main: " + long_line  # the cap applies to the provenance-prefixed line
    marker = server._LINE_TRUNCATION_MARKER.format(len(full) - server.DEFAULT_MAX_LINE_CHARS)
    assert line.endswith(marker)
    assert len(line) == server.DEFAULT_MAX_LINE_CHARS + len(marker)
    assert line.startswith("p/main: 2026-09-08")  # provenance prefix survives


def test_line_cap_env_respected_and_disableable(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    long_line = "2026-09-08T00:00:01Z ERROR " + "y" * 500
    k8s = FakeK8s([make_pod("p")], {("p", "main"): long_line})
    k8s.install(monkeypatch)
    monkeypatch.setenv(server.ENV_MAX_LINE_CHARS, "40")
    (line,) = json.loads(run(server.search_logs("ns", "ERROR")))["matches"]
    assert len(line) == 40 + len(server._LINE_TRUNCATION_MARKER.format(len(long_line) - 40))
    monkeypatch.setenv(server.ENV_MAX_LINE_CHARS, "0")  # disabled
    (line,) = json.loads(run(server.search_logs("ns", "ERROR")))["matches"]
    assert "truncated" not in line and len(line) == len(long_line.rstrip("\n")) + len("p/main: ")


def test_line_cap_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(server.ENV_MAX_LINE_CHARS, "not-a-number")
    assert server._max_line_chars() == server.DEFAULT_MAX_LINE_CHARS
    monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, "junk")
    assert server._fetch_concurrency() == server.DEFAULT_FETCH_CONCURRENCY


def test_budget_full_never_schedules_the_next_wave(monkeypatch):
    """The pull stops BEFORE the fetch: with waves of 2 pods and a budget of 2,
    wave 1 fills the budget and pods 3..6 are never listed-for-read."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, "2")
    pods = [make_pod(f"p{i}") for i in range(6)]
    logs = {(f"p{i}", "main"): pod_log(f"p{i}", i, "ERROR hit") for i in range(6)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=2)))
    assert len(k8s.read_calls) == 2  # wave 1 only — the pull stopped
    assert out["pods_searched"] == 2
    assert out["pods_skipped_budget"] == 4  # never scheduled
    assert out["truncated"] is True
    assert out["match_count"] == 2
    assert [m.split("Z ", 1)[1] for m in out["matches"]] == ["ERROR hit", "ERROR hit"]


def test_budget_full_discards_inflight_results_of_the_current_wave(monkeypatch):
    """A budget filled mid-wave stops the merge: in-flight results are not
    merged and their pods count as skipped; truncated stays honest. The pod
    straddling the stop point appears in BOTH counts — it was read (searched)
    and its remaining matches were dropped (skipped)."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, "8")
    pods = [make_pod(f"p{i}") for i in range(5)]
    logs = {(f"p{i}", "main"): pod_log(f"p{i}", i, "ERROR hit") for i in range(5)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=2)))
    assert out["match_count"] == 2
    assert out["pods_searched"] == 3  # p0, p1 merged + p2 straddled the stop
    assert out["pods_skipped_budget"] == 3  # p2's tail + p3, p4 discarded
    assert out["truncated"] is True


def test_budget_not_hit_reports_no_skip_and_no_truncation(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    pods = [make_pod("p"), make_pod("q")]
    logs = {
        ("p", "main"): pod_log("p", 1, "ERROR one"),
        ("q", "main"): "no match here\n",
    }
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=300)))
    assert out["truncated"] is False and out["pods_skipped_budget"] == 0
    assert out["pods_searched"] == 2 and out["match_count"] == 1


def test_exact_budget_no_leftover_input_is_not_truncated(monkeypatch):
    """matches == budget with nothing left over behaves like the old slice:
    the cap did not bite, so truncated stays false."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    pods = [make_pod("p")]
    logs = {("p", "main"): pod_log("p", 1, "ERROR a") + pod_log("p", 2, "ERROR b")}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=2)))
    assert out["match_count"] == 2 and out["truncated"] is False


# ---------------------------------------------------------------------------
# 3 · Namespace default-deny (D8) + env-restore, at TOOL level
# ---------------------------------------------------------------------------


def test_empty_allowlist_denies_every_tool(monkeypatch):
    """D8: with no allowlist and no escape hatch, every tool answers the
    self-describing denial — never data."""
    for tool_call in (
        lambda: server.list_log_sources("ns"),
        lambda: server.get_pod_logs("ns", "p"),
        lambda: server.search_logs("ns", "x"),
        lambda: server.count_matches("ns", "x"),
    ):
        out = run(tool_call())
        assert out.startswith("Error:"), out
        assert server.ENV_ALLOWED in out and server.ENV_BLOCKED in out
        assert server.ENV_EMPTY_ALLOWS_ALL in out


def test_explicit_allowlist_still_works_under_default_deny(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "team-a")
    k8s = FakeK8s([make_pod("p")], {("p", "main"): pod_log("p", 1, "ERROR x")})
    k8s.install(monkeypatch)
    assert json.loads(run(server.list_log_sources("team-a")))["pod_count"] == 1
    assert run(server.search_logs("team-b", "x")).startswith("Error:")
    # and a wildcard allowlist is still expressible
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    assert run(server.search_logs("anything", "x")).startswith("Error:") is False


def test_env_restore_reopens_all_tools(monkeypatch):
    """LOGSEARCH_EMPTY_ALLOWS_ALL=1 restores the pre-D8 open default exactly:
    empty allowlist -> every namespace searchable again."""
    monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, "1")
    k8s = FakeK8s([make_pod("p")], {("p", "main"): pod_log("p", 1, "ERROR x")})
    k8s.install(monkeypatch)
    assert json.loads(run(server.list_log_sources("ns")))["pod_count"] == 1
    assert json.loads(run(server.search_logs("ns", "ERROR")))["match_count"] == 1
    assert json.loads(run(server.count_matches("ns", "ERROR")))["counts"]["p"] == 1
    assert json.loads(run(server.get_pod_logs("ns", "p")))["lines_returned"] == 1


def test_webui_maps_denied_and_unsafe_regex_to_4xx(monkeypatch):
    monkeypatch.delenv(server.ENV_EMPTY_ALLOWS_ALL, raising=False)  # default-deny
    resp = webui._tool_response(run(server.search_logs("ns", "x")))
    assert resp.status_code == 403
    monkeypatch.setenv(server.ENV_ALLOWED, "*")  # past the policy: now the regex screen
    resp = webui._tool_response(run(server.search_logs("ns", "(a+)+")))
    assert resp.status_code == 400
    assert "unsafe regex" in json.loads(resp.body)["error"]


def test_webui_status_reports_empty_allows_all(monkeypatch):
    routes = webui.build_ui_routes()
    status = next(r for r in routes if getattr(r, "path", None) == "/api/status")
    data = json.loads(asyncio.run(status.endpoint(type("R", (), {"query_params": {}})())).body)
    assert data["policy"]["empty_allows_all"] is False  # default-deny on clean env
    assert data["caps"]["max_line_chars"] == server.DEFAULT_MAX_LINE_CHARS
    assert data["caps"]["fetch_concurrency"] == server.DEFAULT_FETCH_CONCURRENCY
    assert data["caps"]["max_regex_chars"] == server.DEFAULT_MAX_REGEX_CHARS
    monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, "1")
    data = json.loads(asyncio.run(status.endpoint(type("R", (), {"query_params": {}})())).body)
    assert data["policy"]["empty_allows_all"] is True


# ---------------------------------------------------------------------------
# 4 · Parallel fan-out: bounded semaphore, byte-identical results
# ---------------------------------------------------------------------------


def build_mock_fleet(pod_count=24):
    """A deterministic mocked pod set: interleaved timestamps, several matches
    per pod, a couple of unreadable pods, and one dead pod — enough shape for
    ordering, skipping, and error isolation to be observable."""
    pods, logs = [], {}
    for i in range(pod_count):
        name = f"app-{i:02d}"
        pods.append(make_pod(name, containers=("main",) if i % 5 else ("main", "sidecar")))
        if i == 7:
            continue  # dead pod: its log read will fail
        lines = "".join(
            pod_log(name, (i * 7 + j) % 60, f"ERROR event-{i}-{j}" if j % 3 == 0 else f"info {i}-{j}")
            for j in range(10)
        )
        logs[(name, "main")] = lines
    return pods, logs, {"app-07"}


def test_parallel_equals_sequential_search(monkeypatch):
    """THE fan-out invariant: results are byte-identical in ORDER and content
    whether the fetch loop ran concurrency 1 (sequential) or 8 (parallel)."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    pods, logs, fail_pods = build_mock_fleet()

    outs = {}
    for concurrency in ("1", "8", "3"):
        monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, concurrency)
        k8s = FakeK8s(pods, logs, fail_pods=fail_pods)
        k8s.install(monkeypatch)
        outs[concurrency] = run(server.search_logs("ns", "ERROR"))
    assert outs["1"] == outs["8"] == outs["3"]

    data = json.loads(outs["8"])
    assert data["pods_searched"] == 23  # every pod read except the dead one
    stamps = [m.split("Z ", 1)[0][-8:] for m in data["matches"]]
    assert stamps == sorted(stamps)  # chronological order preserved


def test_parallel_equals_sequential_count(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    pods, logs, fail_pods = build_mock_fleet()
    outs = {}
    for concurrency in ("1", "8"):
        monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, concurrency)
        k8s = FakeK8s(pods, logs, fail_pods=fail_pods)
        k8s.install(monkeypatch)
        outs[concurrency] = run(server.count_matches("ns", "ERROR"))
    assert outs["1"] == outs["8"]
    data = json.loads(outs["8"])
    assert len(data["counts"]) == 23 and data["errors"]


def test_fetch_concurrency_bounds_inflight_reads(monkeypatch):
    """The semaphore is real: with concurrency 2 and a 50ms fake read, five
    pods complete in ~3 waves (~150ms), not serially (~250ms) — and a wave
    never holds more than `concurrency` reads at once."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, "2")
    pods = [make_pod(f"p{i}") for i in range(6)]
    logs = {(f"p{i}", "main"): pod_log(f"p{i}", i, "ERROR x") for i in range(6)}
    k8s = FakeK8s(pods, logs, delay=0.05)
    k8s.install(monkeypatch)
    in_flight = 0
    peak = 0
    base_read = server._read_log

    def counting_read(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            return base_read(*args, **kwargs)
        finally:
            in_flight -= 1

    monkeypatch.setattr(server, "_read_log", counting_read)
    t0 = time.perf_counter()
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    wall = time.perf_counter() - t0
    assert out["match_count"] == 6
    assert peak == 2  # semaphore width respected exactly
    assert 0.14 < wall < 1.0  # 3 waves of 50ms, not 6 x 50ms serial


def test_pod_cap_applies_before_fanout(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    monkeypatch.setenv("LOGSEARCH_MAX_PODS", "3")
    monkeypatch.setenv(server.ENV_FETCH_CONCURRENCY, "8")
    pods = [make_pod(f"p{i}") for i in range(10)]
    logs = {(f"p{i}", "main"): pod_log(f"p{i}", i, "ERROR x") for i in range(10)}
    k8s = FakeK8s(pods, logs)
    k8s.install(monkeypatch)
    out = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["pod_cap_applied"] is True and out["pods_searched"] == 3
