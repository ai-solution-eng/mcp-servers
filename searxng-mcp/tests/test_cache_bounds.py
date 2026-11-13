"""Cache-bound tests for the 2026-11 searxng-mcp anonymous-heap fix.

The live pod grew 269 MB -> 481 MB in 10 days on a 512Mi limit because the
fetch result cache was bounded only by entry COUNT (64), never by bytes, and
TTL-expired entries were removed only when their own key was looked up again.
These tests pin the replacement contract: byte budget, per-entry cap, expired
sweep on insert, LRU recency, and env wiring.
"""

import asyncio
import time

from searxng_mcp.fetcher import FetchOutcome, FetchResultCache, WebContentFetcher
from searxng_mcp.searxng_client import (
    SearchResult,
    SearchResultCache,
    SearXNGClient,
    SearXNGResponse,
)


def _outcome(text: str) -> FetchOutcome:
    return FetchOutcome(text=text, source="trafilatura")


def _run(cache, key, outcome):
    async def go():
        return await cache.run(key, lambda: outcome)

    return asyncio.run(go())


def _response_with_snippet(snippet: str) -> SearXNGResponse:
    return SearXNGResponse(
        query="q",
        results=[SearchResult(title="t", url="https://example.com", snippet=snippet, position=1)],
    )


# ---------------------------------------------------------------------------
# Fetch cache
# ---------------------------------------------------------------------------


def test_fetch_cache_byte_budget_evicts_until_under_budget():
    cache = FetchResultCache(ttl_seconds=300, max_entries=64, max_bytes=2000, max_entry_bytes=10_000)
    for i in range(10):
        cache._insert((f"u{i}",), _outcome("x" * 500))
    assert cache.bytes <= 2000
    assert len(cache._store) <= 4
    # The newest entries are the survivors (eviction from the front).
    assert ("u9",) in cache._store


def test_fetch_cache_skips_oversize_entry_but_still_returns_it():
    cache = FetchResultCache(ttl_seconds=300, max_entries=64, max_bytes=10_000, max_entry_bytes=100)
    big = _outcome("y" * 5000)
    outcome, hit = _run(cache, ("big",), big)
    assert hit is False and outcome.text == big.text
    assert ("big",) not in cache._store
    assert cache.bytes == 0


def test_fetch_cache_sweeps_expired_entries_on_insert():
    cache = FetchResultCache(ttl_seconds=60, max_entries=64)
    cache._insert(("old",), _outcome("old"))
    # Force the entry past its TTL without waiting.
    _expiry, size, outcome = cache._store[("old",)]
    cache._store[("old",)] = (time.monotonic() - 1, size, outcome)
    cache._insert(("new",), _outcome("new"))
    assert ("old",) not in cache._store  # swept on insert, not on its own key
    assert ("new",) in cache._store
    assert cache.bytes == cache._store[("new",)][1]  # old bytes released


def test_fetch_cache_lru_hit_survives_eviction():
    cache = FetchResultCache(ttl_seconds=300, max_entries=2, max_bytes=10_000)
    cache._insert(("a",), _outcome("a"))
    cache._insert(("b",), _outcome("b"))
    assert cache._peek(("a",)) is not None  # refresh recency
    cache._insert(("c",), _outcome("c"))
    assert ("a",) in cache._store
    assert ("b",) not in cache._store


def test_fetch_cache_entry_count_bound():
    cache = FetchResultCache(ttl_seconds=300, max_entries=3, max_bytes=0)
    for i in range(10):
        cache._insert((f"k{i}",), _outcome("t"))
    assert len(cache._store) == 3


def test_fetch_cache_zero_ttl_disables_caching():
    cache = FetchResultCache(ttl_seconds=0)
    cache._insert(("k",), _outcome("t"))
    assert cache._store == {}
    assert cache.bytes == 0


def test_fetch_cache_env_defaults(monkeypatch):
    monkeypatch.setenv("SEARXNG_FETCH_CACHE_MAX_BYTES", "1234")
    monkeypatch.setenv("SEARXNG_FETCH_CACHE_MAX_ENTRIES", "7")
    monkeypatch.setenv("SEARXNG_FETCH_CACHE_MAX_ENTRY_BYTES", "99")
    fetcher = WebContentFetcher()
    assert fetcher.result_cache.max_bytes == 1234
    assert fetcher.result_cache.max_entries == 7
    assert fetcher.result_cache.max_entry_bytes == 99


# ---------------------------------------------------------------------------
# Search cache
# ---------------------------------------------------------------------------


def test_search_cache_byte_budget_and_oversize_skip():
    # A minimal response is already ~1 KB of Python objects; the caps below are
    # sized around that floor so the small response is cached and the big one is not.
    cache = SearchResultCache(ttl_seconds=120, max_entries=64, max_bytes=20_000, max_entry_bytes=2_000)
    cache._insert(("k0",), _response_with_snippet("s" * 50))
    assert cache.bytes > 0
    # A response over the per-entry cap is not stored.
    cache._insert(("big",), _response_with_snippet("s" * 5000))
    assert ("big",) not in cache._store
    assert cache.bytes <= 20_000


def test_search_cache_sweeps_expired_on_insert():
    cache = SearchResultCache(ttl_seconds=60)
    cache._insert(("old",), _response_with_snippet("x"))
    _expiry, size, resp = cache._store[("old",)]
    cache._store[("old",)] = (time.monotonic() - 1, size, resp)
    cache._insert(("new",), _response_with_snippet("y"))
    assert ("old",) not in cache._store
    assert ("new",) in cache._store


def test_search_cache_env_defaults(monkeypatch):
    monkeypatch.setenv("SEARXNG_SEARCH_CACHE_MAX_BYTES", "4321")
    monkeypatch.setenv("SEARXNG_SEARCH_CACHE_MAX_ENTRIES", "5")
    monkeypatch.setenv("SEARXNG_SEARCH_CACHE_MAX_ENTRY_BYTES", "77")
    client = SearXNGClient(base_url="http://localhost:8080")
    assert client.result_cache.max_bytes == 4321
    assert client.result_cache.max_entries == 5
    assert client.result_cache.max_entry_bytes == 77


def test_search_cache_hit_still_copies_response():
    cache = SearchResultCache(ttl_seconds=120)
    cache._insert(("k",), _response_with_snippet("abc"))
    first = cache._peek(("k",))
    second = cache._peek(("k",))
    assert first is not None and second is not None and first is not second
    first.query = "mutated"
    assert cache._store[("k",)][2].query == "q"
