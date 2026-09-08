"""Unit tests for the headless-browser escalation in fetch_content.

No network, no real browser: the browser rung is a stub injected via
``WebContentFetcher(browser=...)``. The real CDP client is exercised in
tests/live_check.py (optional section) when a sidecar is reachable.
"""

import asyncio

import pytest

from browser_client import (
    RENDER_MODES,
    BrowserClient,
    BrowserError,
    BrowserUnavailable,
    RenderedPage,
)
from fetcher import WebContentFetcher

RICH_HTML = """
<html><head><title>Rich</title></head><body>
<main>
<h1>Headless Browser Rendering</h1>
<p>JavaScript rendering lets an agent read pages that ship an empty shell:
the initial HTML contains a root div and a bundle of scripts, and every
meaningful sentence only appears after hydration completes inside a real
browser engine.</p>
<p>This paragraph exists so the readability extractor finds well above the
escalation threshold of two hundred and fifty characters of plain text.
Without enough content the fetcher would spend a render attempt on this
page, which is exactly what auto mode must avoid for static pages.</p>
</main>
</body></html>
"""

JS_SHELL_HTML = """
<html><head><title>App</title></head><body>
<noscript>You need to enable JavaScript to run this app.</noscript>
<div id="root"></div>
<script src="/bundle.js"></script>
</body></html>
"""

WEAK_BUT_PRESENT_HTML = """
<html><head><title>App</title></head><body>
<noscript>Please enable JavaScript and cookies to continue.</noscript>
<main><p>hi</p></main>
</body></html>
"""


def rich_page(url="https://spa.example/"):
    return RenderedPage(html=RICH_HTML, final_url=url, title="Rich", status=200)


class StubBrowserClient:
    """Records render calls; returns configured results or raises."""

    def __init__(self, page=None, shot=None, error=None):
        self.page = page
        self.shot = shot
        self.error = error
        self.calls = []
        self.closed = False

    async def render(self, url, *, screenshot=False, full_page=False):
        self.calls.append({"url": url, "screenshot": screenshot})
        if self.error is not None:
            raise self.error
        return self.page, (self.shot if screenshot else None)

    async def aclose(self):
        self.closed = True


class DummyCtx:
    def __init__(self):
        self.messages = []

    async def info(self, msg):
        self.messages.append(msg)

    async def error(self, msg):
        self.messages.append(msg)


def make_fetcher(monkeypatch, plain_html=None, plain_status=None):
    """Fetcher whose plain-HTTP ladder returns plain_html, or fails with
    plain_status when that is set (both rungs fail identically)."""
    fetcher = WebContentFetcher(requests_per_minute=1000)

    async def fake_get_html(url):
        if plain_status is not None:
            fetcher.last_fetch_error = f"{url} returned HTTP {plain_status}"
            return None
        return plain_html

    async def fake_impersonated(url):
        if plain_status is not None:
            fetcher.last_fetch_error = f"{url} returned HTTP {plain_status} (curl_cffi/chrome)"
            return None
        return plain_html

    monkeypatch.setattr(fetcher, "_get_html", fake_get_html)
    monkeypatch.setattr(fetcher, "_get_html_impersonated", fake_impersonated)
    return fetcher


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Escalation heuristics
# ---------------------------------------------------------------------------


def test_weak_extraction_strong_text_not_escalated():
    f = WebContentFetcher(requests_per_minute=1000)
    assert f._weak_extraction(RICH_HTML, "x" * 500) is False


def test_weak_extraction_short_text_without_markers_not_escalated():
    f = WebContentFetcher(requests_per_minute=1000)
    # Tiny but genuine content, no JS markers: not worth a browser render.
    assert f._weak_extraction("<html><body><p>ok</p></body></html>", "ok") is False


def test_weak_extraction_short_text_with_noscript_escalated():
    f = WebContentFetcher(requests_per_minute=1000)
    assert f._weak_extraction(WEAK_BUT_PRESENT_HTML, "hi") is True


def test_weak_extraction_js_marker_in_text_escalated():
    f = WebContentFetcher(requests_per_minute=1000)
    assert f._weak_extraction("<html></html>", "Please enable JavaScript to view this page.") is True


