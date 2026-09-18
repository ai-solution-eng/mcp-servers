"""Headless-browser rendering client for the SearXNG MCP server.

Drives a Chromium instance over CDP (Chrome DevTools Protocol) to render
JavaScript-heavy pages, which plain HTTP fetching cannot see. The browser
runs as a *sidecar container in the same pod* — all containers in a pod
share a network namespace, so the MCP container reaches it on
``http://127.0.0.1:9222`` with no in-cluster exposure (the CDP port binds
loopback only).

Design points:
* Lazy and fail-soft: playwright is imported on first use; if the package
  or the sidecar is missing, callers get :class:`BrowserUnavailable` and
  the fetcher continues without rendering. A slim install never breaks.
* One browser connection, a bounded number of concurrent pages
  (semaphore), one fresh browser context per render (isolation), contexts
  closed eagerly.
* Resource blocking: images / media / fonts are aborted at the route
  level — 3-5x faster loads and far less memory; text extraction does not
  need them.
* CDP route filter (SSRF, fleet audit P0-6): the sidecar shares the pod's
  network namespace, so a navigation to loopback would reach the MCP
  container and the unauthenticated CDP endpoint itself
  (``127.0.0.1:9222`` — RCE-as-browser-user). The top-level URL is
  validated before ``page.goto`` (loopback/pod-local/metadata targets are
  unreachable in every configuration — the fetch allowlist cannot unlock
  them here), and the route handler additionally aborts any request
  (redirects, iframes, subresources) toward such targets mid-render.

Configuration (environment variables):
  BROWSER_CDP_URL           CDP endpoint (default http://127.0.0.1:9222)
  BROWSER_CONNECT_TIMEOUT   CDP connect timeout ms (default 5000)
  BROWSER_NAV_TIMEOUT_MS    Page navigation timeout ms (default 30000)
  BROWSER_EXTRA_WAIT_MS     Settle time after DOMContentLoaded (default 1500)
  BROWSER_MAX_PAGES         Max concurrent pages (default 3)
  BROWSER_BLOCK_RESOURCES   Block image/media/font requests (default true)
  BROWSER_MAX_HTML_BYTES    Cap on stored serialized DOM (default 5000000)
"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass

import url_policy

RENDER_MODES = ("auto", "always", "never")


class BrowserUnavailable(Exception):
    """The headless browser cannot be used at all (not installed / not
    reachable). Callers should degrade gracefully."""


class BrowserError(Exception):
    """The browser was reachable but the render itself failed (navigation
    timeout, crashed tab, ...). Callers should degrade gracefully."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class RenderedPage:
    """Result of rendering one URL in the headless browser."""

    html: str
    final_url: str
    title: str
    status: int | None = None  # HTTP status of the main document, if known


