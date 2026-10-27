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
        # Per-process cooldown pushed in on HTTP 429 (see _request): until
        # this monotonic deadline, acquire() waits instead of admitting. This
        # is deliberately local state — the upstream SearXNG limiter stays
        # OFF (settings.yml server.limiter: false, no valkey), so the
        # per-IP engine budget is protected only by client-side pacing.
        self._cooldown_until: float = 0.0

    def push_cooldown(self, seconds: float) -> None:
        """Pause local admission for ``seconds`` (a 429's Retry-After)."""
        try:
            wait = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        deadline = time.monotonic() + wait
        if wait and deadline > self._cooldown_until:
            self._cooldown_until = deadline

    async def acquire(self) -> None:
        now = time.monotonic()
        if now < self._cooldown_until:
            await asyncio.sleep(self._cooldown_until - now)
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


def _parse_retry_after(value: str | None) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds, or None.

    Accepts the two RFC 7231 forms — delay-seconds (``"30"``) and HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``) — and tolerates missing or
    garbage values (None) so a bad header never blocks the error path.
    Relative dates in the past yield 0.0 (retry now); a negative or
    unparseable delta is None.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        # "inf"/"1e400"-style garbage parses as float but would OverflowError
        # on int() downstream — treat non-finite as unparseable.
        from math import isfinite

        if not isfinite(seconds):
            return None
        return seconds if seconds >= 0 else None
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # RFC 7231 dates always carry GMT; treat a naive one as UTC rather
        # than guessing the local zone.
        from datetime import UTC

        when = when.replace(tzinfo=UTC)
    delta = when.timestamp() - time.time()
    return delta if delta >= 0 else 0.0


class SearXNGClient:
    """Talks to one SearXNG instance over its JSON API.

    ``transport`` is a test hook (httpx2.MockTransport) — leave None in
    production.
    """

    # A 429 Retry-After larger than this is treated as garbage: blocking
    # local admission for minutes on one bad header hurts more than the
    # extra request it saves. The error text still reports the raw value.
    MAX_COOLDOWN_SECONDS = 120.0

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
        # Engine list from GET /config, fetched lazily (None = unknown —
        # fetch not yet attempted or last one failed; retried on a later
        # call). See engines_from_config_hint().
        self._engine_names: set[str] | None = None
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

    async def _engine_names_from_config(self) -> set[str] | None:
        """Engine names from ``GET {base_url}/config``, fetched once per
        process (cached; any failure → None, silently — the sanity hint is
        best-effort diagnostics, never a hard dependency on /config).
        Cache values are ``set[str] | None``: None = unknown (fetch failed
        or not yet attempted), a set = the engine list. A failed fetch is
        retried on a LATER call (cheap, diagnostics-only) rather than
        being pinned False for the life of the process."""
        if self._engine_names is not None:
            return self._engine_names
        try:
            resp = await self._client.get(f"{self.base_url}/config")
            if resp.status_code != 200:
                return None
            data = resp.json()
            engines = data.get("engines") if isinstance(data, dict) else None
            names: set[str] = set()
            for e in engines or []:
                if isinstance(e, dict) and e.get("name"):
                    names.add(str(e["name"]))
            self._engine_names = names or None
        except Exception:  # diagnostics only, never fatal
            return None
        return self._engine_names or None

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
        """GET /search; returns parsed JSON, raises SearXNGError on
        anything but a retryable 400.

        Only HTTP 400 returns None (the caller retries once without the
        language filter — some SearXNG builds reject unknown language codes
        with a 400). Every other 4xx is a request problem no retry fixes
        (401 wrong API key, 404 wrong path, 405 wrong method, …), so it
        raises with the specific status instead of being retried identically
        and reported generically.
        """
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
            retry_after = _parse_retry_after(resp.headers.get("Retry-After", ""))
            if retry_after is not None:
                # Per-process pacing only (no valkey/shared state — the
                # upstream limiter stays off by design): pause local
                # admission until the server says the window resets, so the
                # next search instead of racing straight back into 429.
                self.rate_limiter.push_cooldown(min(retry_after, self.MAX_COOLDOWN_SECONDS))
            if retry_after is not None and retry_after > 0:
                raise SearXNGError(
                    f"SearXNG returned 429 (rate limited); slow down — retry after ~{int(retry_after)}s."
                )
            raise SearXNGError("SearXNG returned 429 (rate limited); slow down.")
        if resp.status_code == 400:
            # The ONLY retryable 4xx (the caller drops the language filter
            # once). Other 4xx fall through to the named raise below.
            return None
        if 400 <= resp.status_code < 500:
            raise SearXNGError(f"SearXNG returned {resp.status_code}; not retrying.")
        if resp.status_code != 200:
            raise SearXNGError(f"SearXNG returned HTTP {resp.status_code}; not retrying.")

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


def engines_from_config_hint(engines_param: str, available: set[str] | None) -> str:
    """Sanity note for an EMPTY search made with ``backend`` != auto.

    backend values beyond "auto" are engine allowlists, and a typo'd engine
    name ("braveimages" vs "brave.images" — names must match GET /config
    exactly) silently returns zero results. When the instance's engine list
    is known and NONE of the named engines exist, return the hint line;
    otherwise "" (no hint when anything matches, or when /config is
    unknown/unreachable — best-effort diagnostics only).
    """
    if not available or not engines_param:
        return ""
    named = [e.strip() for e in engines_param.split(",") if e.strip()]
    if not named or any(e in available for e in named):
        return ""
    return (
        f"\nNote: engines you named: {engines_param} — none match SearXNG's engine list; "
        "check spelling (e.g. 'brave.images', not 'braveimages')."
    )


def format_search_response(
    resp: SearXNGResponse,
    max_results: int,
    *,
    engine_hint: str = "",
) -> str:
    """Format a SearXNGResponse for an LLM, mirroring the ddgs-lite layout."""
    results = resp.results[:max_results]

    if not results and not resp.answers:
        # Keep the leading sentence BYTE-STABLE (downstream grep/tests match
        # it); everything below is APPENDED guidance for the retry-loop trap:
        # an empty result used to land here with nothing to act on, so agents
        # re-ran the identical query (a documented fleet lesson). Surface the
        # corrections/suggestions the response already carried, and point at
        # the knobs that actually change the outcome.
        msg = "No results were found. Try rephrasing your search query."
        if resp.unresponsive_engines:
            names = ", ".join(e for e, _ in resp.unresponsive_engines)
            msg += f" (engines unavailable: {names})"
            if any("uspended" in r or "too many requests" in r.lower() for _, r in resp.unresponsive_engines):
                msg += (
                    "\nNote: some engines are temporarily suspended/rate-limited; retrying in a few minutes may help."
                )
        if resp.corrections:
            msg += f"\nDid you mean: {resp.corrections[0]}?"
        if resp.suggestions:
            msg += "\nRelated searches: " + " | ".join(resp.suggestions[:5])
        if engine_hint:
            msg += engine_hint
        msg += (
            "\nNext step: do not re-run the identical query — change category, "
            "region, or time_range, or fetch a known good URL directly."
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
