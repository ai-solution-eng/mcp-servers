"""Wave-5 F4 feature tests: saved queries, query hints, dashboard deep-links.

The contracts being pinned (FLEET-EXECUTION-PLAN-2026-09 §9 F4):

* SAVED QUERIES — four MCP tools (query_save / query_list / query_delete /
  query_saved) over one name→{query, params} store. Unset
  PROMETHEUS_SAVED_QUERIES_PATH → in-memory for the session AND the tool
  result says so; set → a durable JSON file written ATOMICALLY (temp file
  in the same directory + fsync + os.replace; a failed write rolls the
  store back and leaves the previous file byte-intact with no temp
  strays). Names are sanitized (lossy → the raw spelling is kept as an
  alias so run/delete accept either). query_saved runs through the EXIST
  ING instant/range paths — the D14 step clamp, the series caps and the
  validation all apply unchanged (proven here against stub clients).
* QUERY HINTS — a capped (3), purely advisory, static-pattern hints list
  appended to prom_query / prom_query_range results: a match-all label
  regex (cardinality), a counter used without rate(), a leading-wildcard
  regex, a bare range selector. Text analysis only (quoted label values
  cannot trip it); include_hints=false suppresses the block entirely, and
  a query with no hints produces byte-identical output either way.
* DEEP LINKS — the web UI keeps the query + time range in the URL
  fragment (#q=…&range=…), restored on load, refreshed after each
  successful run, copied by the 🔗 Link button. The fragment format lives
  in two PURE JS functions inside the page; the tests execute that exact
  block under node (no browser, no JS reimplementation to drift).

No network, no live Prometheus; the MCP tests ride the in-memory
transport (never TestClient-GET /mcp — fleet convention).
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

import saved_queries
import webui
from prom_client import MAX_HINTS, query_hints
from saved_queries import (
    MAX_SAVED_QUERIES,
    SavedQueryStore,
    get_store,
    normalize_params,
    reset_store,
    sanitize_name,
    saved_queries_path,
)

# ---------------------------------------------------------------------------
# Saved queries — name sanitization
# ---------------------------------------------------------------------------


def test_sanitize_name_matrix():
    assert sanitize_name("  cpu   by pod ") == "cpu by pod"  # collapse ws
    assert sanitize_name("err/5xx!") == "err_5xx_"  # unsafe chars → "_"
    assert sanitize_name("tab\tname") == "tab name"
    assert sanitize_name("ünïcode") == "_n_code"  # non-ascii is unsafe too
    assert sanitize_name("x" * 100) == "x" * 64  # length capped
    assert sanitize_name("!!!") == "___"  # punctuation survives as '_' (deterministic)
    for bad in ("", "   ", "  ", None):
        with pytest.raises(ValueError, match="query name is required"):
            sanitize_name(bad)


def test_sanitize_name_is_deterministic():
    # same input → same key (delete/run can rely on it)
    assert sanitize_name("a b/c") == sanitize_name("a b/c")


# ---------------------------------------------------------------------------
# Saved queries — params normalization
# ---------------------------------------------------------------------------


def test_normalize_params_matrix():
    assert normalize_params(None) == {}
    assert normalize_params({}) == {}
    assert normalize_params({"mode": "range", "start": "now-6h", "step": "1m"}) == {
        "mode": "range",
        "start": "now-6h",
        "step": "1m",
    }
    assert normalize_params({"mode": "  instant  "}) == {"mode": "instant"}
    assert normalize_params({"time": None}) == {}  # explicit None → dropped
    for bad in ("range", ["x"], 42):
        with pytest.raises(ValueError, match="params must be an object"):
            normalize_params(bad)
    with pytest.raises(ValueError, match="unknown: bogus"):
        normalize_params({"bogus": "1"})
    with pytest.raises(ValueError, match="params.mode must be"):
        normalize_params({"mode": "banana"})
    with pytest.raises(ValueError, match="must be a string"):
        normalize_params({"start": True})


# ---------------------------------------------------------------------------
# Saved queries — in-memory store CRUD
# ---------------------------------------------------------------------------


def test_in_memory_store_crud_round_trip():
    store = SavedQueryStore(None)  # unset path → in-memory only
    assert store.path is None
    entry = store.save("cpu by pod", "sum by (pod) (rate(x_total[5m]))")
    assert entry["name"] == "cpu by pod" and entry["query"].endswith("[5m]))")
    assert len(store) == 1
    # newest first
    store.save("older-name", "up")
    first = store.save("zzz", "m{}")
    assert [e["name"] for e in store.items()] == ["zzz", "older-name", "cpu by pod"]
    assert store.items()[0] == first
    # get by raw AND by sanitized key
    assert store.get("cpu by pod")["query"].startswith("sum by (pod)")
    assert store.get("nope") is None
    # overwrite (update, not duplicate)
    store.save("cpu by pod", "up")
    assert len(store) == 3
    assert store.get("cpu by pod")["query"] == "up"
    # delete by raw and sanitized spellings
    store.save("weird/name!!", "m")
    assert store.delete("weird/name!!") is True
    assert store.get("weird_name__") is None
    assert store.delete("does-not-exist") is False
    assert len(store) == 3


def test_in_memory_store_params_stored_with_entry():
    store = SavedQueryStore(None)
    entry = store.save("r", "up", {"mode": "range", "start": "now-6h", "step": "1m"})
    assert entry["params"] == {"mode": "range", "start": "now-6h", "step": "1m"}
    assert store.get("r")["params"]["mode"] == "range"


def test_store_rejects_empty_query_and_enforces_cap():
    store = SavedQueryStore(None)
    with pytest.raises(ValueError, match="query is required"):
        store.save("x", "   ")
    for i in range(MAX_SAVED_QUERIES):
        store.save(f"q{i:03d}", "up")
    with pytest.raises(ValueError, match="store is full"):
        store.save("one-too-many", "up")
    # overwriting an existing name still works at the cap
    store.save("q000", "up == 0")
    assert store.get("q000")["query"] == "up == 0"
    assert len(store) == MAX_SAVED_QUERIES


# ---------------------------------------------------------------------------
# Saved queries — durable file mode + atomic write proof
# ---------------------------------------------------------------------------


def test_durable_store_survives_reopen(tmp_path):
    path = tmp_path / "saved.json"
    store = SavedQueryStore(str(path))
    store.save("cpu", "sum(rate(x_total[5m]))", {"mode": "instant"})
    store.save("raw !name", "up", {"mode": "range", "start": "now-1h"})
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 1
    assert {e["name"] for e in on_disk["queries"]} == {"cpu", "raw _name"}
    # a fresh store instance (simulated restart) reads the same entries
    reopened = SavedQueryStore(str(path))
    assert reopened.get("cpu")["query"] == "sum(rate(x_total[5m]))"
    assert reopened.get("raw !name") is not None  # alias rebuilt from raw_name
    assert reopened.get("raw _name") is not None  # sanitized key works too
    # delete rewrites the file
    assert reopened.delete("cpu") is True
    assert "cpu" not in {e["name"] for e in json.loads(path.read_text())["queries"]}


def test_store_write_is_an_atomic_same_dir_replace(tmp_path, monkeypatch):
    path = tmp_path / "saved.json"
    store = SavedQueryStore(str(path))
    store.save("first", "up")
    real_replace = os.replace
    calls = []

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", spy)
    store.save("second", "m")
    monkeypatch.undo()
    assert len(calls) == 1, "every mutation commits via exactly one rename"
    src, dst = calls[0]
    assert dst == str(path)
    assert os.path.dirname(src) == str(tmp_path), "temp file must live in the SAME directory"
    assert os.path.basename(src).startswith(".saved-queries-")
    assert src.endswith(".tmp")
    # the committed file is valid JSON with both entries
    assert {e["name"] for e in json.loads(path.read_text())["queries"]} == {"first", "second"}


def test_failed_write_rolls_back_and_keeps_previous_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "saved.json"
    store = SavedQueryStore(str(path))
    store.save("keep", "up")
    before = path.read_text()

    def disk_full(src, dst):
        raise OSError("No space left on device (simulated)")

    monkeypatch.setattr("os.replace", disk_full)
    with pytest.raises(OSError, match="No space left"):
        store.save("lost", "m")
    with pytest.raises(OSError, match="No space left"):
        store.delete("keep")
    monkeypatch.undo()
    # the previous file is byte-intact and no temp strays remain
    assert path.read_text() == before
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".saved-queries-*")) == []
    # the in-memory store rolled back too (a save that could not persist
    # did not happen)
    assert store.get("lost") is None
    assert store.get("keep") is not None


def test_corrupt_store_file_is_tolerated_and_rewritten(tmp_path):
    path = tmp_path / "saved.json"
    path.write_text("not json {{{")
    store = SavedQueryStore(str(path))
    assert len(store) == 0  # corrupt → empty, not a crash
    store.save("a", "up")
    assert json.loads(path.read_text())["queries"][0]["name"] == "a"


def test_store_file_with_junk_entries_loads_the_valid_ones(tmp_path):
    path = tmp_path / "saved.json"
    path.write_text(
        json.dumps(
            {
                "queries": [
                    {"name": "ok", "query": "up", "params": {"mode": "instant"}, "saved_at": 5},
                    {"query": "no name"},
                    {"name": "no query"},
                    "not even a dict",
                    {"name": "bad-params", "query": "m", "params": {"bogus": 1}},
                ]
            }
        )
    )
    store = SavedQueryStore(str(path))
    assert store.get("ok")["saved_at"] == 5
    assert store.get("bad-params")["params"] == {}  # junk params dropped, entry kept
    assert len(store) == 2


# ---------------------------------------------------------------------------
# Saved queries — env + process-wide accessor
# ---------------------------------------------------------------------------


def test_saved_queries_path_env_matrix(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_PATH", raising=False)
    assert saved_queries_path({}) is None  # unset → in-memory session store
    assert saved_queries_path({"PROMETHEUS_SAVED_QUERIES_PATH": ""}) is None
    assert saved_queries_path({"PROMETHEUS_SAVED_QUERIES_PATH": "  "}) is None
    assert saved_queries_path({"PROMETHEUS_SAVED_QUERIES_PATH": "/tmp/s.json"}) == "/tmp/s.json"


def test_get_store_rebuilds_when_env_path_changes(monkeypatch, tmp_path):
    reset_store()
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_PATH", raising=False)
    memory = get_store()
    assert memory.path is None
    memory.save("in-memory-one", "up")

    path = str(tmp_path / "saved.json")
    monkeypatch.setenv("PROMETHEUS_SAVED_QUERIES_PATH", path)
    durable = get_store()
    assert durable.path == path and len(durable) == 0  # fresh file-backed store
    durable.save("on-disk", "m")
    # back to unset → the session store with its earlier entry
    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_PATH")
    assert get_store().get("in-memory-one") is not None
    reset_store()


# ---------------------------------------------------------------------------
# Saved queries — MCP wire round trip (in-memory transport, stub client)
# ---------------------------------------------------------------------------


class _QueryStub:
    """Records steps; serves a capped instant vector + a tiny matrix."""

    def __init__(self, n_series: int = 30):
        self.steps: list[str] = []
        self.queries: list[str] = []
        self.n_series = n_series

    async def instant_query(self, query, ts=None):
        self.queries.append(query)
        return {
            "resultType": "vector",
            "result": [
                {"metric": {"__name__": "up", "pod": f"p{i}"}, "value": [1757337600, str(i)]}
                for i in range(self.n_series)
            ],
        }

    async def range_query(self, query, start, end, step):
        self.queries.append(query)
        self.steps.append(step)
        return {
            "resultType": "matrix",
            "result": [{"metric": {"__name__": "m", "pod": "p1"}, "values": [["1757337600", "1"]]}],
        }


@pytest.fixture()
def stub_server(monkeypatch):
    """The server module with a stub client + fresh in-memory store."""
    import server

    monkeypatch.delenv("PROMETHEUS_SAVED_QUERIES_PATH", raising=False)
    saved_queries.reset_store()
    server.client = _QueryStub()
    try:
        yield server
    finally:
        saved_queries.reset_store()


def _call(server, tool, args):
    """One tool call over the in-memory transport (fresh session per call —
    the fleet convention; never TestClient-GET /mcp)."""

    async def run():
        from mcp.client._memory import InMemoryTransport
        from mcp.client.session import ClientSession

        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return result.content[0].text

    return asyncio.run(run())


def test_saved_query_wire_crud_in_memory_with_honest_note(stub_server):
    server = stub_server
    out = _call(server, "query_save", {"name": "my test!", "query": "up"})
    assert "my test_" in out and "up" in out
    assert "in-memory only for this session" in out
    assert "PROMETHEUS_SAVED_QUERIES_PATH" in out  # the honest note + how to persist

    listing = _call(server, "query_list", {})
    assert "my test_" in listing and "1 saved query(ies)" in listing
    assert "in-memory only" in listing

    # run by the sanitized name AND the raw spelling (alias)
    for spelling in ("my test_", "my test!"):
        ran = _call(server, "query_saved", {"name": spelling})
        assert "(saved query 'my test_')" in ran
        assert "up{pod=p0} = 0" in ran  # same shape as prom_query

    deleted = _call(server, "query_delete", {"name": "my test!"})
    assert "Deleted 'my test_'" in deleted and "0 remaining" in deleted
    assert "No saved queries yet" in _call(server, "query_list", {})


def test_saved_query_runs_through_existing_instant_path_caps(stub_server):
    server = stub_server
    stub = _QueryStub(n_series=30)
    server.client = stub
    _call(server, "query_save", {"name": "caps", "query": "up"})
    out = _call(server, "query_saved", {"name": "caps"})
    assert "10 more series truncated" in out  # config.max_series cap applied
    assert stub.queries == ["up"]


def test_saved_query_runs_through_existing_range_path_with_clamp(stub_server):
    server = stub_server
    stub = _QueryStub()
    server.client = stub
    _call(
        server,
        "query_save",
        {
            "name": "wide",
            "query": "up",
            "params": {"mode": "range", "start": "0", "end": "86400", "step": "1s"},
        },
    )
    out = _call(server, "query_saved", {"name": "wide"})
    assert stub.steps == ["15s"]  # the D14 floor went upstream, not 1s
    assert "step clamped to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS" in out
    assert "(start=0 end=86400 step=15s)" in out


def test_saved_query_params_override_and_defaults(stub_server):
    server = stub_server
    stub = _QueryStub()
    server.client = stub
    _call(server, "query_save", {"name": "both", "query": "up", "params": {"mode": "range"}})
    # saved defaults: mode range, no start/end/step → range defaults apply
    out = _call(server, "query_saved", {"name": "both"})
    assert stub.steps, "range mode from saved params was honored"
    # per-call override wins over saved params
    stub.steps.clear()
    out = _call(
        server,
        "query_saved",
        {
            "name": "both",
            "params": {"mode": "range", "start": "0", "end": "86400", "step": "20s"},
        },
    )
    assert stub.steps == ["20s"]  # above the floor → passes through unclamped
    # instant override of a range-saved query
    stub.steps.clear()
    stub.queries.clear()
    out = _call(server, "query_saved", {"name": "both", "params": {"mode": "instant"}})
    assert "up{pod=p0}" in out and stub.steps == []
    # bad params → honest error, nothing run
    out = _call(server, "query_saved", {"name": "both", "params": {"bogus": "1"}})
    assert out.startswith("Error:") and "unknown: bogus" in out


def test_saved_query_missing_name_is_a_clear_error(stub_server):
    server = stub_server
    out = _call(server, "query_saved", {"name": "ghost"})
    assert out == "Error: no saved query named 'ghost' (query_list shows what exists)."
    out = _call(server, "query_delete", {"name": "ghost"})
    assert out.startswith("Error: no saved query named 'ghost'")


def test_saved_query_durable_mode_via_wire(monkeypatch, stub_server):
    server = stub_server
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="f4-durable-"))
    try:
        monkeypatch.setenv("PROMETHEUS_SAVED_QUERIES_PATH", str(tmp / "saved.json"))
        saved_queries.reset_store()
        out = _call(server, "query_save", {"name": "durable", "query": "up"})
        assert f"durable: {tmp / 'saved.json'}" in out  # the honest durable note
        assert (tmp / "saved.json").exists()
        saved_queries.reset_store()  # simulate a restart: fresh store from disk
        ran = _call(server, "query_saved", {"name": "durable"})
        assert "up{pod=p0} = 0" in ran
        listing = _call(server, "query_list", {})
        assert "durable" in listing and "durable:" in listing
    finally:
        saved_queries.reset_store()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Query hints — pure function
# ---------------------------------------------------------------------------


def test_hint_match_all_regex_fires():
    hints = query_hints('up{pod=~".*"}')
    assert len(hints) == 1
    assert hints[0].startswith("[cardinality]")
    assert 'pod=~".*"' in hints[0] and "narrow" in hints[0]
    # single-quoted matchers and anchored match-alls fire too
    assert query_hints("m{pod=~'.*'}")[0].startswith("[cardinality]")
    assert query_hints('m{pod=~"^.*$"}')[0].startswith("[cardinality]")
    assert query_hints('m{pod=~".+"}')[0].startswith("[cardinality]")


def test_hint_counter_without_rate_fires():
    hints = query_hints("http_requests_total")
    assert len(hints) == 1 and hints[0].startswith("[counter]")
    assert "rate(http_requests_total[5m])" in hints[0]
    # _count convention covered as well
    assert query_hints("x_count")[0].startswith("[counter]")
    # rate/increase/over-time consumers stay silent
    for q in (
        "rate(http_requests_total[5m])",
        "increase(x_total[1h])",
        'sum by (pod) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))',
        "avg_over_time(x_total[1h])",
        "topk(10, increase(kube_pod_container_status_restarts_total[1h]))",
        "histogram_quantile(0.9, rate(x_total[5m]))",
    ):
        assert not [h for h in query_hints(q) if h.startswith("[counter]")], q


def test_hint_leading_wildcard_regex_fires():
    hints = query_hints('m{pod=~".*myapp.*"}')
    assert len(hints) == 1 and hints[0].startswith("[regex]")
    assert 'pod=~".*myapp.*"' in hints[0] and "anchor" in hints[0]
    # anchored / literal-prefixed patterns stay silent
    assert query_hints('m{pod=~"myapp-.*"}') == []
    assert query_hints('m{pod=~"^myapp"}') == []
    assert query_hints('m{status=~"5.."}') == []


def test_hint_bare_range_selector_fires():
    hints = query_hints("http_requests_total[5m]")
    tags = [h.split("]")[0] + "]" for h in hints]
    assert "[counter]" in tags and "[range-vector]" in tags  # both apply
    assert any("rate(metric[5m])" in h for h in hints)
    # matcher-block form fires too
    assert any(h.startswith("[range-vector]") for h in query_hints('m{a="b"}[6h]'))
    # over-time functions keep it silent
    assert not [h for h in query_hints("avg_over_time(m[1h])") if h.startswith("[range-vector]")]


def test_hints_capped_at_three():
    # a query hitting ALL FOUR heuristics is truncated to MAX_HINTS
    four = 'foo_total{pod=~".*",dep=~".*x.*"}[5m]'
    hints = query_hints(four)
    assert len(query_hints(four)) == MAX_HINTS == 3
    assert hints[0].startswith("[cardinality]")
    assert hints[1].startswith("[counter]")
    assert hints[2].startswith("[regex]")  # the 4th ([range-vector]) was cut


def test_hints_do_not_fire_on_label_values_or_healthy_queries():
    assert query_hints("") == []
    assert query_hints("   ") == []
    assert query_hints("up") == []
    assert query_hints("up == 0") == []
    assert query_hints("count(up == 1)") == []
    assert query_hints('m{pod="literal"}') == []  # exact matcher, not a regex
    assert query_hints('m{job="node"}') == []
    # a _total inside a quoted label VALUE must not read as a counter
    assert query_hints('errors{kind="user_total"}') == []
    # the UI's own presets stay hint-free (they are the good examples)
    for q in (
        'sum by (pod) (container_memory_working_set_bytes{pod=~"myapp-.*",container!=""})',
        '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "DCGM_FI_DEV_GPU_UTIL",
        'up{job=~"prometheus"}',
    ):
        assert query_hints(q) == [], q


# ---------------------------------------------------------------------------
# Query hints — MCP tool surface
# ---------------------------------------------------------------------------


def test_prom_query_appends_hints_block_by_default(stub_server):
    server = stub_server
    server.client = _QueryStub(n_series=1)
    default = _call(server, "prom_query", {"query": 'up{pod=~".*"}'})
    assert "Hints (advisory — pattern-matched from the query, may not apply):" in default
    assert default.count("\n  - [") == 1
    assert default.splitlines()[0] == "  up{pod=p0} = 0 (at 1757337600)"  # body unchanged
    suppressed = _call(server, "prom_query", {"query": 'up{pod=~".*"}', "include_hints": False})
    assert "Hints" not in suppressed
    assert suppressed == "  up{pod=p0} = 0 (at 1757337600)"


def test_prom_query_range_hints_come_after_the_clamp_notice(stub_server):
    server = stub_server
    server.client = _QueryStub()
    out = _call(
        server,
        "prom_query_range",
        {
            "query": "http_requests_total[5m]",
            "start": "0",
            "end": "86400",
            "step": "1s",
        },
    )
    clamp_at = out.index("step clamped to 15s")
    hints_at = out.index("Hints (advisory")
    assert clamp_at < hints_at  # clamp notice stays in the header zone
    assert out.count("\n  - [") == 2  # counter + range-vector, capped set
    no_hints = _call(
        server,
        "prom_query_range",
        {
            "query": "http_requests_total[5m]",
            "start": "0",
            "end": "86400",
            "step": "1s",
            "include_hints": False,
        },
    )
    assert "Hints" not in no_hints and "step clamped to 15s" in no_hints


def test_hints_absent_queries_are_byte_identical_either_way(stub_server):
    server = stub_server
    server.client = _QueryStub(n_series=2)
    for args in ({"query": "up"}, {"query": "up", "start": "0", "end": "60"}):
        tool = "prom_query_range" if "start" in args else "prom_query"
        a = _call(server, tool, dict(args, include_hints=True))
        b = _call(server, tool, dict(args, include_hints=False))
        assert a == b, "hint-free queries must be byte-identical with hints on or off"


def test_query_saved_hints_follow_the_same_switch(stub_server):
    server = stub_server
    server.client = _QueryStub(n_series=1)
    _call(server, "query_save", {"name": "h", "query": 'up{pod=~".*"}'})
    with_hints = _call(server, "query_saved", {"name": "h"})
    without = _call(server, "query_saved", {"name": "h", "include_hints": False})
    assert "[cardinality]" in with_hints and "Hints" not in without


# ---------------------------------------------------------------------------
# Deep links — the page's pure JS block, executed under node
# ---------------------------------------------------------------------------

_DEEPLINK_BLOCK_RE = re.compile(r'<script id="deeplink-core">(.*?)</script>', re.DOTALL)

_NODE_HARNESS = textwrap.dedent(
    """
    function eq(a, b, msg) {
      const ja = JSON.stringify(a), jb = JSON.stringify(b);
      if (ja !== jb) throw new Error(msg + " | got " + ja + " want " + jb);
    }
    // 1. the mission's literal example shape
    eq(deepLinkEncode({ q: "up", mode: "instant", t: "now" }), "#q=up&range=now", "instant fragment");
    eq(deepLinkDecode("#q=up&range=now"), { mode: "instant", q: "up", t: "now" }, "instant decode");
    // 2. round-trips (special chars, regexes, newlines, RFC3339, empty step)
    const states = [
      { mode: "instant", q: "up", t: "now" },
      { mode: "instant", q: 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (pod)', t: "now-6h" },
      { mode: "instant", q: 'up{foo="a&b=c?d#e"}', t: "2026-09-08T00:00:00Z" },
      { mode: "instant", q: "line1\\nline2 \\"quoted\\"", t: "now-30m" },
      { mode: "range", q: 'container_memory_working_set_bytes{pod=~"myapp-.*",container!=""}', start: "now-6h", end: "now", step: "2m" },
      { mode: "range", q: 'rate(x_total[5m])', start: "2026-09-08T00:00:00Z", end: "now", step: "" },
      { mode: "range", q: "up", start: "0", end: "86400", step: "15s" },
    ];
    for (const s of states) {
      const frag = deepLinkEncode(s);
      if (frag.charAt(0) !== "#" || !frag.includes("q=") || !frag.includes("range="))
        throw new Error("fragment must be #q=…&range=… : " + frag);
      const back = deepLinkDecode(frag);
      if (s.mode === "instant") {
        eq([back.mode, back.q, back.t], ["instant", s.q, s.t], "instant round-trip");
      } else {
        eq([back.mode, back.q, back.start, back.end, back.step],
           ["range", s.q, s.start, s.end, s.step], "range round-trip");
      }
    }
    // 3. space survives the URLSearchParams +/+ round trip
    eq(deepLinkDecode(deepLinkEncode({ mode: "instant", q: "a b", t: "now" })).q, "a b", "space");
    // 4. refusals: no fragment / missing pieces / malformed
    for (const bad of ["", "#", "q=up", "#range=now", "#q=", "#q=up&range=",
                       "#q=up&range=a..b..c", "not a fragment at all"]) {
      if (deepLinkDecode(bad) !== null) throw new Error("should refuse: " + JSON.stringify(bad));
    }
    // 5. unknown params are ignored, not fatal
    eq(deepLinkDecode("#q=up&range=now&junk=1").q, "up", "extra params ignored");
    // 6. range without step decodes to an empty step (auto resolution)
    eq(deepLinkDecode("#q=up&range=now-3h..now").step, "", "missing step -> auto");
    console.log("DEEPLINK-ALL-OK");
    """
)


def _deeplink_js() -> str:
    html = webui._load_html()
    m = _DEEPLINK_BLOCK_RE.search(html)
    assert m, "the page must carry the pure deeplink-core script block"
    return m.group(1)


def test_deeplink_fragment_round_trip_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the pure-JS block cannot be executed here")
    js = _deeplink_js()
    assert "function deepLinkEncode" in js and "function deepLinkDecode" in js
    proc = subprocess.run(
        [node, "-e", js + "\n" + _NODE_HARNESS],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    assert "DEEPLINK-ALL-OK" in proc.stdout


def test_deeplink_core_block_is_dom_free_and_escape_clean():
    js = _deeplink_js()
    # pure: no DOM access at all (the DOM work lives in the main script)
    for banned in ("document", "innerHTML", "eval(", "new Function", "localStorage"):
        assert banned not in js, f"deeplink-core must stay DOM-free ({banned})"


def test_deeplink_page_wiring():
    html = webui._load_html()
    # the affordance exists next to Save, in the query actions row
    assert 'id="copy-link"' in html and "🔗 Link" in html
    # restored on load (and it runs the restored query)
    assert "restoreDeepLink();" in html
    assert "deepLinkDecode(location.hash)" in html
    assert 'switchTab("query");' in html
    # refreshed after a successful run via replaceState (no reload)
    assert "history.replaceState" in html and "updateDeepLink();" in html
    # clipboard copy goes through the existing toast
    assert "Link copied" in html


def test_deeplink_restore_only_uses_value_assignments():
    """Escape-clean: decode output flows into .value / literals — never into
    innerHTML or a selector string."""
    html = webui._load_html()
    m = re.search(r"function applyDeepLink\(st\) \{(.*?)\n\}", html, re.DOTALL)
    assert m, "applyDeepLink must be extractable"
    body = m.group(1)
    assert "innerHTML" not in body and "insertAdjacentHTML" not in body
    assert body.count(".value =") >= 4  # promql + the time/range inputs
    # st.mode only reaches setMode via the fixed literals decode returns
    assert "setMode(st.mode)" in body


def test_deeplink_core_block_is_referenced_by_a_unique_script_id():
    html = webui._load_html()
    assert len(_DEEPLINK_BLOCK_RE.findall(html)) == 1
    ids = re.findall(r'id="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "no duplicate element ids may be introduced"


def test_all_page_script_blocks_parse_under_node():
    """The main page script is never executed by the tests — at least pin
    that every inline <script> block still PARSES (syntax-level safety net
    for the deep-link edits)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — script blocks cannot be parse-checked here")
    html = webui._load_html()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(blocks) >= 2, "theme pre-paint + main script expected"
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        tmp_name = fh.name
    try:
        for i, block in enumerate(blocks):
            with open(tmp_name, "w", encoding="utf-8") as fh:
                fh.write(block)
            proc = subprocess.run(
                [node, "--check", tmp_name],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert proc.returncode == 0, f"script block {i} does not parse:\n{proc.stderr}"
    finally:
        os.unlink(tmp_name)


def test_saved_queries_module_is_packaged_everywhere():
    """saved_queries.py must ship in EVERY packaging path — the source tree
    import works implicitly, but a missing py-modules / Dockerfile COPY /
    MANIFEST entry makes the installed image crash at first import."""
    import saved_queries

    root = os.path.dirname(os.path.abspath(saved_queries.__file__))
    pyproject = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    assert '"saved_queries"' in pyproject, "add saved_queries to [tool.setuptools] py-modules"
    dockerfile = open(os.path.join(root, "Dockerfile"), encoding="utf-8").read()
    assert "saved_queries.py ./" in dockerfile, "add saved_queries.py to the Dockerfile COPY line"
    manifest = open(os.path.join(root, "MANIFEST.in"), encoding="utf-8").read()
    assert "saved_queries.py" in manifest, "ship the module in sdists too"