class BrowserClient:
    """Playwright-over-CDP client for the headless-browser sidecar."""

    def __init__(
        self,
        cdp_url: str | None = None,
        connect_timeout_ms: int | None = None,
        navigation_timeout_ms: int | None = None,
        extra_wait_ms: int | None = None,
        max_pages: int | None = None,
        block_resources: bool | None = None,
        max_html_bytes: int | None = None,
    ):
        self.cdp_url = cdp_url or os.getenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
        self.connect_timeout_ms = (
            connect_timeout_ms if connect_timeout_ms is not None else _env_int("BROWSER_CONNECT_TIMEOUT", 5000)
        )
        self.navigation_timeout_ms = (
            navigation_timeout_ms if navigation_timeout_ms is not None else _env_int("BROWSER_NAV_TIMEOUT_MS", 30000)
        )
        self.extra_wait_ms = extra_wait_ms if extra_wait_ms is not None else _env_int("BROWSER_EXTRA_WAIT_MS", 1500)
        self.max_pages = max_pages if max_pages is not None else _env_int("BROWSER_MAX_PAGES", 3)
        self.block_resources = (
            block_resources if block_resources is not None else _env_bool("BROWSER_BLOCK_RESOURCES", True)
        )
        self.max_html_bytes = (
            max_html_bytes if max_html_bytes is not None else _env_int("BROWSER_MAX_HTML_BYTES", 5_000_000)
        )
        self._pw = None
        self._browser = None
        self._connect_lock = asyncio.Lock()
        self._page_sem = asyncio.Semaphore(max(1, self.max_pages))

    # ------------------------------------------------------------------ io
    async def _ensure_connected(self):
        """Connect lazily; safe to call concurrently; reconnects after the
        sidecar restarted underneath us."""
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._connect_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise BrowserUnavailable(
                    "playwright is not installed in the MCP image "
                    "(build with the 'browser' extra: uv pip install '.[browser]')"
                ) from e
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=self.connect_timeout_ms)
            except Exception as e:
                await self._teardown()
                raise BrowserUnavailable(
                    f"headless browser not reachable at {self.cdp_url} ({type(e).__name__}: {e})"
                ) from e

    async def _teardown(self):
        browser, self._browser = self._browser, None
        pw, self._pw = self._pw, None
        for closer in (
            getattr(browser, "close", None),
            getattr(pw, "stop", None),
        ):
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    async def aclose(self):
        await self._teardown()

    # -------------------------------------------------------------- render
    async def render(
        self,
        url: str,
        *,
        screenshot: bool = False,
        full_page: bool = False,
    ) -> tuple[RenderedPage, bytes | None]:
        """Render ``url`` with JS execution and return the serialized DOM
        (plus an optional PNG screenshot). Raises BrowserUnavailable /
        BrowserError; one automatic reconnect is attempted if the CDP
        connection dropped.

        SSRF guard: the URL is validated (same policy as the plain fetch
        path) right before navigation. Loopback/pod-local/metadata targets —
        the CDP endpoint at 127.0.0.1:9222 above all — are refused in every
        configuration and never reach ``page.goto``.
        """
        try:
            return await self._render_once(url, screenshot=screenshot, full_page=full_page)
        except (BrowserUnavailable, BrowserError):
            raise
        except Exception:
            # Stale connection (sidecar restarted): drop it and retry once.
            await self._teardown()
            try:
                return await self._render_once(url, screenshot=screenshot, full_page=full_page)
            except BrowserUnavailable:
                raise
            except Exception as e2:
                raise BrowserError(f"{type(e2).__name__}: {e2}") from e2

    async def _render_once(self, url, *, screenshot, full_page):
        await self._ensure_connected()
        # SSRF gate: after the CDP connect (which is not a navigation), but
        # strictly before page.goto / any page-level network activity.
        try:
            await url_policy.validate_fetch_url_async(url, for_browser=True)
        except url_policy.UrlPolicyError as e:
            raise BrowserError(f"SSRF policy blocked navigation to {url}: {e}") from None
        async with self._page_sem:
            context = await self._browser.new_context()
            page = await context.new_page()
            try:
                if self.block_resources:
                    await context.route("**/*", self._route_filter)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
                # Give SPA frameworks a moment to hydrate; fixed and bounded
                # (networkidle hangs on long-polling pages).
                if self.extra_wait_ms > 0:
                    await page.wait_for_timeout(self.extra_wait_ms)
                html = await page.content()
                if len(html) > self.max_html_bytes:
                    html = html[: self.max_html_bytes]
                shot = None
                if screenshot:
                    raw = await page.screenshot(type="png", full_page=full_page)
                    shot = base64.b64encode(raw).decode("ascii")
                status = None
                try:
                    if response is not None:
                        status = response.status
                except Exception:
                    status = None
                return (
                    RenderedPage(
                        html=html,
                        final_url=page.url,
                        title=(await page.title()) or "",
                        status=status,
                    ),
                    shot,
                )
            except BrowserUnavailable:
                raise
            except Exception as e:
                # A navigation timeout / crashed tab is a render failure,
                # not an availability problem.
                raise BrowserError(f"{type(e).__name__}: {e}") from e
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _route_filter(self, route):
        """Per-request guard: pod-local/loopback/metadata targets are aborted
        outright (redirect targets, iframes and subresources included — the
        pre-goto validation only saw the first URL), then the usual
        image/media/font blocking applies."""
        try:
            if url_policy.is_pod_local_url(route.request.url):
                await route.abort()
                return
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            # A raced abort/continue on a dying page must never fail the
            # render — the request pipeline is best-effort by design.
            pass
