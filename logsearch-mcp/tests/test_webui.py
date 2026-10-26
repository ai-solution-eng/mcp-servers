"""Unit tests for the LogSearch MCP web UI + JSON API (no cluster, no network).

Mirrors the fleet's prometheus tests/test_webui.py pattern, adapted to this
repo's test venv (mcp + starlette + pytest, NO httpx — so instead of
starlette's TestClient the Starlette Route endpoints are driven directly
with stub requests; the same "call it like a client would" coverage).

The UI endpoints call the MCP tool coroutines on ``server`` (the SAME code
paths), so the seams are monkeypatched exactly like tests/test_logsearch.py
does — the webui gets whatever the fakes serve and can have no extra powers.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

import server
import webui

# ---------------------------------------------------------------------------
# Fixtures & fakes (same shape as tests/test_logsearch.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    """Start every test from a clean LOGSEARCH_* environment so a developer's
    shell can never leak policy/caps/UI gating into these results.

    The D8 escape hatch is SET here (empty allowlist = open) because the
    behavioral tests below address pods in a plain 'ns' namespace and are not
    about the policy; the deny-on-empty default has its own tests in
    tests/test_hardening.py."""
    for name in (
        server.ENV_ALLOWED,
        server.ENV_BLOCKED,
        server.ENV_EMPTY_ALLOWS_ALL,
        server.ENV_MAX_LINE_CHARS,
        server.ENV_FETCH_CONCURRENCY,
        server.ENV_MAX_REGEX_CHARS,
        server.ENV_WEBUI_ENABLED,
        "LOGSEARCH_MAX_PODS",
        "LOGSEARCH_MAX_LINES_PER_POD",
        "LOGSEARCH_MAX_TOTAL_LINES",
        "LOGSEARCH_UI_HTML",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, "1")


def make_pod(name, containers=("main",), restarts=0, started="2026-09-08T00:00:00Z"):
    return {"name": name, "containers": list(containers), "restarts": restarts, "started": started}


class FakeK8s:
    """In-memory stand-in for the two kubernetes seams, with call recording."""

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


class FakeRequest:
    """Just enough of starlette.Request for the webui endpoints: query
    params (GET) and a JSON body (POST)."""

    def __init__(self, params=None, body=None):
        self.query_params = params or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


def route_by_path(routes, path):
    return next(r for r in routes if getattr(r, "path", None) == path)


def call_route(routes, path, request):
    """Invoke one route endpoint exactly as the router would."""
    return asyncio.run(route_by_path(routes, path).endpoint(request))


def get(routes, path, **params):
    return call_route(routes, path, FakeRequest(params=params))


def post(routes, path, body):
    return call_route(routes, path, FakeRequest(body=body))


def post_raw(routes, path):
    """POST with no parseable body."""
    return call_route(routes, path, FakeRequest())


def routes():
    return webui.build_ui_routes()


# ---------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------


def test_ui_serves_hpe_branding_and_tabs():
    r = get(routes(), "/")
    assert r.status_code == 200
    html = r.body.decode("utf-8")
    assert "Hewlett Packard Enterprise" in html
    assert "hpe-element" in html  # the green parallelogram mark
    assert "logsearch-theme" in html  # no-flash theme script + persistence
    assert "LogSearch MCP" in html
    for tab in ("Search", "Counts", "Sources", "MCP Tools"):
        assert tab in html
    # the console inputs the task requires
    for name in ("s-namespace", "s-selector", "s-pattern", "s-since", "s-case", "s-pod-regex"):
        assert f'id="{name}"' in html
    # tool catalog is rendered client-side from this embedded array
    for tool in ("list_log_sources", "get_pod_logs", "search_logs", "count_matches"):
        assert tool in html
    assert get(routes(), "/ui").body == r.body  # same single asset at /ui


def test_ui_is_selfcontained_no_external_assets():
    # Corporate-proxy safe: no CDN, no external fonts, no build step.
    # XML namespace identifiers (www.w3.org/2000/svg inside data: URIs) are
    # spec-required labels, never fetched — exempt them.
    html = webui._load_html()
    urls = re.findall(r"https?://[^\s\"'<>)]+", html)
    external = [u for u in urls if "www.w3.org" not in u]
    assert not external, f"external assets referenced: {external}"
    assert "integrity=" not in html and "src=" not in html


def test_ui_html_fallback_when_asset_missing(monkeypatch):
    monkeypatch.setattr(webui, "_HTML_CANDIDATES", (Path("/nonexistent/ui/index.html"),))
    html = webui._load_html()
    assert "UI asset not found" in html
    assert "LOGSEARCH_UI_HTML" in html


def test_ui_has_no_duplicate_element_ids():
    # getElementById silently resolves to the FIRST match — duplicated ids
    # would leave one of the two panels rendering "—" forever.
    ids = re.findall(r'id="([^"]+)"', webui._load_html())
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate element ids: {sorted(dupes)}"


# ---------------------------------------------------------------------------
# /api/status — policy + caps, self-describing
# ---------------------------------------------------------------------------


def test_status_reports_policy_and_caps():
    data = json.loads(get(routes(), "/api/status").body)
    assert data["status"] == "ok" and data["server"] == "logsearch-mcp"
    assert data["mcp_endpoint"] == "/mcp"
    # clean env: empty lists mean ALL namespaces allowed
    assert data["policy"]["allowed"] == []
    assert data["policy"]["blocked"] == []
    assert data["policy"]["env_allowed"] == server.ENV_ALLOWED
    assert data["caps"]["max_pods"] == server.DEFAULT_MAX_PODS
    assert data["caps"]["max_lines_per_pod"] == server.DEFAULT_MAX_LINES_PER_POD
    assert data["caps"]["max_total_lines"] == server.DEFAULT_MAX_TOTAL_LINES
    assert data["caps"]["max_output_chars"] == server._MAX_OUTPUT_CHARS


def test_status_reflects_env_policy(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "team-a,prod-*")
    monkeypatch.setenv(server.ENV_BLOCKED, "kube-*")
    data = json.loads(get(routes(), "/api/status").body)
    assert data["policy"]["allowed"] == ["team-a", "prod-*"]
    assert data["policy"]["blocked"] == ["kube-*"]


# ---------------------------------------------------------------------------
# /api/sources — the discovery view (list_log_sources)
# ---------------------------------------------------------------------------


def test_sources_lists_pods_containers_restarts_age(monkeypatch):
    k8s = FakeK8s(
        [
            make_pod("b-pod", containers=("api", "sidecar"), restarts=3, started="2026-09-07T00:00:00Z"),
            make_pod("a-pod", containers=("worker",)),
        ],
        {},
    )
    k8s.install(monkeypatch)
    resp = get(routes(), "/api/sources", namespace="ns", label_selector="app=x")
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["pod_count"] == 2
    assert [p["name"] for p in data["pods"]] == ["a-pod", "b-pod"]
    assert data["pods"][1]["containers"] == ["api", "sidecar"]
    assert data["pods"][1]["restarts"] == 3
    assert data["pods"][1]["age"].endswith(("d", "h", "m"))
    # selector reaches the SAME seam the tool uses
    assert k8s.list_calls == [("ns", "app=x")]


def test_sources_requires_namespace():
    resp = get(routes(), "/api/sources")
    assert resp.status_code == 400
    assert json.loads(resp.body)["error"].startswith("Error:")


def test_sources_denied_namespace_is_clean_403(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "team-a")
    resp = get(routes(), "/api/sources", namespace="team-b")
    assert resp.status_code == 403
    err = json.loads(resp.body)["error"]
    assert err.startswith("Error:")
    assert server.ENV_ALLOWED in err and server.ENV_BLOCKED in err


# ---------------------------------------------------------------------------
# /api/search — the console (search_logs), parity with the MCP tool
# ---------------------------------------------------------------------------


def test_search_matches_tool_payload_parity(monkeypatch):
    k8s = FakeK8s(
        [make_pod("api-a", ("api",)), make_pod("api-b", ("worker",))],
        {
            ("api-a", "api"): ("2026-09-08T00:00:10Z ERROR late\n2026-09-08T00:00:01Z ERROR early\n"),
            ("api-b", "worker"): "2026-09-08T00:00:05Z ERROR middle\n",
        },
    )
    k8s.install(monkeypatch)
    resp = post(routes(), "/api/search", {"namespace": "ns", "pattern": "ERROR"})
    assert resp.status_code == 200
    data = json.loads(resp.body)
    # EXACT parity: the API returns what the MCP tool returns.
    assert data == json.loads(asyncio.run(server.search_logs("ns", "ERROR")))
    # provenance + chronological order on the wire
    assert data["matches"][0].startswith("api-a/api: ")
    assert data["matches"][1].startswith("api-b/worker: ")
    assert [m.split("Z ", 1)[1] for m in data["matches"]] == [
        "ERROR early",
        "ERROR middle",
        "ERROR late",
    ]
    assert data["truncated"] is False


def test_search_budget_stop_flag_and_early_exit(monkeypatch):
    k8s = FakeK8s(
        [make_pod("p")],
        {("p", "main"): "".join(f"2026-09-08T00:00:0{i}Z hit {i}\n" for i in range(1, 6))},
    )
    k8s.install(monkeypatch)
    data = json.loads(post(routes(), "/api/search", {"namespace": "ns", "pattern": "hit", "max_total_lines": 3}).body)
    assert data["truncated"] is True and data["match_count"] == 3
    # Early budget enforcement (Wave-1): the first matches in pod order are
    # kept, the rest of the pod is not pulled.
    assert "hit 5" not in " ".join(data["matches"])
    assert data["pods_skipped_budget"] == 1


def test_search_passthrough_toggles(monkeypatch):
    k8s = FakeK8s(
        [make_pod("p")],
        {("p", "main"): "2026-09-08T00:00:01Z ERROR upper\n2026-09-08T00:00:02Z error lower\n"},
    )
    k8s.install(monkeypatch)
    ins = json.loads(
        post(routes(), "/api/search", {"namespace": "ns", "pattern": "error", "case_insensitive": True}).body
    )
    sens = json.loads(
        post(routes(), "/api/search", {"namespace": "ns", "pattern": "error", "case_insensitive": False}).body
    )
    assert ins["match_count"] == 2
    assert sens["match_count"] == 1
    since = json.loads(
        post(routes(), "/api/search", {"namespace": "ns", "pattern": "error", "since_minutes": 2.5}).body
    )
    assert since["match_count"] == 2
    assert k8s.read_calls[-1]["since_seconds"] == 150  # same conversion as the tool


def test_search_failing_pod_isolated_in_errors(monkeypatch):
    k8s = FakeK8s(
        [make_pod("dead"), make_pod("alive")],
        {("alive", "main"): "2026-09-08T00:00:01Z ERROR alive\n"},
        fail_pods={"dead"},
    )
    k8s.install(monkeypatch)
    resp = post(routes(), "/api/search", {"namespace": "ns", "pattern": "ERROR"})
    assert resp.status_code == 200  # one dead pod must not sink the search
    data = json.loads(resp.body)
    assert data["pods_searched"] == 1
    assert len(data["errors"]) == 1 and "dead" in data["errors"][0]


def test_search_denied_namespace_is_clean_403(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "team-a")
    resp = post(routes(), "/api/search", {"namespace": "team-b", "pattern": "x"})
    assert resp.status_code == 403
    err = json.loads(resp.body)["error"]
    assert err.startswith("Error:") and "team-b" in err
    assert server.ENV_ALLOWED in err and server.ENV_BLOCKED in err


def test_search_bad_regex_is_clean_400():
    resp = post(routes(), "/api/search", {"namespace": "ns", "pattern": "([unclosed"})
    assert resp.status_code == 400
    err = json.loads(resp.body)["error"]
    assert "invalid regex" in err and "([unclosed" in err


def test_search_bad_pod_regex_is_clean_400():
    resp = post(routes(), "/api/search", {"namespace": "ns", "pattern": "fine", "pod_regex": "api(["})
    assert resp.status_code == 400
    assert "api([" in json.loads(resp.body)["error"]


def test_search_unreachable_cluster_is_clean_502(monkeypatch):
    def unreachable(namespace, label_selector=""):
        raise server.LogSearchError(
            "Kubernetes cluster unreachable while listing pods in namespace "
            f"{namespace!r}: ConfigException: no configuration"
        )

    monkeypatch.setattr(server, "_list_pods", unreachable)
    resp = post(routes(), "/api/search", {"namespace": "ns", "pattern": "x"})
    assert resp.status_code == 502
    err = json.loads(resp.body)["error"]
    assert err.startswith("Error:")
    assert "Kubernetes cluster unreachable" in err


def test_search_validates_namespace_and_pattern():
    r1 = post(routes(), "/api/search", {"pattern": "x"})
    assert r1.status_code == 400
    r2 = post(routes(), "/api/search", {"namespace": "ns"})
    assert r2.status_code == 400
    assert json.loads(r2.body)["error"].startswith("Error:")


def test_post_with_invalid_body_is_400():
    assert post_raw(routes(), "/api/search").status_code == 400
    assert post_raw(routes(), "/api/count").status_code == 400


# ---------------------------------------------------------------------------
# /api/count — the per-pod counts view (count_matches)
# ---------------------------------------------------------------------------


def test_count_ranking_descending(monkeypatch):
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
    resp = post(routes(), "/api/count", {"namespace": "ns", "pattern": "ERROR"})
    assert resp.status_code == 200
    data = json.loads(resp.body)
    # JSON object preserves insertion order == the descending ranking.
    assert list(data["counts"].items()) == [("loud", 5), ("quiet", 2), ("silent", 0)]
    assert data["total_matches"] == 7
    assert data["pods_with_matches"] == 2


def test_count_denied_namespace_and_bad_regex(monkeypatch):
    monkeypatch.setenv(server.ENV_ALLOWED, "team-a")
    resp = post(routes(), "/api/count", {"namespace": "team-b", "pattern": "x"})
    assert resp.status_code == 403
    assert "team-b" in json.loads(resp.body)["error"]
    # an ALLOWED namespace with a broken pattern -> the regex error, not the policy
    resp = post(routes(), "/api/count", {"namespace": "team-a", "pattern": "ERROR["})
    assert resp.status_code == 400
    assert "ERROR[" in json.loads(resp.body)["error"]


def test_count_missing_pattern_is_400():
    assert post(routes(), "/api/count", {"namespace": "ns"}).status_code == 400


# ---------------------------------------------------------------------------
# server wiring: webui.enabled gate around the MCP 2.0 stateless core
# ---------------------------------------------------------------------------


def test_server_mounts_ui_routes_by_default():
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/ui", "/api/status", "/api/sources", "/api/search", "/api/count"} <= paths
    # the MCP core is untouched and still mounted
    assert {"/health", "/healthz", "/mcp"} <= paths


def test_webui_disabled_removes_ui_routes_but_keeps_mcp(monkeypatch):
    monkeypatch.setenv(server.ENV_WEBUI_ENABLED, "false")
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert not any(p.startswith("/api/") or p == "/" or p == "/ui" for p in paths)
    assert "/mcp" in paths and "/health" in paths and "/healthz" in paths


def test_webui_gate_truthiness_matrix(monkeypatch):
    for raw, expected in (
        ("", True),
        ("true", True),
        ("1", True),
        ("YES", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("junk", True),
    ):
        monkeypatch.setenv(server.ENV_WEBUI_ENABLED, raw)
        assert server._webui_enabled() is expected, raw
    monkeypatch.delenv(server.ENV_WEBUI_ENABLED)
    assert server._webui_enabled() is True  # unset -> enabled
