"""Web content fetcher for the SearXNG MCP server.

Extracts the main readable text of a page as clean markdown.

Fetch phase (getting the HTML):
0. SSRF policy gate (url_policy.py) — scheme allowlist, resolved-IP
   denylist, and a DNS-rebinding pin: every hop connects to the address
   that was validated, not to whatever the name resolves to at
   connect-time. Requests through an HTTP(S) proxy skip the pin (the proxy
   performs egress DNS; the denylist still applies).
1. plain httpx — browser-like headers, honors HTTP(S)_PROXY / NO_PROXY
2. curl_cffi impersonating Chrome — escalation for sites whose anti-bot
   edge rejects python's TLS fingerprint with 403 (wikipedia does this from
   some egress paths); restores the ddgs-lite primp capability without the
   ddgs scraping library
3. headless browser (optional sidecar, Playwright over CDP) — escalation
   for pages the first two rungs cannot see: JS-rendered SPAs, challenge
   interstitials, or content that only exists after scripts run. Renders
   the page, then feeds the resulting DOM through the same extraction
   chain below.

Redirects are NOT followed blindly: the httpx client is created with
``follow_redirects=False`` and a manual hop loop re-validates every
``Location`` (scheme, resolved IPs, pin) before the next request, up to
``SEARXNG_FETCH_MAX_REDIRECTS`` hops.

Extraction phase (HTML -> markdown):
1. trafilatura  — best-quality readability extraction, native markdown output
2. bs4 + html2text — structural fallback for pages trafilatura rejects
   (bs4 prefers the lxml parser when lxml is importable)
3. Wikipedia REST API — last resort for wikipedia pages

The tool-level output contract (pagination meta line, source attribution,
error wording) matches ddgs-lite's fetch_content exactly, plus failure
telemetry: the error names the actual HTTP status / exception of the last
attempt.

Rendering is controlled by the ``render`` parameter:
* "auto" (default) — escalate to the browser only when the plain fetch
  failed outright or produced a weak/JS-stub extraction (see
  ``_weak_extraction``).
* "always" — render in the browser first; fall back to plain HTTP if the
  sidecar is unavailable.
* "never" — plain HTTP ladder only, exactly like before the sidecar
  existed.

Caching: successful ladder runs are memoized per (url, backend, render,
screenshot) for ``SEARXNG_FETCH_CACHE_TTL`` seconds (default 300, 0
disables) with single-flight coalescing — concurrent identical requests
share one upstream fetch. Pagination/slicing stays outside the cache.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin

import httpx

import url_policy
from browser_client import (
    BrowserClient,
    BrowserError,
    BrowserUnavailable,
)
from searxng_client import RateLimiter

FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}

BACKENDS = ("auto", "trafilatura", "bs4", "httpx", "curl", "wikipedia")

REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Escalation-to-browser heuristics (render="auto"). A page whose extracted
# text is shorter than RENDER_MIN_CHARS *and* smells like a JS-only shell
# gets one render attempt in the sidecar browser.
RENDER_MIN_CHARS = 250
# A document at least this big whose extracted text is still under
# RENDER_MIN_CHARS is treated as a JS-only shell even without explicit
# markers: scripts inject the content and leave none. Genuine small pages
# sit far below this; the shell that motivated the rule
# (quotes.toscrape.com/js/) is 5.8 KB of markup with 96 chars of text.
RENDER_SHELL_MIN_BYTES = 4000
JS_PAGE_MARKERS = re.compile(
    r"<noscript[\s>]"
    r"|enable\s+javascript"
    r"|just\s+a\s+moment"  # Cloudflare interstitial title
    r"|attention\s+required"
    r"|challenge-platform"
    r"|cf-browser-verification"
    r"|checking\s+your\s+browser"
    r"|ddos\s+protection",
    re.IGNORECASE,
)
WIKIPEDIA_URL_RE = re.compile(r"https?://([a-z]+)\.wikipedia\.org/wiki/([^?#]+)")


def clean_markdown_cruft(text: str) -> str:
    """Collapse excessive whitespace from extracted markdown."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _bs4_parser() -> str:
    """Prefer the lxml parser (5-10x faster than html.parser) when lxml is
    importable; fall back to the pure-python parser otherwise."""
    try:
        import lxml  # noqa: F401
    except ImportError:
        return "html.parser"
    return "lxml"


