"""Wave-5 F3 tests — export_matches (ADDITIVE, OPT-IN via LOGSEARCH_EXPORT_ROOT).

Covers the audit's accepted feature idea (FLEET-AUDIT §3.3 workbench ideas,
built on the logsearch side): run the EXACT search_logs pipeline and write
the matched lines to <LOGSEARCH_EXPORT_ROOT>/<dest_name> so a result set too
big for a context window can be read back from disk (fleet convention: a
path on the workbench/shared PVC).

Asserted here:
1. OPT-IN — LOGSEARCH_EXPORT_ROOT unset → self-describing refusal, and the
   search never even starts (no k8s call).
2. The written file carries a small #-header (query, namespace, timestamp,
   counts) and the matched lines BYTE-IDENTICAL to search_logs' output —
   same caps (max_total_lines budget, per-line char cap), same sort.
3. Path safety — dest_name with separators/'..'/leading dots is refused
   before anything is written; the export cannot escape the root.
4. Every guard of the shared pipeline still applies through export_matches:
   the ReDoS screen, the D8 namespace policy (identical denial strings),
   invalid args. A failed search writes NOTHING.
5. Write discipline — root auto-created, atomic replace (not append), and a
   clean error when the root is not writable.

Run:  cd mcp_servers/logsearch_mcp && SQLhandler/.venv312/bin/python -m pytest tests/test_export.py -v
"""

import asyncio
import json

import pytest

import server


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    """Clean LOGSEARCH_* environment; NO escape hatch (D8 default-deny is the
    posture these tests set what they need around)."""
    for name in (
        server.ENV_ALLOWED,
        server.ENV_BLOCKED,
        server.ENV_EMPTY_ALLOWS_ALL,
        server.ENV_MAX_LINE_CHARS,
        server.ENV_FETCH_CONCURRENCY,
        server.ENV_MAX_REGEX_CHARS,
        server.ENV_EXPORT_ROOT,
        "LOGSEARCH_MAX_PODS",
        "LOGSEARCH_MAX_LINES_PER_POD",
        "LOGSEARCH_MAX_TOTAL_LINES",
    ):
        monkeypatch.delenv(name, raising=False)


def make_pod(name, containers=("main",), restarts=0, started="2026-09-08T00:00:00Z"):
    return {"name": name, "containers": list(containers), "restarts": restarts, "started": started}


class FakeK8s:
    """Same in-memory seam stand-in as tests/test_hardening.py."""

    def __init__(self, pods, logs, fail_pods=()):
        self.pods = pods
        self.logs = logs
        self.fail_pods = set(fail_pods)
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
            if pod in k8s.fail_pods:
                raise server.LogSearchError(f"pod {pod!r} evaporated mid-search (fake 404)")
            return k8s.logs[(pod, container)]

        monkeypatch.setattr(server, "_list_pods", fake_list_pods)
        monkeypatch.setattr(server, "_read_log", fake_read_log)


def run(coro):
    return asyncio.run(coro)


def pod_log(pod, ts, text):
    return f"2026-09-08T00:00:{ts:02d}Z {text}\n"


def header_lines(text):
    """The file's #-prefixed header block (up to the blank separator line)."""
    lines = text.splitlines()
    if "" in lines:
        cut = lines.index("")
        return lines[:cut], lines[cut + 1 :]
    return lines, []  # empty export: header only, no blank separator


# ---------------------------------------------------------------------------
# 1 · opt-in posture: unset root → refusal, no search, no write
# ---------------------------------------------------------------------------


def test_export_refuses_when_root_unset(monkeypatch):
    k8s = FakeK8s([make_pod("p")], {("p", "main"): pod_log("p", 1, "ERROR x")})
    k8s.install(monkeypatch)
    out = run(server.export_matches("ns", "ERROR", "out.log"))
    assert out.startswith("Error:")
    assert server.ENV_EXPORT_ROOT in out
    assert "opt" in out.lower() or "Setup" in out  # self-describing setup guidance
    assert k8s.list_calls == []  # refused BEFORE the search pipeline starts


def test_export_root_read_lazily(monkeypatch, tmp_path):
    """The env is read per call (the fleet pattern): setting it mid-process
    enables the tool without a reimport."""
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    k8s = FakeK8s([make_pod("p")], {("p", "main"): pod_log("p", 1, "ERROR x")})
    k8s.install(monkeypatch)
    out = run(server.export_matches("ns", "ERROR", "out.log"))
    assert out.startswith("Error:")  # unset → refused
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out = json.loads(run(server.export_matches("ns", "ERROR", "out.log")))
    assert out["exported"] is True


