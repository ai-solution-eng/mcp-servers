"""Unit tests for the SearXNG MCP server (no network required).

Run:  .venv/bin/python -m pytest tests/ -v
Live end-to-end check (needs a reachable SearXNG): see tests/live_check.py
"""

import asyncio
import time
from typing import Any

import httpx2
import pytest

import server
from fetcher import WebContentFetcher, clean_markdown_cruft, extract_via_bs4
from searxng_client import (
    SearXNGClient,
    SearXNGError,
    engines_from_config_hint,
    format_search_response,
    normalize_language,
)

SAMPLE_RESPONSE = {
    "query": "python mcp",
    "results": [
        {
            "title": "MCP Python SDK",
            "url": "https://github.com/modelcontextprotocol/python-sdk",
            "content": "The official Python SDK for Model Context Protocol.",
            "engine": "google cse",
            "engines": ["google cse"],
        },
        {
            "title": "SearXNG docs",
            "url": "https://docs.searxng.org/",
            "content": "SearXNG is a privacy-respecting metasearch engine.",
            "engine": "bing",
            "engines": ["bing", "qwant"],
        },
    ],
    "answers": [],
    "corrections": [],
    "suggestions": ["python mcp server"],
    "infoboxes": [],
    "unresponsive_engines": [["brave", "timeout"]],
    "number_of_results": 4123,
}


def make_transport(responder) -> httpx2.MockTransport:
    return httpx2.MockTransport(responder)


def make_client(responder, **kwargs) -> SearXNGClient:
    defaults: dict[str, Any] = {"base_url": "http://searxng.test:8080", "requests_per_minute": 1000}
    defaults.update(kwargs)
    return SearXNGClient(transport=make_transport(responder), **defaults)


# ---------------------------------------------------------------------------
# Language / region normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("wt-wt", "all"),
        ("all", "all"),
        # ddgs style <country>-<lang>
        ("us-en", "en-US"),
        ("uk-en", "en-GB"),
        ("fr-fr", "fr-FR"),
        ("de-de", "de-DE"),
        # native SearXNG locales pass through
        ("en-US", "en-US"),
        ("zh-CN", "zh-CN"),
        ("zh-cn", "zh-CN"),
        ("pt-BR", "pt-BR"),
        # bare language codes
        ("en", "en"),
        ("de", "de"),
    ],
)
def test_normalize_language(raw, expected):
    assert normalize_language(raw) == expected


# ---------------------------------------------------------------------------
# Client: parameter mapping and parsing
# ---------------------------------------------------------------------------


def test_search_maps_params_and_parses():
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, default_language="en-US")
    resp = asyncio.run(
        client.search(
            "python mcp",
            language="us-en",
            engines="google,bing",
            categories="it",
            safesearch=1,
            time_range="week",
            pageno=2,
        )
    )

    assert captured["q"] == "python mcp"
    assert captured["format"] == "json"
    assert captured["language"] == "en-US"  # ddgs style converted
    assert captured["engines"] == "google,bing"
    assert captured["categories"] == "it"
    assert captured["safesearch"] == "1"
    assert captured["time_range"] == "week"
    assert captured["pageno"] == "2"

    assert [r.title for r in resp.results] == ["MCP Python SDK", "SearXNG docs"]
    assert resp.results[0].url == "https://github.com/modelcontextprotocol/python-sdk"
    assert resp.results[0].position == 1
    assert resp.results[0].engines == ["google cse"]
    assert resp.results[1].engines == ["bing", "qwant"]
    assert resp.suggestions == ["python mcp server"]
    assert resp.unresponsive_engines == [("brave", "timeout")]
    assert resp.number_of_results == 4123


def test_search_auto_omits_engines():
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler)
    asyncio.run(client.search("test"))
    assert "engines" not in captured
    assert captured["language"] == "en-US"  # default applied
    assert captured["categories"] == "general"


def test_search_403_gives_json_format_hint():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, text="Forbidden")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match="json"):
        asyncio.run(client.search("test"))