def env_proxies() -> dict[str, str] | None:
    """Proxy mapping for curl_cffi from the environment (httpx trust_env
    equivalent). libcurl honors NO_PROXY from the environment itself."""
    p = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
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
        import html2text
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        soup = BeautifulSoup(html, _bs4_parser())
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]):
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


# ---------------------------------------------------------------------------
# Result cache with single-flight coalescing
# ---------------------------------------------------------------------------


@dataclass
class FetchOutcome:
    """Everything the ladder produced for one fetch (pre-pagination)."""

    text: str | None = None
    source: str | None = None
    screenshot_b64: str | None = None
    screenshot_omitted: str | None = None  # why a requested screenshot is absent
    body_truncated: bool = False
    policy_error: str | None = None  # SSRF-policy rejection (deterministic -> cacheable)


class FetchResultCache:
    """Tiny TTL cache + single-flight coalescer for fetch ladder outcomes.

    * TTL: entries expire after ``ttl_seconds`` (0 disables caching entirely).
    * Single flight: concurrent calls with the same key share ONE upstream
      run — later callers await the same task instead of racing the ladder.
    * Unbounded outcomes are never stored; the caller passes a
      ``cacheable`` predicate (failures/policy errors may coalesce but a
      plain failure is not memoized).
    * Bounded to ``max_entries`` (oldest evicted) so a long-lived process
      cannot accumulate response bodies.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 64):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._store: dict[tuple, tuple[float, FetchOutcome]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def _peek(self, key) -> FetchOutcome | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expiry, outcome = entry
        if expiry <= time.monotonic():
            self._store.pop(key, None)
            return None
        return outcome

    async def run(self, key, factory, cacheable=None) -> tuple[FetchOutcome, bool]:
        """Run ``factory()`` once per key within the TTL window; returns
        ``(outcome, cache_hit)``."""
        hit = self._peek(key)
        if hit is not None:
            self.hits += 1
            return hit, True
        loop = asyncio.get_running_loop()
        # Coalesce: one shared task per key. All logic (publish + cleanup +
        # memoize) lives in the task's done-callback so it happens even if
        # the owner caller is cancelled mid-flight; asyncio.shield lets
        # individual waiters go away without killing the upstream run.
        async with self._lock:
            hit = self._peek(key)
            if hit is not None:
                self.hits += 1
                return hit, True
            task = self._inflight.get(key)
            if task is None:
                task = loop.create_task(_run_factory(factory))
                self._inflight[key] = task

                def _publish(done: asyncio.Task, key=key) -> None:
                    self._inflight.pop(key, None)
                    if done.cancelled():
                        return
                    exc = done.exception()
                    if exc is not None:
                        return  # failures are coalesced but never memoized
                    outcome = done.result()
                    if cacheable is None or cacheable(outcome):
                        self._store[key] = (
                            time.monotonic() + self.ttl_seconds,
                            outcome,
                        )
                        while len(self._store) > self.max_entries:
                            self._store.pop(next(iter(self._store)))

                task.add_done_callback(_publish)
        self.misses += 1
        return await asyncio.shield(task), False


async def _run_factory(factory):
    result = factory()
    if asyncio.iscoroutine(result):
        result = await result
    return result


class WebContentFetcher:
    def __init__(
        self,
        requests_per_minute: int = 20,
        timeout: float = 30.0,
        verify_tls: bool = True,
        browser: BrowserClient | None = None,
        cache_ttl_seconds: float | None = None,
        max_body_bytes: int | None = None,
        max_screenshot_kb: float | None = None,
        max_redirects: int | None = None,
    ):
        self.rate_limiter = RateLimiter(requests_per_minute)
        self.last_fetch_error: str | None = None
        self.last_policy_error: str | None = None  # SSRF rejection (last fetch)
        self.body_truncated: bool = False  # response-body cap hit on the last fetch
        self.browser_note: str | None = None  # why the browser rung failed, if it did
        # Headless-browser client, created lazily on first escalation so a
        # plain install (no playwright, no sidecar) never pays for it.
        # Tests inject a stub here.
        self._browser = browser
        self.timeout = timeout
        self.verify_tls = verify_tls
        # Quick wins (fleet audit §3.4): response caps + TTL result cache.
        self.max_body_bytes = (
            max_body_bytes if max_body_bytes is not None else _env_int("SEARXNG_FETCH_MAX_BODY_BYTES", 5_000_000)
        )
        self.max_screenshot_bytes = (
            int(float(max_screenshot_kb) * 1024)
            if max_screenshot_kb is not None
            else _env_int("SEARXNG_FETCH_MAX_SCREENSHOT_KB", 512) * 1024
        )
        self.max_redirect_hops = (
            max_redirects if max_redirects is not None else max(0, _env_int("SEARXNG_FETCH_MAX_REDIRECTS", 5))
        )
        if cache_ttl_seconds is not None:
            ttl = float(cache_ttl_seconds)
        else:
            try:
                ttl = float(os.getenv("SEARXNG_FETCH_CACHE_TTL", "300"))
            except ValueError:
                ttl = 300.0
        self.result_cache = FetchResultCache(ttl_seconds=ttl)

    def _make_client(self, *, pinned: bool) -> httpx.AsyncClient:
        """One short-lived client per logical fetch.

        ``pinned=True`` (default when no proxy env is set) connects to the
        policy-validated IP directly — proxies are bypassed on purpose, the
        URL rewrite IS the DNS-rebinding pin. ``pinned=False`` (a proxy is
        configured) keeps trust_env so corporate-egress deployments keep
        working; the proxy then performs egress DNS.
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,  # manual hop loop re-validates every Location
            trust_env=not pinned,
            headers=FETCH_HEADERS,
            verify=self.verify_tls,
        )

    @property
    def browser(self) -> BrowserClient:
        if self._browser is None:
            self._browser = BrowserClient()
        return self._browser

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.aclose()

    async def _get_html(self, url: str) -> str | None:
        """Plain HTTP GET returning the raw HTML, or None on any failure.

        Every hop is validated against the SSRF policy (scheme, resolved
        IPs, denylist/allowlist) and — when no proxy env is set — connected
        to the validated address (DNS-rebinding pin). Redirects are followed
        manually up to ``max_redirect_hops``; a redirect into blocked space
        aborts the whole fetch with a policy error, and so does exceeding
        the hop cap (both set self.last_policy_error — a policy refusal is
        terminal, the ladder never escalates it). Records the failure
        reason on self.last_fetch_error so the tool's error output can say
        WHY (status code / exception / policy) instead of a bare "could not
        access".
        """
        current = url
        for hop in range(self.max_redirect_hops + 1):
            try:
                pinned = await url_policy.validate_fetch_url_async(current)
            except url_policy.UrlPolicyError as e:
                self.last_fetch_error = str(e)
                self.last_policy_error = str(e)
                return None
            client = self._make_client(pinned=pinned.pin_active)
            try:
                request = client.build_request(
                    "GET",
                    pinned.pinned_url,
                    headers={"Host": pinned.host_header} if pinned.host_header else None,
                    extensions=({"sni_hostname": pinned.sni_hostname} if pinned.sni_hostname else None),
                )
                response = await client.send(request, stream=True)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    status = response.status_code
                    await response.aclose()
                    if hop >= self.max_redirect_hops:
                        self.last_fetch_error = (
                            f"{url} redirected more than {self.max_redirect_hops} "
                            f"time(s) (last hop: {status} -> {location or '?'})"
                        )
                        # The hop cap is part of the SSRF redirect guard: a
                        # policy-class refusal, terminal like a blocked hop.
                        self.last_policy_error = self.last_fetch_error
                        return None
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    self.last_fetch_error = f"{current} returned HTTP {response.status_code}"
                    await response.aclose()
                    return None
                body, truncated = await self._read_body_capped(response)
                self.body_truncated = truncated
                self.last_fetch_error = None
                self.last_policy_error = None
                return body
            except Exception as e:
                self.last_fetch_error = f"{type(e).__name__}: {e}"[:200]
                return None
            finally:
                await client.aclose()
        # Unreachable: the loop either returns or continues past the cap.
        self.last_fetch_error = f"{url} exceeded the redirect hop limit"
        return None

    async def _read_body_capped(self, response: httpx.Response) -> tuple[str, bool]:
        """Stream the response body up to ``max_body_bytes``; returns
        (text, truncated). The connection is closed as soon as the cap is
        hit — a huge document never fully downloads."""
        cap = max(1024, self.max_body_bytes)
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= cap:
                    truncated = True
                    break
        finally:
            await response.aclose()
        data = b"".join(chunks)[:cap]
        encoding = response.encoding or "utf-8"
        return data.decode(encoding, errors="replace"), truncated

    async def _get_html_impersonated(self, url: str) -> str | None:
        """GET with a browser-grade TLS fingerprint (curl_cffi impersonating
        Chrome). Escalation for sites that 403 plain python clients based on
        TLS fingerprint. The sync libcurl call runs in an executor.

        Redirects are followed manually (``allow_redirects=False``) with the
        same per-hop policy re-validation as the plain rung; libcurl does
        its own name resolution, so no IP pin here — every hop's target is
        still deny-checked before the request.
        """

        def _sync() -> str | None:
            try:
                from curl_cffi import requests as curl_requests
            except ImportError:
                self.last_fetch_error = "curl_cffi backend unavailable (not installed)"
                return None
            current = url
            for hop in range(self.max_redirect_hops + 1):
                try:
                    pinned = url_policy.validate_fetch_url(current)
                except url_policy.UrlPolicyError as e:
                    self.last_fetch_error = str(e)
                    self.last_policy_error = str(e)
                    return None
                try:
                    r = curl_requests.get(
                        pinned.url,
                        impersonate="chrome",
                        timeout=30.0,
                        proxies=env_proxies(),
                        allow_redirects=False,
                    )
                except Exception as e:
                    self.last_fetch_error = f"curl_cffi {type(e).__name__}: {e}"[:200]
                    return None
                if r.status_code in REDIRECT_STATUS_CODES and r.headers.get("location"):
                    if hop >= self.max_redirect_hops:
                        self.last_fetch_error = (
                            f"{url} redirected more than {self.max_redirect_hops} time(s) (curl_cffi/chrome)"
                        )
                        return None
                    current = urljoin(current, r.headers["location"])
                    continue
                if r.status_code != 200:
                    self.last_fetch_error = f"{current} returned HTTP {r.status_code} (curl_cffi/chrome)"
                    return None
                text = r.text
                if len(text) > self.max_body_bytes:
                    text = text[: self.max_body_bytes]
                    self.body_truncated = True
                self.last_fetch_error = None
                self.last_policy_error = None
                return text
            self.last_fetch_error = f"{url} exceeded the redirect hop limit"
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
        # A policy refusal is terminal: never re-request the refused target.
        body = await self._get_html(api_url)
        if body is None and self.last_policy_error is None:
            body = await self._get_html_impersonated(api_url)
        if body is None:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        pages = data.get("query", {}).get("pages", {})
        extracts = [page.get("extract", "") for page in pages.values() if page.get("extract", "")]
        return "\n\n".join(extracts) if extracts else None

    async def fetch_and_parse(
        self,
        url: str,
        ctx,
        start_index: int = 0,
        max_length: int = 8000,
        backend: str = "auto",
        render: str = "auto",
        include_screenshot: bool = False,
    ) -> str:
        # ---- SSRF policy preflight: DNS-free fast fail (scheme, loopback /
        # metadata / cluster-local names, literal private IPs) before any
        # rate budget, ladder, cache or browser work is spent. The full
        # policy — resolved IPs, denylist, pin — is enforced per hop in
        # _get_html and pre-goto in the browser client.
        try:
            url_policy.preflight_fetch_url(url)
        except url_policy.UrlPolicyError as e:
            await ctx.error(f"Blocked by the fetch SSRF policy: {e}")
            return f"Error: {e}"

        key = (url, backend, render, bool(include_screenshot))
        outcome, cache_hit = await self.result_cache.run(
            key,
            lambda: self._fetch_outcome(url, ctx, backend, render, include_screenshot),
            cacheable=lambda o: o.text is not None or o.policy_error is not None,
        )
        if cache_hit:
            await ctx.info(f"Fetch served from cache (TTL {self.result_cache.ttl_seconds:.0f}s): {url}")

        if outcome.policy_error:
            await ctx.error(f"Blocked by the fetch SSRF policy: {outcome.policy_error}")
            return f"Error: {outcome.policy_error}"

        text, source = outcome.text, outcome.source
        if text is None:
            # The policy verdict is reported verbatim whenever one exists —
            # a later rung's backend complaint (e.g. "curl_cffi backend
            # unavailable (not installed)") may never mangle the real
            # refusal reason (Wave-2 fix: backend-unavailability is only
            # reportable when no policy verdict exists).
            reason = self.last_policy_error or self.last_fetch_error
            detail = f" (last attempt: {reason})" if reason else ""
            browser_note = getattr(self, "browser_note", None)
            if browser_note:
                detail += f" [headless browser: {browser_note}]"
            return (
                f"Error: Could not access the webpage at {url}{detail}. "
                "The site may require JavaScript, be blocked, or require authentication. "
                "Fetching escalates from plain HTTP to a browser-grade TLS fingerprint "
                "(curl_cffi) and then to a headless browser (when the sidecar is "
                "enabled); sites that block even a real browser or need logins "
                "remain out of reach."
            )

        text = clean_markdown_cruft(text)

        total = len(text)
        text = text[start_index : start_index + max_length]
        truncated = start_index + max_length < total

        # ---- screenshot, INSIDE the pagination contract: served once, with
        # the first page only, and size-capped. Previously it was appended
        # AFTER the max_length slice — an uncapped payload on every page
        # (a response-size bypass and an exfil channel).
        if start_index == 0 and outcome.screenshot_b64:
            shot_kb = len(outcome.screenshot_b64) // 1024
            text += (
                "\n\n---\n[Screenshot of the rendered page (PNG data URL — "
                f"pass to a vision tool as-is, ~{shot_kb} KB base64):]\n"
                f"data:image/png;base64,{outcome.screenshot_b64}"
            )
        elif start_index == 0 and outcome.screenshot_omitted:
            text += (
                f"\n\n---\n[Screenshot omitted: {outcome.screenshot_omitted}; "
                "raise the cap if you need this page's screenshot]"
            )
        elif start_index > 0 and (outcome.screenshot_b64 or outcome.screenshot_omitted):
            text += (
                "\n\n---\n[Screenshot: shipped with the first page only — re-fetch with start_index=0 to receive it]"
            )

        meta = f"\n\n---\n[Content info: Showing characters {start_index}-{start_index + len(text)} of {total} total"
        if truncated:
            meta += f". Use start_index={start_index + max_length} to see more"
        meta += f" (via {source})]"
        if outcome.body_truncated:
            meta += (
                f"\n[Note: the response body was truncated at "
                f"{self.max_body_bytes} bytes (SEARXNG_FETCH_MAX_BODY_BYTES); "
                "extraction ran on partial content]"
            )

        await ctx.info(f"Extracted {len(text)} characters from {url}")
        return text + meta

    async def _fetch_outcome(self, url: str, ctx, backend: str, render: str, include_screenshot: bool) -> FetchOutcome:
        """The fetch+extract ladder (cache-miss path). Exactly one call per
        cache key runs this at a time (single-flight coalescing)."""
        await self.rate_limiter.acquire()
        await ctx.info(f"Fetching content from: {url}")

        outcome = FetchOutcome()
        self.body_truncated = False
        self.last_policy_error = None
        loop = asyncio.get_running_loop()
        is_wiki_url = bool(WIKIPEDIA_URL_RE.match(url))

        if backend == "wikipedia":
            text = await self._try_wikipedia_api(url)
            if text:
                outcome.text, outcome.source = text, "Wikipedia API"
        else:
            html = None
            rendered_in_browser = False

            # ---- fetch phase, rung 0: explicit headless-browser render.
            # "always" or a requested screenshot skips the plain-HTTP ladder
            # entirely — the caller asked for JS execution by definition.
            if render == "always" or include_screenshot:
                page, shot = await self._render_via_browser(url, ctx, with_screenshot=include_screenshot)
                if page is not None:
                    rendered_in_browser = True
                    text, source = await self._extract_html(
                        page.html, url, loop, backend, source_prefix="headless-browser+"
                    )
                    outcome.text, outcome.source = text, source
                    outcome.screenshot_b64 = self._cap_screenshot(shot, outcome)

            # ---- fetch phase, rungs 1-2: plain httpx, escalating to a
            # browser-grade TLS fingerprint when the site rejects plain
            # clients. (A per-hop policy rejection shows up in
            # self.last_policy_error, checked by the rungs below.) A POLICY
            # refusal is TERMINAL: the blocked target is never re-requested
            # through a further rung — escalation would both fetch a URL the
            # policy just refused and overwrite the refusal reason with a
            # backend complaint (e.g. "curl_cffi backend unavailable").
            if outcome.text is None:
                if backend == "curl":
                    html = await self._get_html_impersonated(url)
                else:
                    html = await self._get_html(url)
                    if html is None and self.last_policy_error is None:
                        html = await self._get_html_impersonated(url)

                text, source = await self._extract_html(html, url, loop, backend, "")
                outcome.text, outcome.source = text, source

                if text is None and backend == "auto" and self.last_policy_error is None:
                    wiki_text = await self._try_wikipedia_api(url)
                    if wiki_text:
                        outcome.text, outcome.source = wiki_text, "Wikipedia API"

            # ---- fetch phase, rung 3: headless-browser escalation
            # (render="auto"). Wikipedia URLs skip it — the Wikipedia API
            # fallback above is cheaper and authoritative for those. A page
            # already rendered in rung 0 is never re-rendered. A policy
            # rejection never escalates (the URL is bad, not JS-heavy) —
            # last_policy_error is set live by the rungs themselves.
            if (
                render == "auto"
                and not is_wiki_url
                and not rendered_in_browser
                and self.last_policy_error is None
                and (outcome.text is None or self._weak_extraction(html, outcome.text))
            ):
                page, shot = await self._render_via_browser(url, ctx, with_screenshot=include_screenshot)
                if page is not None:
                    alt_text, alt_source = await self._extract_html(
                        page.html, url, loop, backend, source_prefix="headless-browser+"
                    )
                    if alt_text and (outcome.text is None or len(alt_text) > len(outcome.text)):
                        outcome.text, outcome.source = alt_text, alt_source
                    if outcome.screenshot_b64 is None:
                        outcome.screenshot_b64 = self._cap_screenshot(shot, outcome)

        outcome.body_truncated = self.body_truncated
        # A per-hop policy rejection (redirect into blocked space) is the
        # definitive answer — surface it instead of the generic error.
        if outcome.text is None and self.last_policy_error:
            outcome.policy_error = self.last_policy_error
        return outcome

    def _cap_screenshot(self, shot: str | None, outcome: FetchOutcome) -> str | None:
        """Drop an oversized screenshot up front (never cached, never
        returned whole): the reason lands in the page-1 output."""
        if shot is None:
            return None
        if len(shot) > self.max_screenshot_bytes:
            outcome.screenshot_omitted = (
                f"{len(shot) // 1024} KB exceeds the "
                f"{self.max_screenshot_bytes // 1024} KB cap "
                "(SEARXNG_FETCH_MAX_SCREENSHOT_KB)"
            )
            return None
        return shot

    # ------------------------------------------------------- browser rung
    def _weak_extraction(self, html: str | None, text: str | None) -> bool:
        """True when the plain-HTTP result smells like a JS-only page:
        almost no readable text despite a substantial document, or explicit
        JS-required / challenge markers."""
        if text is None:
            return False  # nothing extracted at all is escalated regardless
        if len(text) >= RENDER_MIN_CHARS:
            return False
        haystack = (html or "")[:8000] + text
        if JS_PAGE_MARKERS.search(haystack):
            return True
        # Marker-less SPA shell: the script bundle fills the page at runtime
        # and leaves no explicit marker, so a substantial document with
        # near-empty readable text is only readable after a render. One
        # bounded attempt; the better extraction of the two is kept either way.
        return len(html or "") >= RENDER_SHELL_MIN_BYTES

    async def _render_via_browser(self, url, ctx, *, with_screenshot=False):
        """Render via the sidecar; every failure degrades to (None, None)
        with the reason logged, never raised — the browser is an optional
        escalation rung, not a hard dependency."""
        try:
            return await self.browser.render(url, screenshot=with_screenshot)
        except BrowserUnavailable as e:
            self.browser_note = str(e)
            await ctx.info(f"Headless browser unavailable, continuing without it: {e}")
            return None, None
        except BrowserError as e:
            self.browser_note = str(e)
            await ctx.info(f"Headless browser render failed, continuing without it: {e}")
            return None, None

    async def _extract_html(self, html, url, loop, backend, source_prefix):
        """Run the trafilatura -> bs4 extraction chain over raw HTML."""
        if html is None:
            return None, None
        text = None
        source = None
        if backend in ("auto", "trafilatura", "curl"):
            text = await loop.run_in_executor(None, extract_via_trafilatura, html, url)
            if text:
                source = f"{source_prefix}trafilatura"
        if text is None and backend in ("auto", "bs4", "httpx", "curl"):
            text = await loop.run_in_executor(None, extract_via_bs4, html)
            if text:
                source = f"{source_prefix}bs4+html2text"
        return text, source
