"""Async client for a SearXNG instance's JSON search API.

Wraps the SearXNG ``/search`` endpoint (``format=json``) behind a small
typed interface: region normalization (ddgs-style codes keep working),
engine/category selection, and structured parsing of results, answers,
suggestions, corrections and unresponsive engines.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field, replace

import httpx2


class SearXNGError(Exception):
    """Raised when the SearXNG instance cannot be queried or returns an error."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    position: int
    engines: list[str] = field(default_factory=list)
    category: str = ""


@dataclass
class SearXNGResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    infoboxes: list[dict] = field(default_factory=list)
    unresponsive_engines: list[tuple[str, str]] = field(default_factory=list)
    number_of_results: int = 0
    cache_hit: bool = False


class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < 60.0]
        if len(self._timestamps) >= self.requests_per_minute:
            wait = 60.0 - (now - self._timestamps[0])
            if wait > 0:
                await asyncio.sleep(wait)
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
        self._timestamps.append(now)


class SearchResultCache:
    """Tiny TTL cache + single-flight coalescer for search outcomes.

    Mirrors fetcher.FetchResultCache. Shared multi-tenant instances get many
    identical/duplicate queries (several agent sessions on the same cluster);
    each miss fans out to every enabled engine through ONE corporate-egress
    IP, so every avoided duplicate search is a meaningful chunk of the
    upstream rate-limit budget.

    * TTL: entries expire after ``ttl_seconds`` (0 disables caching entirely).
    * Single flight: concurrent calls with the same key share ONE upstream
      run — later callers await the same task instead of racing the engines.
    * Failures are coalesced but never memoized: an exception propagates to
      every waiter, but the next call after the failure retries upstream.
    * Bounded to ``max_entries`` (oldest evicted).
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 64):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._store: dict[tuple, tuple[float, SearXNGResponse]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def _peek(self, key) -> SearXNGResponse | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expiry, resp = entry
        if expiry <= time.monotonic():
            self._store.pop(key, None)
            return None
        # Copy on read: callers mutate their response (cache_hit flag), and
        # one shared instance must never alias between callers.
        return replace(resp)

    async def run(self, key, factory, cacheable=None) -> tuple[SearXNGResponse, bool]:
        """Run ``factory()`` once per key within the TTL window; returns
        ``(response, cache_hit)``."""
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
                    resp = done.result()
                    if cacheable is None or cacheable(resp):
                        self._store[key] = (
                            time.monotonic() + self.ttl_seconds,
                            resp,
                        )
                        while len(self._store) > self.max_entries:
                            self._store.pop(next(iter(self._store)))

                task.add_done_callback(_publish)
        self.misses += 1
        resp = await asyncio.shield(task)
        # Copy per caller (same aliasing concern as _peek): waiters share the
        # one task result otherwise.
        return replace(resp), False


async def _run_factory(factory):
    result = factory()
    if asyncio.iscoroutine(result):
        result = await result
    return result


# Minimal ISO-639-1 language code set, used to disambiguate ddgs-style
# region codes ("us-en" = country-language) from native SearXNG locales
# ("zh-CN" = language-country) when both halves are two letters.
_LANGUAGES = {
    "af",
    "am",
    "ar",
    "az",
    "be",
    "bg",
    "bn",
    "bs",
    "ca",
    "cs",
    "cy",
    "da",
    "de",
    "el",
    "en",
    "eo",
    "es",
    "et",
    "eu",
    "fa",
    "fi",
    "fr",
    "ga",
    "gd",
    "gl",
    "gu",
    "he",
    "hi",
    "hr",
    "ht",
    "hu",
    "hy",
    "id",
    "is",
    "it",
    "ja",
    "ka",
    "kk",
    "km",
    "kn",
    "ko",
    "ky",
    "lo",
    "lt",
    "lv",
    "mg",
    "mk",
    "ml",
    "mn",
    "mr",
    "ms",
    "mt",
    "my",
    "ne",
    "nl",
    "no",
    "pa",
    "pl",
    "ps",
    "pt",
    "ro",
    "ru",
    "sd",
    "si",
    "sk",
    "sl",
    "so",
    "sq",
    "sr",
    "sv",
    "sw",
    "ta",
    "te",
    "tg",
    "th",
    "tl",
    "tr",
    "uk",
    "ur",
    "uz",
    "vi",
    "xh",
    "yi",
    "zh",
}


def normalize_language(value: str) -> str:
    """Normalize a region/language code to a SearXNG ``language`` value.

    Accepts both conventions so existing ddgs-style callers keep working:

    - ddgs-style ``<country>-<lang>``:  ``us-en`` -> ``en-US``, ``de-de``
      -> ``de-DE``, ``uk-en`` -> ``en-GB``
    - native SearXNG locales (passthrough): ``en-US``, ``zh-CN``, ``pt-BR``,
      or bare language codes like ``en``, ``de``
    - worldwide markers: ``wt-wt`` (ddgs "no region") -> ``all``

    Empty input returns "" (caller decides the default).
    """
    v = (value or "").strip()
    if not v:
        return ""
    low = v.lower().replace("_", "-")
    if low in ("all", "wt-wt", "*"):
        return "all"
    parts = low.split("-")
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
        a, b = parts
        if a == b:
            # de-de, fr-fr ... same result under either reading
            return f"{a}-{b.upper()}"
        if a in _LANGUAGES and b not in _LANGUAGES:
            # native form with lowercase country, e.g. zh-cn -> zh-CN
            return f"{a}-{b.upper()}"
        if b in _LANGUAGES:
            # ddgs form <country>-<lang>, e.g. us-en -> en-US, uk-en -> en-GB
            country = "GB" if a == "uk" else a.upper()
            return f"{b}-{country}"
    return v


class SearXNGClient:
    """Talks to one SearXNG instance over its JSON API.

    ``transport`` is a test hook (httpx2.MockTransport) — leave None in
    production.
    """

    def __init__(
        self,
        base_url: str,
        default_language: str = "en-US",
        timeout: float = 10.0,
        verify_tls: bool = True,
        requests_per_minute: int = 30,
        cache_ttl_seconds: float | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_language = default_language
        self.rate_limiter = RateLimiter(requests_per_minute)
        if cache_ttl_seconds is not None:
            ttl = float(cache_ttl_seconds)
        else:
            try:
                ttl = float(os.getenv("SEARXNG_SEARCH_CACHE_TTL", "120"))
            except ValueError:
                ttl = 120.0
        self.result_cache = SearchResultCache(ttl_seconds=ttl)
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(timeout),
            verify=verify_tls,
            # In-cluster the instance is reached over plain HTTP on localhost
            # or a .svc.cluster.local address — environment proxies must NOT
            # intercept that. Set trust_env=True only for exotic setups.
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "searxng-mcp/1.0",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        language: str = "",
        engines: str = "",
        categories: str = "general",
        safesearch: int = 0,
        time_range: str = "",
        pageno: int = 1,
    ) -> SearXNGResponse:
        """Run one search. Raises SearXNGError on transport/HTTP/parse errors.

        Cached per exact request params for ``SEARXNG_SEARCH_CACHE_TTL``
        seconds (default 120, 0 disables). Concurrent identical searches
        share one upstream fan-out; errors are coalesced but never memoized.
        """
        params: dict[str, str] = {
            "q": query,
            "format": "json",
            "safesearch": str(max(0, min(2, int(safesearch)))),
            "pageno": str(max(1, int(pageno))),
        }
        lang = normalize_language(language) if language else self.default_language
        if lang:
            params["language"] = lang
        if engines:
            params["engines"] = engines
        if categories:
            params["categories"] = categories
        if time_range:
            params["time_range"] = time_range

        key = tuple(sorted(params.items()))

        async def _run() -> SearXNGResponse:
            await self.rate_limiter.acquire()
            data = await self._request(params)
            if data is None:
                # Some SearXNG builds reject unknown language codes with a
                # 400 — retry once without the language filter rather than
                # failing. (Re-run with the mutated params; the cache key is
                # already fixed and only successful parses are memoized.)
                params.pop("language", None)
                data = await self._request(params)
                if data is None:
                    raise SearXNGError(f"SearXNG request failed for query: {query!r}")
            return self._parse(data, query)

        resp, cache_hit = await self.result_cache.run(
            key,
            _run,
            cacheable=lambda r: True,  # errors raise; only successes arrive
        )
        resp.cache_hit = cache_hit
        return resp

    async def _request(self, params: dict[str, str]) -> dict | None:
        """GET /search; returns parsed JSON, or None on HTTP 4xx (caller may
        retry), or raises SearXNGError for anything else."""
        try:
            resp = await self._client.get(f"{self.base_url}/search", params=params)
        except httpx2.HTTPError as e:
            raise SearXNGError(f"could not reach SearXNG at {self.base_url}: {e}") from e

        if resp.status_code == 403:
            raise SearXNGError(
                "SearXNG returned 403 — the 'json' output format is likely not "
                "enabled. Add 'json' to search.formats in settings.yml."
            )
        if resp.status_code == 429:
            raise SearXNGError("SearXNG returned 429 (rate limited); slow down.")
        if 400 <= resp.status_code < 500:
            return None  # retryable by the caller (e.g. drop language)
        if resp.status_code != 200:
            raise SearXNGError(f"SearXNG returned HTTP {resp.status_code}")

        try:
            return resp.json()
        except ValueError as e:
            raise SearXNGError(
                "SearXNG response was not valid JSON — is 'json' listed in search.formats in settings.yml?"
            ) from e

    @staticmethod
    def _parse(data: dict, query: str) -> SearXNGResponse:
        results: list[SearchResult] = []
        for i, r in enumerate(data.get("results", [])):
            results.append(
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("content", ""),
                    position=i + 1,
                    engines=list(r.get("engines") or [r.get("engine", "")]),
                    category=r.get("category", ""),
                )
            )

        unresponsive: list[tuple[str, str]] = []
        for entry in data.get("unresponsive_engines", []):
            if isinstance(entry, (list, tuple)) and entry:
                engine = str(entry[0])
                reason = str(entry[1]) if len(entry) > 1 else ""
            else:
                engine, reason = str(entry), ""
            unresponsive.append((engine, reason))

        return SearXNGResponse(
            query=str(data.get("query", query)),
            results=results,
            answers=[str(a) for a in data.get("answers", [])],
            corrections=[str(c) for c in data.get("corrections", [])],
            suggestions=[str(s) for s in data.get("suggestions", [])],
            infoboxes=list(data.get("infoboxes", [])),
            unresponsive_engines=unresponsive,
            number_of_results=int(data.get("number_of_results", 0) or 0),
        )


def format_search_response(resp: SearXNGResponse, max_results: int) -> str:
    """Format a SearXNGResponse for an LLM, mirroring the ddgs-lite layout."""
    results = resp.results[:max_results]

    if not results and not resp.answers:
        msg = "No results were found. Try rephrasing your search query."
        if resp.unresponsive_engines:
            names = ", ".join(e for e, _ in resp.unresponsive_engines)
            msg += f" (engines unavailable: {names})"
            if any("uspended" in r or "too many requests" in r.lower() for _, r in resp.unresponsive_engines):
                msg += (
                    "\nNote: some engines are temporarily suspended/rate-limited; retrying in a few minutes may help."
                )
        return msg

    output = [f"Found {len(results)} search results:\n"]
    for r in results:
        output.append(f"{r.position}. {r.title}")
        output.append(f"   URL: {r.url}")
        if r.snippet:
            output.append(f"   Summary: {r.snippet}")
        output.append("")

    if resp.answers:
        for a in resp.answers[:3]:
            output.append(f"Answer: {a}")

    if resp.corrections:
        output.append(f"Did you mean: {resp.corrections[0]}?")

    if resp.suggestions:
        output.append("Related searches: " + " | ".join(resp.suggestions[:5]))

    if resp.unresponsive_engines:
        details = ", ".join(f"{e} ({r})" if r else e for e, r in resp.unresponsive_engines)
        output.append(f"Note: some engines did not respond: {details}")

    if results:
        engines = sorted({e for r in results for e in r.engines if e})
        if engines:
            output.append(f"Source engines: {', '.join(engines)}")

    return "\n".join(output)