def test_search_400_retries_without_language():
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(dict(request.url.params))
        if "language" in request.url.params:
            return httpx2.Response(400, text="bad language")
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler)
    resp = asyncio.run(client.search("test", language="xx-xx"))
    assert len(calls) == 2
    assert "language" not in calls[1]
    assert len(resp.results) == 2


# ---------------------------------------------------------------------------
# 429 Retry-After (spec: honor the header; per-process cooldown, no valkey)
# ---------------------------------------------------------------------------


def test_429_with_retry_after_seconds_reports_delay_and_cooldowns():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "17"}, text="slow down")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match=r"429.*retry after ~17s"):
        asyncio.run(client.search("test"))
    # The error also pushed a cooldown into the per-process RateLimiter, so
    # local admission pauses until the server's window resets.
    assert client.rate_limiter._cooldown_until > time.monotonic()


def test_429_with_http_date_retry_after_parses_delay():
    from email.utils import formatdate

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": formatdate(time.time() + 25, usegmt=True)})

    client = make_client(handler)
    with pytest.raises(SearXNGError, match=r"429.*retry after ~\d+s"):
        asyncio.run(client.search("test"))
    assert client.rate_limiter._cooldown_until > time.monotonic()


def test_429_without_retry_after_keeps_generic_message():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, text="slow down")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match=r"429 \(rate limited\); slow down\.$"):
        asyncio.run(client.search("test"))
    # No header → no cooldown: nothing invented to wait on.
    assert client.rate_limiter._cooldown_until <= time.monotonic()


def test_429_garbage_retry_after_is_tolerated():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "soon-ish"}, text="slow down")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match=r"429 \(rate limited\); slow down\.$"):
        asyncio.run(client.search("test"))
    assert client.rate_limiter._cooldown_until <= time.monotonic()


def test_429_huge_retry_after_is_clamped_for_admission():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "86400"}, text="come back tomorrow")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match=r"retry after ~86400s"):
        asyncio.run(client.search("test"))
    # The message reports the raw value, but local admission is clamped:
    # blocking the process for a day on one header would hurt more than the
    # saved request.
    remaining = client.rate_limiter._cooldown_until - time.monotonic()
    assert 0 < remaining <= SearXNGClient.MAX_COOLDOWN_SECONDS + 1


