"""Wave-4 performance/hardening tests (D14): step clamp, overview cache,
label-name validation.

The contracts being pinned (FLEET-EXECUTION-PLAN-2026-09 §8 E3, decision
D14 — ratified):

* STEP CLAMP — a range-query step below PROMETHEUS_MIN_STEP_SECONDS
  (default 15; 0 disables; garbage -> default) is raised to the floor and
  the caller is TOLD (one honest notice line in the MCP tool result, a
  ``step_notice`` field in the JSON API). Steps at/above the floor pass
  through byte-unchanged. Applies to every range path: MCP
  prom_query_range, POST /api/query_range, and the /api/overview trends.
* OVERVIEW CACHE — /api/overview (~14 upstream queries per refresh) is
  served from a short-TTL cache (PROMETHEUS_OVERVIEW_CACHE_TTL, default
  20s; 0 disables) keyed by the request's actual parameter set. Every
  response is honestly marked (``cached`` + ``cache_age_seconds``);
  failures (every block errored) are never memoized; concurrent identical
  overviews share one computation (single-flight).
* LABEL NAMES — the client validates the label NAME before interpolating
  it into the /api/v1/label/<name>/values URL path (same rule the webui
  handler always applied, now shared: prom_client.LABEL_NAME_RE).

No network, no live Prometheus; the MCP tests ride the in-memory transport
(never TestClient-GET /mcp — fleet convention).
"""

import asyncio
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import webui
from prom_client import (
    LABEL_NAME_RE,
    PromConfig,
    PrometheusClient,
    PrometheusError,
    min_step_seconds,
    resolve_step,
    step_seconds,
)