# ---------------------------------------------------------------------------
# 2 · file written: header + matches byte-identical to search_logs
# ---------------------------------------------------------------------------


@pytest.fixture()
def search_k8s(monkeypatch):
    k8s = FakeK8s(
        [make_pod("api-1"), make_pod("api-2", containers=("main", "sidecar"))],
        {
            ("api-1", "main"): pod_log("api-1", 1, "ERROR boom") + pod_log("api-1", 3, "fine"),
            ("api-2", "main"): pod_log("api-2", 2, "ERROR again"),
            ("api-2", "sidecar"): pod_log("api-2", 4, "ERROR side"),
        },
    )
    k8s.install(monkeypatch)
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    return k8s


def test_export_writes_header_plus_identical_matches(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    raw = run(server.export_matches("ns", "ERROR", "triage.log"))
    out = json.loads(raw)
    assert out["exported"] is True
    assert out["path"] == str(tmp_path / "exports" / "triage.log")
    # search_logs searches each pod's FIRST container by default:
    # api-1/main ERROR boom + api-2/main ERROR again (the sidecar is not read)
    assert out["match_count"] == 2

    text = (tmp_path / "exports" / "triage.log").read_text()
    header, body_lines = header_lines(text)
    assert header[0] == "# logsearch export_matches"
    joined = "\n".join(header)
    assert "# namespace: ns" in joined
    assert "# pattern: ERROR" in joined
    assert "# timestamp: " in joined
    assert "# matches: 2 (truncated: False)" in joined

    # the payload: BYTE-IDENTICAL to what search_logs returns, same order
    expected = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["match_count"] == expected["match_count"]
    assert out["pods_searched"] == expected["pods_searched"]
    assert out["truncated"] == expected["truncated"]
    assert body_lines == expected["matches"]


def test_export_identical_under_budget_truncation(tmp_path, monkeypatch, search_k8s):
    """Caps honored: a small max_total_lines budget truncates the export the
    same way it truncates search_logs (and the header says so)."""
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out = json.loads(run(server.export_matches("ns", "ERROR", "budget.log", max_total_lines=1)))
    expected = json.loads(run(server.search_logs("ns", "ERROR", max_total_lines=1)))
    text = (tmp_path / "exports" / "budget.log").read_text()
    header, body_lines = header_lines(text)
    assert body_lines == expected["matches"]
    assert expected["truncated"] is True
    assert out["truncated"] is True
    assert "# matches: 1 (truncated: True)" in "\n".join(header)
    assert out["pods_skipped_budget"] == expected["pods_skipped_budget"]


def test_export_identical_under_per_line_char_cap(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    monkeypatch.setenv(server.ENV_MAX_LINE_CHARS, "30")
    out = json.loads(run(server.export_matches("ns", "ERROR", "capped.log")))
    expected = json.loads(run(server.search_logs("ns", "ERROR")))
    _, body_lines = header_lines((tmp_path / "exports" / "capped.log").read_text())
    assert body_lines == expected["matches"]
    assert all(len(line) < 60 for line in body_lines)  # cap + marker, no megabyte lines
    assert out["match_count"] == expected["match_count"]


def test_export_identical_with_selector_container_and_case_flags(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    for kwargs in (
        {"label_selector": "app=x"},
        {"container": "sidecar"},
        {"case_insensitive": False, "pattern": "error"},
        {"pod_regex": "^api-1$"},
        {"tail_lines": 1},
        {"since_minutes": 2.5},
    ):
        pattern = kwargs.pop("pattern", "ERROR")
        out = json.loads(run(server.export_matches("ns", pattern, "k.log", **kwargs)))
        expected = json.loads(run(server.search_logs("ns", pattern, **kwargs)))
        _, body_lines = header_lines((tmp_path / "exports" / "k.log").read_text())
        assert body_lines == expected["matches"], kwargs
        assert out["match_count"] == expected["match_count"], kwargs


def test_export_empty_result_writes_header_only(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out = json.loads(run(server.export_matches("ns", "NOMATCH-XYZ", "empty.log")))
    assert out["exported"] is True and out["match_count"] == 0
    text = (tmp_path / "exports" / "empty.log").read_text()
    header, body_lines = header_lines(text)
    assert body_lines == []
    assert "# matches: 0 (truncated: False)" in "\n".join(header)


def test_export_replaces_not_appends(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    run(server.export_matches("ns", "ERROR", "out.log"))
    run(server.export_matches("ns", "NOMATCH-XYZ", "out.log"))
    text = (tmp_path / "exports" / "out.log").read_text()
    header, body_lines = header_lines(text)
    assert body_lines == []
    assert "# matches: 0" in "\n".join(header)
    # no temp litter left behind
    assert [p.name for p in (tmp_path / "exports").iterdir()] == ["out.log"]


def test_export_response_never_carries_the_matches(tmp_path, monkeypatch, search_k8s):
    """The file is the artifact: the MCP response stays small (counts + path)."""
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out = json.loads(run(server.export_matches("ns", "ERROR", "out.log")))
    assert "matches" not in out and "match_count" in out and "bytes" in out


# ---------------------------------------------------------------------------
# 3 · path safety: dest_name is a bare file name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dest",
    [
        "../escape.log",
        "sub/dir.log",
        "sub\\win.log",
        "..",
        "...",
        "a..b.log",
        ".hidden",
        "",
        "  spaced.log",
        "trailing ",
        "tab\tname",
        "new\nline",
        "/absolute.log",
    ],
)
def test_dest_name_traversal_and_junk_rejected(tmp_path, monkeypatch, dest):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out = run(server.export_matches("ns", "ERROR", dest))
    assert out.startswith("Error:"), dest
    assert "dest_name" in out
    # nothing written anywhere under the root
    assert not (tmp_path / "exports").exists() or list((tmp_path / "exports").iterdir()) == []


def test_valid_dest_names_accepted(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    for dest in ("out.log", "triage-2026-09-13", "a_b.c-d", "9lives"):
        out = json.loads(run(server.export_matches("ns", "ERROR", dest)))
        assert out["exported"] is True, dest
        assert (tmp_path / "exports" / dest).is_file()


def test_export_cannot_escape_root_via_valid_name(tmp_path, monkeypatch, search_k8s):
    root = tmp_path / "exports"
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(root))
    run(server.export_matches("ns", "ERROR", "out.log"))
    assert sorted(p.name for p in root.iterdir()) == ["out.log"]
    assert not (tmp_path / "escape.log").exists()


# ---------------------------------------------------------------------------
# 4 · the shared pipeline's guards still apply through export_matches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["(a+)+", "(.*)*x", r"(\w+\s)*x", "(a|aa)+"])
def test_redos_screen_still_applies_and_writes_nothing(tmp_path, monkeypatch, pattern):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    monkeypatch.setenv(server.ENV_ALLOWED, "*")
    k8s = FakeK8s([make_pod("p")], {("p", "main"): pod_log("p", 1, "aaaaaaaaaaaaaaaaaaaa x")})
    k8s.install(monkeypatch)
    out = run(server.export_matches("ns", pattern, "out.log"))
    assert out.startswith("Error:") and "unsafe regex" in out
    assert not (tmp_path / "exports" / "out.log").exists()


def test_d8_policy_denial_identical_to_search_logs_and_writes_nothing(tmp_path, monkeypatch, search_k8s):
    """Empty allowlist = deny-all (D8): export_matches refuses with the VERY
    SAME string search_logs produces, and writes nothing."""
    monkeypatch.delenv(server.ENV_ALLOWED, raising=False)  # empty → deny all
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out_export = run(server.export_matches("secret-ns", "ERROR", "out.log"))
    out_search = run(server.search_logs("secret-ns", "ERROR"))
    assert out_export == out_search
    assert "denied by this server's namespace policy" in out_export
    assert not (tmp_path / "exports").exists()  # nothing written


def test_bad_args_identical_to_search_logs(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    for kwargs in (
        {"pattern": "([unclosed"},
        {"pod_regex": "api(["},
        {"tail_lines": 0},
        {"max_total_lines": 0},
        {"since_minutes": -1},
    ):
        pattern = kwargs.pop("pattern", "ERROR")
        out_export = run(server.export_matches("ns", pattern, "out.log", **kwargs))
        out_search = run(server.search_logs("ns", pattern, **kwargs))
        assert out_export == out_search, kwargs
        assert not (tmp_path / "exports" / "out.log").exists(), kwargs


def test_dead_pod_recorded_in_export_like_search(tmp_path, monkeypatch, search_k8s):
    k8s = FakeK8s(
        [make_pod("dead"), make_pod("alive")],
        {
            ("dead", "main"): "",
            ("alive", "main"): pod_log("alive", 1, "ERROR ok"),
        },
        fail_pods=("dead",),
    )
    k8s.install(monkeypatch)
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    out = json.loads(run(server.export_matches("ns", "ERROR", "dead.log")))
    expected = json.loads(run(server.search_logs("ns", "ERROR")))
    assert out["errors"] == expected["errors"] and len(out["errors"]) == 1
    text = (tmp_path / "exports" / "dead.log").read_text()
    assert "# errors: [" in "\n".join(header_lines(text)[0])  # the dead pod is honestly reported


# ---------------------------------------------------------------------------
# 5 · write discipline: root creation, unwritable root, atomicity helpers
# ---------------------------------------------------------------------------


def test_export_root_created_on_demand(tmp_path, monkeypatch, search_k8s):
    root = tmp_path / "deep" / "nested" / "exports"
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(root))
    out = json.loads(run(server.export_matches("ns", "ERROR", "out.log")))
    assert out["exported"] is True
    assert root.is_dir() and (root / "out.log").is_file()


def test_export_root_that_cannot_be_created_clear_error(tmp_path, monkeypatch, search_k8s):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(blocker / "exports"))
    out = run(server.export_matches("ns", "ERROR", "out.log"))
    assert out.startswith("Error:")
    assert "WRITABLE" in out and str(blocker / "exports") in out


def test_export_root_as_file_clear_error(tmp_path, monkeypatch, search_k8s):
    root = tmp_path / "afile"
    root.write_text("not a dir")
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(root))
    out = run(server.export_matches("ns", "ERROR", "out.log"))
    assert out.startswith("Error:") and "WRITABLE" in out


# ---------------------------------------------------------------------------
# 6 · header/content hygiene helpers
# ---------------------------------------------------------------------------


def test_hostile_pattern_cannot_forge_header_lines(tmp_path, monkeypatch, search_k8s):
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    pattern = "ERROR\nmatches: 999 (forged)"  # a REAL newline in the pattern
    run(server.export_matches("ns", pattern, "forged.log"))
    text = (tmp_path / "exports" / "forged.log").read_text()
    header, _ = header_lines(text)
    assert "# pattern: ERROR\\nmatches: 999 (forged)" in header  # one ESCAPED line
    assert not any(line == "# matches: 999 (forged)" for line in header)
    assert len([line for line in header if line.startswith("# matches:")]) == 1


def test_export_header_counts_reflect_pipeline(tmp_path, monkeypatch, search_k8s):
    k8s = FakeK8s(
        [make_pod("dead"), make_pod("alive")],
        {("dead", "main"): "", ("alive", "main"): pod_log("alive", 1, "ERROR ok")},
        fail_pods=("dead",),
    )
    k8s.install(monkeypatch)
    monkeypatch.setenv(server.ENV_EXPORT_ROOT, str(tmp_path / "exports"))
    run(server.export_matches("ns", "ERROR", "out.log"))
    text = (tmp_path / "exports" / "out.log").read_text()
    header, body = header_lines(text)
    assert body == ["alive/main: 2026-09-08T00:00:01Z ERROR ok"]
    assert "# matches: 1 (truncated: False)" in "\n".join(header)
    assert "# pods_searched: 1" in "\n".join(header)
    assert "# errors: [" in "\n".join(header)  # the dead pod is honestly reported


# ---------------------------------------------------------------------------
# 7 · the web console is unchanged by the new tool (UI has no export powers)
# ---------------------------------------------------------------------------


class FakeRequest:
    """Just enough of starlette.Request for the webui endpoints (the
    test_webui.py house pattern)."""

    def __init__(self, body=None):
        self._body = body

    async def json(self):
        return self._body


def test_webui_search_endpoint_parity_unchanged(monkeypatch, search_k8s):
    """The console /api/search wraps the SAME search_logs coroutine and is
    byte-identical; export_matches is deliberately MCP-only (no /api route)."""
    import webui as webui_module

    by_path = {getattr(r, "path", None): r for r in webui_module.build_ui_routes()}
    out = asyncio.run(by_path["/api/search"].endpoint(FakeRequest({"namespace": "ns", "pattern": "ERROR"})))
    assert out.status_code == 200
    assert json.loads(out.body) == json.loads(run(server.search_logs("ns", "ERROR")))
    # export is MCP-only by design: no /api route carries it
    paths = {getattr(r, "path", "") for r in server._build_http_app().routes}
    assert not any("export" in p for p in paths)