def test_429_infinite_retry_after_is_tolerated_not_fatal():
    """Retry-After: inf parses as float('inf') — it must be treated as
    unparseable garbage (generic 429 message), never an OverflowError."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "inf"}, text="slow down")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match=r"429 \(rate limited\); slow down\.$"):
        asyncio.run(client.search("test"))
    assert client.rate_limiter._cooldown_until <= time.monotonic()


def test_engine_names_cache_failure_returns_none_and_retries_later():
    """A failed /config fetch leaves the cache UNKNOWN (None), so a later
    call retries — no False sentinel pinned for the life of the process."""
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(500, text="boom")
        return httpx2.Response(200, json={"engines": [{"name": "google"}, {"name": "bing"}]})

    client = make_client(handler)
    assert asyncio.run(client._engine_names_from_config()) is None  # failure
    assert client._engine_names is None  # unknown, not a pinned sentinel
    names = asyncio.run(client._engine_names_from_config())  # retried, succeeds
    assert names == {"google", "bing"}
    assert calls["n"] == 2
    # and a successful empty engine list is ALSO unknown (nothing to hint on)
    def empty_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"engines": []})
    client2 = make_client(empty_handler)
    assert asyncio.run(client2._engine_names_from_config()) is None


def test_cooldown_delays_next_acquire():
    from searxng_client import RateLimiter

    rl = RateLimiter(1000)
    rl.push_cooldown(0.15)
    start = time.monotonic()
    asyncio.run(rl.acquire())
    assert time.monotonic() - start >= 0.1


def test_search_unreachable_raises():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match="could not reach SearXNG"):
        asyncio.run(client.search("test"))


def test_search_404_not_retried_and_named():
    """A 404 from a misconfigured instance is a request problem: raise the
    specific status once — never retried (identically) then reported
    generically like a 400."""
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(dict(request.url.params))
        return httpx2.Response(404, text="no such endpoint")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match="404") as exc_info:
        asyncio.run(client.search("test", language="en-US"))
    assert len(calls) == 1  # exactly one upstream call — no blind retry
    assert "not retrying" in str(exc_info.value)


def test_search_401_not_retried_and_named():
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(dict(request.url.params))
        return httpx2.Response(401, text="unauthorized")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match="401"):
        asyncio.run(client.search("test"))
    assert len(calls) == 1


def test_search_400_still_retryable_only_4xx():
    """400 remains the ONLY 4xx that returns None (retryable) from
    _request — contract of the language-drop retry."""

    async def exercise():
        data = await client._request({"q": "x"})
        assert data is None

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, text="bad language")

    client = make_client(handler)
    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# Output formatting (ddgs-lite layout parity)
# ---------------------------------------------------------------------------


def test_format_response_layout():
    resp = SearXNGClient._parse(SAMPLE_RESPONSE, "python mcp")
    out = format_search_response(resp, max_results=10)

    assert out.startswith("Found 2 search results:\n")
    assert "1. MCP Python SDK" in out
    assert "   URL: https://github.com/modelcontextprotocol/python-sdk" in out
    assert "   Summary: The official Python SDK" in out
    assert "Related searches: python mcp server" in out
    assert "some engines did not respond: brave (timeout)" in out
    assert "Source engines: bing, google cse, qwant" in out


def test_format_response_max_results_slice():
    resp = SearXNGClient._parse(SAMPLE_RESPONSE, "q")
    out = format_search_response(resp, max_results=1)
    assert out.startswith("Found 1 search results:\n")
    assert "2. SearXNG docs" not in out


def test_format_response_empty():
    resp = SearXNGClient._parse({"results": []}, "q")
    out = format_search_response(resp, 10)
    # The leading sentence is BYTE-STABLE (things grep it); the empty branch
    # now APPENDS explicit next-step guidance after it.
    assert out.startswith("No results were found. Try rephrasing your search query.")
    assert "do not re-run the identical query" in out
    assert "change category, region, or time_range" in out

    resp_unresponsive = SearXNGClient._parse({"results": [], "unresponsive_engines": [["bing", "timeout"]]}, "q")
    assert "bing" in format_search_response(resp_unresponsive, 10)


def test_format_response_empty_surfaces_corrections_and_suggestions():
    """The exact retry-loop state: empty results WITH a correction/suggestion
    available — the formatter used to drop both."""
    resp = SearXNGClient._parse(
        {"results": [], "corrections": ["python mpc"], "suggestions": ["python mcp server", "python sdk"]},
        "q",
    )
    out = format_search_response(resp, 10)
    assert "Did you mean: python mpc?" in out
    assert "Related searches: python mcp server | python sdk" in out
    # Guidance is appended even when corrections/suggestions exist.
    assert "do not re-run the identical query" in out
    # Byte-stable leading sentence still opens the output.
    assert out.startswith("No results were found.")


def test_format_response_empty_unresponsive_note_stays_before_guidance():
    resp = SearXNGClient._parse(
        {
            "results": [],
            "corrections": ["pyton"],
            "unresponsive_engines": [["brave", "suspended"]],
        },
        "q",
    )
    out = format_search_response(resp, 10)
    suspended_note = out.index("temporarily suspended/rate-limited")
    correction = out.index("Did you mean: pyton?")
    guidance = out.index("do not re-run the identical query")
    # Existing unresponsive note stays; new content appends after it.
    assert suspended_note < correction < guidance


def test_format_response_answers_and_corrections():
    data = dict(
        SAMPLE_RESPONSE,
        answers=["42"],
        corrections=["python mpc"],
        suggestions=[],
        unresponsive_engines=[],
    )
    resp = SearXNGClient._parse(data, "q")
    out = format_search_response(resp, 10)
    assert "Answer: 42" in out
    assert "Did you mean: python mpc?" in out


# ---------------------------------------------------------------------------
# backend engine-name sanity (empty search + typo'd engine allowlist)
# ---------------------------------------------------------------------------


CONFIG_RESPONSE = {
    "engines": [
        {"name": "google", "enabled": True},
        {"name": "brave.images", "enabled": True},
        {"name": "wikipedia", "enabled": True},
    ]
}


def test_engine_hint_no_match():
    hint = engines_from_config_hint("braveimages", {"google", "brave.images", "wikipedia"})
    assert "braveimages" in hint
    assert "none match SearXNG's engine list" in hint
    assert "'brave.images', not 'braveimages'" in hint


def test_engine_hint_match_or_unknown_config_is_silent():
    # Any named engine existing → no hint (partial matches still search).
    assert engines_from_config_hint("google,braveimages", {"google"}) == ""
    # Unknown engine list (/config failed) → no hint, never a false alarm.
    assert engines_from_config_hint("braveimages", None) == ""
    assert engines_from_config_hint("braveimages", set()) == ""
    # auto / empty engines param → no hint.
    assert engines_from_config_hint("", {"google"}) == ""


def test_empty_backend_search_surfaces_engine_hint_end_to_end():
    """backend='braveimages' (typo) returns zero results → the formatter
    appends the /config-based spelling hint."""
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url.path))
        if request.url.path == "/config":
            return httpx2.Response(200, json=CONFIG_RESPONSE)
        return httpx2.Response(200, json={"results": [], "query": "q"})

    client = make_client(handler)

    async def run():
        resp = await client.search("q", engines="braveimages")
        return format_search_response(
            resp, 10, engine_hint=engines_from_config_hint("braveimages", await client._engine_names_from_config())
        )

    out = asyncio.run(run())
    assert "/config" in calls  # fetched once for the engine list
    assert out.startswith("No results were found.")
    assert "none match SearXNG's engine list" in out
    assert "brave.images" in out


def test_engine_names_from_config_cached_and_failure_tolerated():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/config":
            return httpx2.Response(200, json=CONFIG_RESPONSE)
        return httpx2.Response(200, json={"results": []})

    client = make_client(handler)
    names = asyncio.run(client._engine_names_from_config())
    assert names == {"google", "brave.images", "wikipedia"}
    # Second call serves the cache without a second fetch (same handler, but
    # assert via the sentinel: swap _engine_names and re-call).
    client._engine_names = {"cached-only"}
    assert asyncio.run(client._engine_names_from_config()) == {"cached-only"}

    # Failure tolerance: a 500 /config or a transport error → None, silently.
    def failing_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/config":
            return httpx2.Response(500, text="boom")
        return httpx2.Response(200, json={"results": []})

    failing = make_client(failing_handler)
    assert asyncio.run(failing._engine_names_from_config()) is None

    def raising_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    raising = make_client(raising_handler)
    assert asyncio.run(raising._engine_names_from_config()) is None


def test_mcp_search_backend_typo_end_to_end_mocked(monkeypatch):
    """Full MCP path: search(backend='braveimages') on an empty hit appends
    the engine-name hint to the tool output."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/config":
            return httpx2.Response(200, json=CONFIG_RESPONSE)
        return httpx2.Response(200, json={"results": [], "query": "q"})

    real_client = make_client(handler)
    monkeypatch.setattr(server, "searcher", real_client)

    async def run():
        return await server.search(
            query="anything",
            ctx=DummyCtx(),
            backend="braveimages",
        )

    out = asyncio.run(run())
    assert out.startswith("No results were found.")
    assert "none match SearXNG's engine list" in out


