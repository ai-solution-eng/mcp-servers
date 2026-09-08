"""Unit tests for the SearXNG MCP server (no network required).

Run:  .venv/bin/python -m pytest tests/ -v
Live end-to-end check (needs a reachable SearXNG): see tests/live_check.py
"""

import asyncio
import json

import httpx
import pytest

from fetcher import WebContentFetcher, clean_markdown_cruft, extract_via_bs4
from searxng_client import (
    SearXNGClient,
    SearXNGError,
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


def make_transport(responder) -> httpx.MockTransport:
    return httpx.MockTransport(responder)


def make_client(responder, **kwargs) -> SearXNGClient:
    defaults = dict(base_url="http://searxng.test:8080", requests_per_minute=1000)
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

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=SAMPLE_RESPONSE)

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

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler)
    asyncio.run(client.search("test"))
    assert "engines" not in captured
    assert captured["language"] == "en-US"  # default applied
    assert captured["categories"] == "general"


def test_search_403_gives_json_format_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match="json"):
        asyncio.run(client.search("test"))


def test_search_400_retries_without_language():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if "language" in request.url.params:
            return httpx.Response(400, text="bad language")
        return httpx.Response(200, json=SAMPLE_RESPONSE)

    client = make_client(handler)
    resp = asyncio.run(client.search("test", language="xx-xx"))
    assert len(calls) == 2
    assert "language" not in calls[1]
    assert len(resp.results) == 2


def test_search_unreachable_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    with pytest.raises(SearXNGError, match="could not reach SearXNG"):
        asyncio.run(client.search("test"))


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
    assert out == "No results were found. Try rephrasing your search query."

    resp_unresponsive = SearXNGClient._parse(
        {"results": [], "unresponsive_engines": [["bing", "timeout"]]}, "q"
    )
    assert "bing" in format_search_response(resp_unresponsive, 10)


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

    second = asyncio.run(
        fetcher.fetch_and_parse("https://example.com/x", ctx, start_index=8000, backend="bs4")
    )
    assert "Showing characters 8000-" in second


def test_fetch_and_parse_unreachable(monkeypatch):
    fetcher = WebContentFetcher(requests_per_minute=1000)

    async def fake_get_html(url):
        return None

    async def fake_impersonated(url):
        fetcher.last_fetch_error = "403 (simulated)"
        return None

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
        return None

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
            for p in ("query", "max_results", "region", "backend",
                      "category", "time_range", "safesearch", "pageno"):
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

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json=SAMPLE_RESPONSE)

    server.searcher._client = httpx.AsyncClient(transport=make_transport(handler))

    async def run():
        async with InMemoryTransport(server.mcp) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
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
        async with InMemoryTransport(server.mcp) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
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
