"""Web content fetcher for the SearXNG MCP server.

Extracts the main readable text of a page as clean markdown.

Fetch phase (getting the HTML):
1. plain httpx — browser-like headers, honors HTTP(S)_PROXY / NO_PROXY
2. curl_cffi impersonating Chrome — escalation for sites whose anti-bot
   edge rejects python's TLS fingerprint with 403 (wikipedia does this from
   some egress paths); restores the ddgs-lite primp capability without the
   ddgs scraping library

Extraction phase (HTML -> markdown):
1. trafilatura  — best-quality readability extraction, native markdown output
2. bs4 + html2text — structural fallback for pages trafilatura rejects
3. Wikipedia REST API — last resort for wikipedia pages

The tool-level output contract (pagination meta line, source attribution,
error wording) matches ddgs-lite's fetch_content exactly, plus failure
telemetry: the error names the actual HTTP status / exception of the last
attempt.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import unquote

import httpx

from searxng_client import RateLimiter

FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}

BACKENDS = ("auto", "trafilatura", "bs4", "httpx", "curl", "wikipedia")


def clean_markdown_cruft(text: str) -> str:
    """Collapse excessive whitespace from extracted markdown."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def env_proxies() -> dict[str, str] | None:
    """Proxy mapping for curl_cffi from the environment (httpx trust_env
    equivalent). libcurl honors NO_PROXY from the environment itself."""
    p = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    )
    return {"http": p, "https": p} if p else None


def extract_via_trafilatura(html: str, url: str) -> str | None:
    """Best-quality extraction; returns markdown or None."""
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        text = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            favor_recall=True,
        )
        return (text or "").strip() or None
    except Exception:
        return None


def extract_via_bs4(html: str) -> str | None:
    """Structural fallback: strip chrome, convert the main block to markdown."""
    try:
        from bs4 import BeautifulSoup
        import html2text
    except ImportError:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header",
                         "footer", "aside", "form", "iframe", "svg"]):
            tag.decompose()
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(id="content")
            or soup.find(role="main")
            or soup.body
            or soup
        )
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.body_width = 0
        text = converter.handle(str(main))
        return (text or "").strip() or None
    except Exception:
        return None


class WebContentFetcher:
    def __init__(
        self,
        requests_per_minute: int = 20,
        timeout: float = 30.0,
        verify_tls: bool = True,
    ):
        self.rate_limiter = RateLimiter(requests_per_minute)
        self.last_fetch_error: str | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
            trust_env=True,  # honor HTTP(S)_PROXY / NO_PROXY like ddgs-lite did
            headers=FETCH_HEADERS,
            verify=verify_tls,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_html(self, url: str) -> str | None:
        """Plain HTTP GET returning the raw HTML, or None on any failure.

        Records the failure reason on self.last_fetch_error so the tool's
        error output can say WHY (status code / exception) instead of a bare
        "could not access".
        """
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            self.last_fetch_error = None
            return response.text
        except httpx.HTTPStatusError as e:
            self.last_fetch_error = f"{url} returned HTTP {e.response.status_code}"
            return None
        except Exception as e:
            self.last_fetch_error = f"{type(e).__name__}: {e}"[:200]
            return None

    async def _get_html_impersonated(self, url: str) -> str | None:
        """GET with a browser-grade TLS fingerprint (curl_cffi impersonating
        Chrome). Escalation for sites that 403 plain python clients based on
        TLS fingerprint. The sync libcurl call runs in an executor."""

        def _sync() -> str | None:
            try:
                from curl_cffi import requests as curl_requests
            except ImportError:
                self.last_fetch_error = "curl_cffi backend unavailable (not installed)"
                return None
            try:
                r = curl_requests.get(
                    url,
                    impersonate="chrome",
                    timeout=30.0,
                    proxies=env_proxies(),
                )
                if r.status_code != 200:
                    self.last_fetch_error = (
                        f"{url} returned HTTP {r.status_code} (curl_cffi/chrome)"
                    )
                    return None
                return r.text
            except Exception as e:
                self.last_fetch_error = f"curl_cffi {type(e).__name__}: {e}"[:200]
                return None

        return await asyncio.get_running_loop().run_in_executor(None, _sync)

    async def _try_wikipedia_api(self, url: str) -> str | None:
        """Fetch page prose via the Wikipedia API — the sanctioned route for
        wiki content. Escalates plain -> impersonated like everything else
        (the API edge applies the same anti-bot filtering)."""
        m = re.match(r"https?://([a-z]+)\.wikipedia\.org/wiki/([^?#]+)", url)
        if not m:
            return None
        lang = m.group(1)
        title = unquote(m.group(2))
        api_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=query&titles={title}&prop=extracts"
            f"&exlimit=1&explaintext=1&format=json"
        )
        # Fetch via the shared paths (browser UA; escalates to curl_cffi on
        # failure — the API edge filters TLS fingerprints like the site does).
        body = await self._get_html(api_url)
        if body is None:
            body = await self._get_html_impersonated(api_url)
        if body is None:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        pages = data.get("query", {}).get("pages", {})
        extracts = [
            page.get("extract", "")
            for page in pages.values()
            if page.get("extract", "")
        ]
        return "\n\n".join(extracts) if extracts else None

    async def fetch_and_parse(
        self,
        url: str,
        ctx,
        start_index: int = 0,
        max_length: int = 8000,
        backend: str = "auto",
    ) -> str:
        await self.rate_limiter.acquire()
        await ctx.info(f"Fetching content from: {url}")

        text = None
        source = None
        loop = asyncio.get_running_loop()

        if backend == "wikipedia":
            text = await self._try_wikipedia_api(url)
            if text:
                source = "Wikipedia API"
        else:
            # ---- fetch phase: plain httpx, escalating to a browser-grade
            # TLS fingerprint when the site rejects plain clients.
            if backend == "curl":
                html = await self._get_html_impersonated(url)
            else:
                html = await self._get_html(url)
                if html is None:
                    html = await self._get_html_impersonated(url)

            # ---- extraction phase
            if html is not None:
                if backend in ("auto", "trafilatura", "curl"):
                    text = await loop.run_in_executor(
                        None, extract_via_trafilatura, html, url
                    )
                    if text:
                        source = "trafilatura"
                if text is None and backend in ("auto", "bs4", "httpx", "curl"):
                    text = await loop.run_in_executor(None, extract_via_bs4, html)
                    if text:
                        source = "bs4+html2text"

            if text is None and backend == "auto":
                wiki_text = await self._try_wikipedia_api(url)
                if wiki_text:
                    text, source = wiki_text, "Wikipedia API"

        if text is None:
            detail = f" (last attempt: {self.last_fetch_error})" if self.last_fetch_error else ""
            return (
                f"Error: Could not access the webpage at {url}{detail}. "
                "The site may require JavaScript, be blocked, or require authentication. "
                "Fetching escalates from plain HTTP to a browser-grade TLS fingerprint "
                "(curl_cffi); sites that block even that (JS challenges, logins) "
                "remain out of reach."
            )

        text = clean_markdown_cruft(text)

        total = len(text)
        text = text[start_index : start_index + max_length]
        truncated = start_index + max_length < total

        meta = (
            f"\n\n---\n[Content info: Showing characters {start_index}-"
            f"{start_index + len(text)} of {total} total"
        )
        if truncated:
            meta += f". Use start_index={start_index + max_length} to see more"
        meta += f" (via {source})]"

        await ctx.info(f"Extracted {len(text)} characters from {url}")
        return text + meta