# ---------------------------------------------------------------------------
# Fetcher: extraction + pagination (network mocked away)
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<html><head><title>T</title><style>body{color:red}</style></head>
<body>
<nav>Menu</nav>
<main><h1>Hello World</h1>
<p>This is <a href="https://example.com">a link</a> and some content.</p>
<ul><li>one</li><li>two</li></ul>
</main>
<footer>foot</footer>
<script>alert(1)</script>
</body></html>
"""


def test_bs4_extraction_strips_chrome():
    text = extract_via_bs4(SAMPLE_HTML)
    assert text is not None
    assert "Hello World" in text
    assert "https://example.com" in text
    assert "one" in text
    assert "Menu" not in text
    assert "alert(1)" not in text
    assert "foot" not in text


class DummyCtx:
    def __init__(self):
        self.messages = []

    async def info(self, msg):
        self.messages.append(msg)

    async def error(self, msg):
        self.messages.append(msg)


def make_fetcher(monkeypatch, html_body):
    fetcher = WebContentFetcher(requests_per_minute=1000)

    async def fake_get_html(url):
        return html_body

    monkeypatch.setattr(fetcher, "_get_html", fake_get_html)
    return fetcher


def test_fetch_and_parse_bs4_backend(monkeypatch):
    fetcher = make_fetcher(monkeypatch, SAMPLE_HTML)
    ctx = DummyCtx()
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", ctx, backend="bs4"))
    assert "Hello World" in out
    assert "(via bs4+html2text)]" in out


def test_fetch_and_parse_pagination(monkeypatch):
    body = "<main><p>" + ("word " * 3000) + "</p></main>"
    fetcher = make_fetcher(monkeypatch, body)
    ctx = DummyCtx()
    first = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", ctx, backend="bs4"))
    assert "Showing characters 0-8000" in first
    assert "Use start_index=8000 to see more" in first

    second = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", ctx, start_index=8000, backend="bs4"))
    assert "Showing characters 8000-" in second


def test_fetch_and_parse_unreachable(monkeypatch):
    fetcher = WebContentFetcher(requests_per_minute=1000)

    async def fake_get_html(url):
        return None

    async def fake_impersonated(url):
        fetcher.last_fetch_error = "403 (simulated)"

    monkeypatch.setattr(fetcher, "_get_html", fake_get_html)
    monkeypatch.setattr(fetcher, "_get_html_impersonated", fake_impersonated)
    ctx = DummyCtx()
    out = asyncio.run(fetcher.fetch_and_parse("https://blocked.example/", ctx))
    assert out.startswith("Error: Could not access the webpage")
    assert "last attempt: 403 (simulated)" in out  # telemetry included


def test_fetch_escalates_to_impersonated(monkeypatch):
    """auto backend: plain fetch fails -> curl_cffi escalation -> success."""
    fetcher = WebContentFetcher(requests_per_minute=1000)
    calls = {"plain": 0, "impersonated": 0}

    async def fake_plain(url):
        calls["plain"] += 1
        fetcher.last_fetch_error = "403 (simulated)"

    async def fake_impersonated(url):
        calls["impersonated"] += 1
        fetcher.last_fetch_error = None
        return SAMPLE_HTML

    monkeypatch.setattr(fetcher, "_get_html", fake_plain)
    monkeypatch.setattr(fetcher, "_get_html_impersonated", fake_impersonated)
    ctx = DummyCtx()
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", ctx, backend="auto"))
    assert "Hello World" in out
    assert calls == {"plain": 1, "impersonated": 1}


def test_fetch_curl_backend_skips_plain(monkeypatch):
    """backend='curl' goes straight to the impersonated fetch."""
    fetcher = WebContentFetcher(requests_per_minute=1000)
    calls = {"plain": 0, "impersonated": 0}

    async def fake_plain(url):
        calls["plain"] += 1
        return SAMPLE_HTML

    async def fake_impersonated(url):
        calls["impersonated"] += 1
        return SAMPLE_HTML

    monkeypatch.setattr(fetcher, "_get_html", fake_plain)
    monkeypatch.setattr(fetcher, "_get_html_impersonated", fake_impersonated)
    ctx = DummyCtx()
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", ctx, backend="curl"))
    assert "Hello World" in out
    assert calls["plain"] == 0 and calls["impersonated"] == 1


def test_clean_markdown_cruft():
    assert clean_markdown_cruft("a\n\n\n\n\nb") == "a\n\nb"


# ---------------------------------------------------------------------------
# Search result cache (TTL + single-flight)
# ---------------------------------------------------------------------------


def test_search_cache_hit_within_ttl():
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, cache_ttl_seconds=120)
    r1 = asyncio.run(client.search("python mcp"))
    r2 = asyncio.run(client.search("python mcp"))
    assert calls["n"] == 1  # second search served from cache
    assert r2.cache_hit is True and r1.cache_hit is False
    assert r2.results == r1.results
    assert client.result_cache.hits == 1 and client.result_cache.misses == 1


def test_search_cache_key_includes_params():
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, cache_ttl_seconds=120)
    asyncio.run(client.search("python mcp"))
    asyncio.run(client.search("python mcp", categories="it"))
    asyncio.run(client.search("python mcp", pageno=2))
    asyncio.run(client.search("different query"))
    assert calls["n"] == 4  # every param/query variation is a distinct entry


def test_search_cache_disabled_with_zero_ttl():
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, cache_ttl_seconds=0)
    asyncio.run(client.search("python mcp"))
    asyncio.run(client.search("python mcp"))
    assert calls["n"] == 2


def test_search_cache_expires(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, cache_ttl_seconds=120)
    asyncio.run(client.search("python mcp"))
    # Age every entry past the TTL (fake clock keeps this fast).
    real_monotonic = time.monotonic
    clock = {"offset": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + clock["offset"])
    clock["offset"] = 121.0
    asyncio.run(client.search("python mcp"))
    assert calls["n"] == 2


def test_search_cache_errors_not_memoized():
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(500)
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, cache_ttl_seconds=120)
    with pytest.raises(SearXNGError):
        asyncio.run(client.search("python mcp"))
    resp = asyncio.run(client.search("python mcp"))
    assert calls["n"] == 2  # the failure was coalesced but NOT cached
    assert resp.cache_hit is False


def test_search_cache_single_flight():
    """Concurrent identical searches share ONE upstream fan-out."""
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler, cache_ttl_seconds=120)

    async def burst():
        return await asyncio.gather(*(client.search("python mcp") for _ in range(5)))

    results = asyncio.run(burst())
    assert calls["n"] == 1  # all five callers shared one upstream run
    assert all(r.results for r in results)
    # Coalesced waiters count as misses (same convention as the fetch cache);
    # the point is the single upstream fan-out, verified above.


# ---------------------------------------------------------------------------
# MCP 2.0 wiring
# ---------------------------------------------------------------------------


def test_mcp_tools_registered():
    import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"search", "fetch_content"} <= names
    for t in tools:
        assert t.description
        if t.name == "search":
            params = t.input_schema.get("properties", {})
            # parity params + free extras
            for p in ("query", "max_results", "region", "backend", "category", "time_range", "safesearch", "pageno"):
                assert p in params, f"missing param {p}"
            assert t.annotations and t.annotations.read_only_hint is True
        if t.name == "fetch_content":
            params = t.input_schema.get("properties", {})
            for p in ("url", "start_index", "max_length", "backend"):
                assert p in params, f"missing param {p}"


def test_mcp_search_end_to_end_mocked(monkeypatch):
    """Full MCP round-trip over the SDK's in-memory transport."""
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    import server

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["format"] == "json"
        return httpx2.Response(200, json=SAMPLE_RESPONSE)

    server.searcher._client = httpx2.AsyncClient(transport=make_transport(handler))

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("search", {"query": "python mcp"})

    result = asyncio.run(run())
    assert not result.is_error
    text = result.content[0].text
    assert "Found 2 search results:" in text
    assert "MCP Python SDK" in text
    assert "Source engines: bing, google cse" in text


def test_mcp_fetch_content_end_to_end_mocked(monkeypatch):
    """fetch_content through the in-memory transport, backend=bs4."""
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    import server

    async def fake_get_html(url):
        return SAMPLE_HTML

    server.fetcher._get_html = fake_get_html

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool(
                "fetch_content",
                {"url": "https://example.com/x", "backend": "bs4", "max_length": 100},
            )

    result = asyncio.run(run())
    assert not result.is_error
    text = result.content[0].text
    assert "Hello World" in text
    assert "Showing characters 0-87 of 87 total" in text  # no truncation needed
    assert "via bs4+html2text" in text