def make_prom_client(handler, **overrides) -> PrometheusClient:
    cfg = PromConfig(base_url="http://prom.test:9090", **overrides)
    return PrometheusClient(cfg, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Step clamp — resolve_step unit matrix
# ---------------------------------------------------------------------------


def test_resolve_step_clamps_below_floor_with_notice():
    step, notice = resolve_step("1s", "0", "86400")
    assert step == "15s"
    assert notice == "step clamped to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS"
    # other below-floor forms clamp too
    for requested in ("0.5", "5s", "500ms", "2"):
        step, notice = resolve_step(requested, "0", "86400")
        assert step == "15s"
        assert notice and "PROMETHEUS_MIN_STEP_SECONDS" in notice
        assert f"(requested {requested})" in notice


def test_resolve_step_floor_and_above_pass_through_unchanged():
    for requested in ("15s", "15", "30", "30s", "1m", "5m", "1m30s", "1h"):
        step, notice = resolve_step(requested, "0", "86400")
        assert step == requested  # byte-identical, not rewritten
        assert notice is None


def test_resolve_step_zero_disables_clamping(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MIN_STEP_SECONDS", "0")
    step, notice = resolve_step("1s", "0", "86400")
    assert step == "1s" and notice is None  # escape hatch: today's behavior


def test_resolve_step_env_garbage_negative_or_unset_use_default(monkeypatch):
    for raw in ("banana", "", "  ", "-3", "12 monkeys", None):
        if raw is None:
            monkeypatch.delenv("PROMETHEUS_MIN_STEP_SECONDS", raising=False)
        else:
            monkeypatch.setenv("PROMETHEUS_MIN_STEP_SECONDS", raw)
        step, notice = resolve_step("1s", "0", "86400")
        assert step == "15s"  # default floor 15 applies
        assert notice and "requested 1s" in notice


def test_resolve_step_honors_custom_floor(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MIN_STEP_SECONDS", "30")
    step, notice = resolve_step("15s", "0", "86400")
    assert step == "30s" and notice and "(requested 15s)" in notice
    step, _ = resolve_step("30s", "0", "86400")
    assert step == "30s"  # at the custom floor -> passthrough


def test_resolve_step_auto_step_below_floor_is_clamped_too():
    # default resolver picks span/240 = 2s for a 600s window; the floor applies
    step, notice = resolve_step("", "0", "600")
    assert step == "15s"
    assert notice == "step clamped to 15s (requested 2s) — PROMETHEUS_MIN_STEP_SECONDS"


def test_resolve_step_unparseable_step_passes_through():
    step, notice = resolve_step("banana", "0", "86400")
    assert step == "banana" and notice is None  # Prometheus rejects it itself


def test_min_step_seconds_env_matrix(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_MIN_STEP_SECONDS", raising=False)
    assert min_step_seconds({}) == 15  # unset -> default
    assert min_step_seconds({"PROMETHEUS_MIN_STEP_SECONDS": "0"}) == 0  # disables
    assert min_step_seconds({"PROMETHEUS_MIN_STEP_SECONDS": "30"}) == 30
    assert min_step_seconds({"PROMETHEUS_MIN_STEP_SECONDS": " 15 "}) == 15
    for bad in ("banana", "", "-1", "12 monkeys"):
        assert min_step_seconds({"PROMETHEUS_MIN_STEP_SECONDS": bad}) == 15


def test_step_seconds_parser_matrix():
    assert step_seconds("15s") == 15.0
    assert step_seconds("1m30s") == 90.0
    assert step_seconds("500ms") == 0.5
    assert step_seconds("1h") == 3600.0
    assert step_seconds("1d") == 86400.0
    assert step_seconds("2") == 2.0  # bare seconds
    assert step_seconds("0.5") == 0.5
    for bad in ("", "   ", "banana", "15abc", "s", "m5"):
        assert step_seconds(bad) is None


# ---------------------------------------------------------------------------
# Step clamp — MCP tool surface (in-memory wire)
# ---------------------------------------------------------------------------


class _RangeStub:
    """Records the step each range_query actually sends upstream."""

    def __init__(self):
        self.steps: list[str] = []

    async def range_query(self, query, start, end, step):
        self.steps.append(step)
        return {
            "resultType": "matrix",
            "result": [{"metric": {"__name__": "m", "pod": "p1"}, "values": [["1757337600", "1"]]}],
        }


def _call_range(stub, args):
    import server

    async def run():
        from mcp.client._memory import InMemoryTransport
        from mcp.client.session import ClientSession

        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("prom_query_range", args)

    return asyncio.run(run())


def test_mcp_prom_query_range_clamps_and_says_so(monkeypatch):
    import server

    monkeypatch.delenv("PROMETHEUS_MIN_STEP_SECONDS", raising=False)
    stub = _RangeStub()
    monkeypatch.setattr(server, "client", stub)
    result = _call_range(stub, {"query": "up", "start": "0", "end": "86400", "step": "1s"})
    assert stub.steps == ["15s"]  # the floor went upstream, not 1s
    text = result.content[0].text
    assert "step clamped to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS" in text
    assert "step=15s" in text


def test_mcp_prom_query_range_unclamped_has_no_notice(monkeypatch):
    import server

    monkeypatch.delenv("PROMETHEUS_MIN_STEP_SECONDS", raising=False)
    stub = _RangeStub()
    monkeypatch.setattr(server, "client", stub)
    result = _call_range(stub, {"query": "up", "start": "0", "end": "86400", "step": "1m"})
    assert stub.steps == ["1m"]
    assert "clamped" not in result.content[0].text


def test_mcp_prom_query_range_disabled_clamp_sends_requested_step(monkeypatch):
    import server

    monkeypatch.setenv("PROMETHEUS_MIN_STEP_SECONDS", "0")
    stub = _RangeStub()
    monkeypatch.setattr(server, "client", stub)
    result = _call_range(stub, {"query": "up", "start": "0", "end": "86400", "step": "1s"})
    assert stub.steps == ["1s"]  # escape hatch: byte-for-byte today's behavior
    assert "clamped" not in result.content[0].text


# ---------------------------------------------------------------------------
# Step clamp — JSON API surface
# ---------------------------------------------------------------------------


def test_web_query_range_clamps_with_step_notice(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_MIN_STEP_SECONDS", raising=False)

    class StubClient:
        async def range_query(self, query, start, end, step):
            assert step == "15s"  # floored before the upstream call
            return {"resultType": "matrix", "result": []}

    cfg = PromConfig(base_url="http://prom.test:9090")
    c = TestClient(Starlette(routes=webui.build_ui_routes(StubClient(), cfg)))
    data = c.post(
        "/api/query_range",
        json={"query": "m", "start": "1757337600", "end": "1757341200", "step": "1s"},
    ).json()
    assert data["step"] == "15s"
    assert data["step_notice"] == "step clamped to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS"


def test_web_query_range_unclamped_omits_step_notice():
    class StubClient:
        async def range_query(self, query, start, end, step):
            assert step == "1m"
            return {"resultType": "matrix", "result": []}

    cfg = PromConfig(base_url="http://prom.test:9090")
    c = TestClient(Starlette(routes=webui.build_ui_routes(StubClient(), cfg)))
    data = c.post(
        "/api/query_range",
        json={"query": "m", "start": "1757337600", "end": "1757341200", "step": "1m"},
    ).json()
    assert data["step"] == "1m"
    assert "step_notice" not in data


def test_overview_trend_clamps_small_auto_span(monkeypatch):
    # The overview trends resolve their step automatically (span/240); a 1m
    # window would ask for 1s — the floor must apply on this path too.
    monkeypatch.delenv("PROMETHEUS_MIN_STEP_SECONDS", raising=False)
    monkeypatch.setattr(webui, "_OVERVIEW_TRENDS", (("cpu_trend", "up", "now-1m"),))

    class StubClient:
        async def instant_query(self, query, ts=None):
            return {"resultType": "vector", "result": []}

        async def range_query(self, query, start, end, step):
            assert step == "15s"
            return {"resultType": "matrix", "result": []}

        async def alerts(self):
            return []

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(StubClient(), cfg))).get("/api/overview").json()
    trend = data["trends"]["cpu_trend"]
    assert trend["step"] == "15s"
    assert "step clamped to 15s" in trend["step_notice"]


# ---------------------------------------------------------------------------
# /api/overview response cache (D14)
# ---------------------------------------------------------------------------


class CountingStub:
    """Counts upstream calls; one overview compute = 11 instant + 2 range + 1 alerts."""

    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.counts = {"instant": 0, "range": 0, "alerts": 0}
        self.delay = delay
        self.fail = fail

    async def _pause(self):
        if self.delay:
            await asyncio.sleep(self.delay)

    async def instant_query(self, query, ts=None):
        self.counts["instant"] += 1
        await self._pause()
        if self.fail:
            raise PrometheusError("upstream down (stub)")
        return {
            "resultType": "vector",
            "result": [{"metric": {"__name__": "up", "pod": f"p{i}"}, "value": [1757337600, str(i)]} for i in range(3)],
        }

    async def range_query(self, query, start, end, step):
        self.counts["range"] += 1
        await self._pause()
        if self.fail:
            raise PrometheusError("upstream down (stub)")
        return {
            "resultType": "matrix",
            "result": [{"metric": {"__name__": "m"}, "values": [["1757337600", "1"]]}],
        }

    async def alerts(self):
        self.counts["alerts"] += 1
        await self._pause()
        if self.fail:
            raise PrometheusError("upstream down (stub)")
        return []


def _overview_app(client) -> TestClient:
    cfg = PromConfig(base_url="http://prom.test:9090")
    return TestClient(Starlette(routes=webui.build_ui_routes(client, cfg)))


def test_overview_cache_hit_within_ttl_is_honestly_marked(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "20")
    stub = CountingStub()
    c = _overview_app(stub)
    first = c.get("/api/overview").json()
    assert first["cached"] is False and first["cache_age_seconds"] == 0
    generated_at = first["generated_at"]
    second = c.get("/api/overview").json()
    assert second["cached"] is True  # served from the TTL cache
    assert 0 <= second["cache_age_seconds"] < 20
    assert second["generated_at"] == generated_at  # the SAME payload, not recomputed
    assert second["cards"] == first["cards"] and second["alerts"] == first["alerts"]
    # one compute's worth of upstream traffic (11 instant + 2 range + 1 alerts)
    assert stub.counts == {"instant": 11, "range": 2, "alerts": 1}


def test_overview_cache_expires_and_recomputes(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "0.05")
    stub = CountingStub()
    c = _overview_app(stub)
    assert c.get("/api/overview").json()["cached"] is False
    assert c.get("/api/overview").json()["cached"] is True  # within the TTL
    time.sleep(0.12)  # > TTL -> entry expired
    after = c.get("/api/overview").json()
    assert after["cached"] is False and after["cache_age_seconds"] == 0
    assert stub.counts["instant"] == 22  # recomputed (11 × 2), not served stale


def test_overview_cache_disabled_by_ttl_zero(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "0")
    stub = CountingStub()
    c = _overview_app(stub)
    first = c.get("/api/overview").json()
    second = c.get("/api/overview").json()
    assert first["cached"] is False and second["cached"] is False
    assert first["cache_age_seconds"] == 0 and second["cache_age_seconds"] == 0
    assert stub.counts == {"instant": 22, "range": 4, "alerts": 2}  # every refresh recomputes


def test_overview_cache_ttl_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "banana")
    stub = CountingStub()
    c = _overview_app(stub)
    c.get("/api/overview")
    assert c.get("/api/overview").json()["cached"] is True  # default 20s TTL -> hit
    assert stub.counts["instant"] == 11


def test_overview_cache_key_is_the_parameter_set(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "20")
    stub = CountingStub()
    c = _overview_app(stub)
    c.get("/api/overview")
    other = c.get("/api/overview", params={"x": "1"}).json()
    assert other["cached"] is False  # different parameter set -> different key
    assert stub.counts["instant"] == 22
    assert c.get("/api/overview", params={"x": "1"}).json()["cached"] is True  # its own entry
    assert c.get("/api/overview?x=1").json()["cached"] is True  # same set, same key


def test_overview_failure_is_never_memoized(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "20")
    stub = CountingStub(fail=True)  # every upstream call fails -> every block errors
    c = _overview_app(stub)
    first = c.get("/api/overview").json()
    assert first["cached"] is False
    assert all("error" in b for b in first["cards"].values())
    second = c.get("/api/overview").json()
    assert second["cached"] is False  # the all-error payload was NOT memoized
    assert stub.counts["instant"] == 22  # second refresh genuinely recomputed


async def _gather_two(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await asyncio.gather(ac.get("/api/overview"), ac.get("/api/overview"))


def test_overview_single_flight_coalesces_concurrent_refreshes(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_OVERVIEW_CACHE_TTL", "20")
    stub = CountingStub(delay=0.05)  # slow enough that the second call overlaps
    cfg = PromConfig(base_url="http://prom.test:9090")
    app = Starlette(routes=webui.build_ui_routes(stub, cfg))
    r1, r2 = asyncio.run(_gather_two(app))
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["cached"] is False and b2["cached"] is False  # both freshly computed — together
    assert b1["cards"] == b2["cards"]
    assert stub.counts["instant"] == 11  # ONE computation served both (not 22)


# ---------------------------------------------------------------------------
# Label-name validation (client-side, before URL-path interpolation)
# ---------------------------------------------------------------------------


def test_label_name_re_matrix():
    for good in ("namespace", "__name__", "_a1", "pod0", "x"):
        assert LABEL_NAME_RE.match(good), good
    for bad in ("", "a-b", "x;drop", "a/b", "1abc", "a b", "label.name", "a=b", "../../etc"):
        assert not LABEL_NAME_RE.match(bad), bad


def test_client_label_values_rejects_weird_names_before_http():
    def handler_must_not_run(request):
        raise AssertionError(f"request reached the server: {request.url}")

    c = make_prom_client(handler_must_not_run)
    for bad in ("a/b", "x;drop", "", "a-b", "../../etc"):
        with pytest.raises(PrometheusError, match="invalid label name"):
            asyncio.run(c.label_values(bad))


def test_client_label_values_valid_names_reach_the_server():
    seen = []

    def handler(request):
        # /api/v1/label/<name>/values — the interpolated name must be the
        # valid one we passed (no escaping/mangling on the way through).
        parts = request.url.path.split("/")
        assert parts[1:5] == ["api", "v1", "label", "values"] or parts[1:4] == ["api", "v1", "label"]
        name = parts[4]
        seen.append(name)
        assert LABEL_NAME_RE.match(name)
        return httpx.Response(200, json={"status": "success", "data": {"result": ["ns1"]}})

    c = make_prom_client(handler)
    assert asyncio.run(c.label_values("namespace")) == ["ns1"]
    assert asyncio.run(c.label_values("__name__", match="up")) == ["ns1"]
    assert seen == ["namespace", "__name__"]


def test_mcp_prom_label_values_rejects_weird_and_accepts_valid(monkeypatch):
    import server

    class StubClient:
        """Keeps the REAL client's label-name validation (the contract under
        test) while stubbing the HTTP layer beneath it."""

        def __init__(self):
            self._inner = make_prom_client(
                lambda request: httpx.Response(200, json={"status": "success", "data": {"result": ["ns1", "ns2"]}})
            )

        async def label_values(self, label, match=None):
            return await self._inner.label_values(label, match)

    monkeypatch.setattr(server, "client", StubClient())

    async def run(label):
        from mcp.client._memory import InMemoryTransport
        from mcp.client.session import ClientSession

        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("prom_label_values", {"label": label})

    bad = asyncio.run(run("a/b")).content[0].text
    assert bad.startswith("Error: invalid label name")
    assert "a/b" in bad  # the offending name is quoted back
    good = asyncio.run(run("namespace")).content[0].text
    assert "ns1" in good and "ns2" in good


def test_web_label_values_shares_the_client_regex():
    assert webui._LABEL_RE is LABEL_NAME_RE  # one rule, two enforcement points
    cfg = PromConfig(base_url="http://prom.test:9090")

    class StubClient:
        async def label_values(self, label, match=None):
            return ["ns1"]

    c = TestClient(Starlette(routes=webui.build_ui_routes(StubClient(), cfg)))
    for bad in ("a/b", "x;drop", "a b", "1abc"):
        assert c.get("/api/label_values", params={"label": bad}).status_code == 400
    assert c.get("/api/label_values", params={"label": "namespace"}).status_code == 200