# ---------------------------------------------------------------------------
# render="auto" escalation behavior
# ---------------------------------------------------------------------------


def test_auto_strong_extraction_skips_browser(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=RICH_HTML)
    stub = StubBrowserClient(page=rich_page())
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://static.example/", DummyCtx()))
    assert stub.calls == []
    assert "Headless Browser Rendering" in out
    assert "(via " in out and "headless-browser" not in out


def test_auto_escalates_when_plain_fetch_blocked(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_status=403)
    stub = StubBrowserClient(page=rich_page())
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://botwalled.example/", DummyCtx()))
    assert len(stub.calls) == 1
    assert "Headless Browser Rendering" in out
    assert "via headless-browser+" in out
    assert "[headless browser" not in out


def test_auto_escalates_on_js_shell_and_prefers_longer_text(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=JS_SHELL_HTML)
    stub = StubBrowserClient(page=rich_page())
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://spa.example/", DummyCtx()))
    assert len(stub.calls) == 1
    assert "Headless Browser Rendering" in out
    assert "enable JavaScript to run this app" not in out


def test_auto_escalation_browser_unavailable_falls_back_to_plain(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=WEAK_BUT_PRESENT_HTML)
    stub = StubBrowserClient(error=BrowserUnavailable("sidecar not reachable"))
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://spa.example/", DummyCtx()))
    # Weak plain result is still returned; failure is logged, not fatal.
    assert "hi" in out
    assert "(via bs4+html2text)" in out or "(via trafilatura)" in out
    assert "via headless-browser" not in out


def test_auto_keeps_plain_result_when_browser_text_not_better(monkeypatch):
    # Plain path found weak content (escalation triggered); the browser
    # render came back with LESS -> the plain result is kept.
    fetcher = make_fetcher(monkeypatch, plain_html=WEAK_BUT_PRESENT_HTML)
    empty_page = RenderedPage(
        html="<html><head><title>x</title></head><body></body></html>",
        final_url="u",
        title="x",
        status=200,
    )
    stub = StubBrowserClient(page=empty_page)
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://mixed.example/", DummyCtx()))
    assert len(stub.calls) == 1
    assert "hi" in out
    assert "(via headless-browser" not in out


def test_error_message_reports_both_ladder_and_browser(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_status=403)
    stub = StubBrowserClient(error=BrowserUnavailable("CDP connect refused"))
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://blocked.example/", DummyCtx()))
    assert out.startswith("Error: Could not access the webpage")
    assert "HTTP 403" in out
    assert "[headless browser: CDP connect refused]" in out


# ---------------------------------------------------------------------------
# render="never" / render="always"
# ---------------------------------------------------------------------------


def test_render_never_skips_browser_even_on_failure(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_status=403)
    stub = StubBrowserClient(page=rich_page())
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://blocked.example/", DummyCtx(), render="never"))
    assert stub.calls == []
    assert out.startswith("Error: Could not access the webpage")


def test_render_always_browser_first_without_plain_fetch(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=RICH_HTML)
    plain_calls = []

    async def spy_get_html(url):
        plain_calls.append(url)
        return RICH_HTML

    monkeypatch.setattr(fetcher, "_get_html", spy_get_html)
    stub = StubBrowserClient(page=rich_page())
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://spa.example/", DummyCtx(), render="always"))
    assert len(stub.calls) == 1
    assert plain_calls == []
    assert "via headless-browser+" in out


def test_render_always_falls_back_to_plain_when_browser_down(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=RICH_HTML)
    stub = StubBrowserClient(error=BrowserUnavailable("down"))
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://spa.example/", DummyCtx(), render="always"))
    assert "Headless Browser Rendering" in out
    assert "via trafilatura" in out or "via bs4+html2text" in out


def test_render_always_browser_render_error_degrades_to_plain(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=RICH_HTML)
    stub = StubBrowserClient(error=BrowserError("Timeout 30000ms exceeded"))
    fetcher._browser = stub
    out = run(fetcher.fetch_and_parse("https://spa.example/", DummyCtx(), render="always"))
    assert "Headless Browser Rendering" in out


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def test_include_screenshot_forces_browser_and_appends_data_url(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=RICH_HTML)
    stub = StubBrowserClient(page=rich_page(), shot="QUJD")  # base64("ABC")
    fetcher._browser = stub
    out = run(
        fetcher.fetch_and_parse(
            "https://spa.example/", DummyCtx(), include_screenshot=True
        )
    )
    assert len(stub.calls) == 1 and stub.calls[0]["screenshot"] is True
    assert "data:image/png;base64,QUJD" in out
    assert "PNG data URL" in out


def test_screenshot_requested_but_browser_down_still_returns_content(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_html=RICH_HTML)
    stub = StubBrowserClient(error=BrowserUnavailable("down"))
    fetcher._browser = stub
    out = run(
        fetcher.fetch_and_parse(
            "https://spa.example/", DummyCtx(), include_screenshot=True
        )
    )
    assert "Headless Browser Rendering" in out
    assert "data:image/png" not in out


# ---------------------------------------------------------------------------
# Wikipedia URLs skip the browser rung
# ---------------------------------------------------------------------------


def test_wikipedia_url_skips_browser_and_uses_api(monkeypatch):
    fetcher = make_fetcher(monkeypatch, plain_status=403)
    stub = StubBrowserClient(page=rich_page())
    fetcher._browser = stub

    async def fake_wiki_api(url):
        return "Wikipedia API prose."

    monkeypatch.setattr(fetcher, "_try_wikipedia_api", fake_wiki_api)
    out = run(
        fetcher.fetch_and_parse("https://en.wikipedia.org/wiki/Search_engine", DummyCtx())
    )
    assert stub.calls == []
    assert "Wikipedia API prose." in out
    assert "(via Wikipedia API)" in out


# ---------------------------------------------------------------------------
# Tool schema / wire validation
# ---------------------------------------------------------------------------


def test_fetch_content_schema_has_render_and_screenshot():
    import server

    tools = asyncio.run(server.mcp.list_tools())
    fc = next(t for t in tools if t.name == "fetch_content")
    params = fc.input_schema.get("properties", {})
    for p in ("url", "start_index", "max_length", "backend", "render", "include_screenshot"):
        assert p in params, f"missing param {p}"
    # The old "cannot execute JavaScript" limitation note must be gone.
    assert "cannot execute JavaScript" not in (fc.description or "")


def test_fetch_content_rejects_unknown_render_over_wire():
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    import server

    async def run_wire():
        async with InMemoryTransport(server.mcp) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await session.call_tool(
                    "fetch_content",
                    {"url": "https://example.com/", "render": "bogus"},
                )

    result = asyncio.run(run_wire())
    assert not result.is_error  # validation errors are returned as text
    assert "unknown render 'bogus'" in result.content[0].text
    assert "auto, always, never" in result.content[0].text


# ---------------------------------------------------------------------------
# BrowserClient (real client; no sidecar needed for these paths)
# ---------------------------------------------------------------------------


def test_render_modes_contract():
    assert RENDER_MODES == ("auto", "always", "never")


def test_browser_client_env_defaults(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("BROWSER_NAV_TIMEOUT_MS", "12345")
    monkeypatch.setenv("BROWSER_MAX_PAGES", "2")
    monkeypatch.setenv("BROWSER_BLOCK_RESOURCES", "false")
    client = BrowserClient()
    assert client.cdp_url == "http://127.0.0.1:9999"
    assert client.navigation_timeout_ms == 12345
    assert client.max_pages == 2
    assert client.block_resources is False


def test_browser_client_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9999")
    client = BrowserClient(cdp_url="http://localhost:9222", max_pages=1)
    assert client.cdp_url == "http://localhost:9222"
    assert client.max_pages == 1


def test_browser_client_unreachable_cdp_raises_unavailable():
    client = BrowserClient(
        cdp_url="http://127.0.0.1:59999",
        connect_timeout_ms=500,
        navigation_timeout_ms=1000,
    )
    with pytest.raises(BrowserUnavailable):
        asyncio.run(client.render("https://example.com/"))
