"""Fleet decision D17 stage 1 — ddgs_lite deprecation warning (Wave-3 C3).

One DeprecationWarning per process, emitted at TOOL-CALL time (first tool
call only); tool behavior is otherwise unchanged. This file runs BOTH ways:

  ddgs_lite/.venv/bin/python tests/test_deprecation.py
      the REAL run: ddgs_lite's own venv has the runtime deps (ddgs, httpx,
      mcp) but no pytest, so the standalone runner below executes the same
      assertions. This is the form the wave report's numbers come from.

  python -m pytest tests/test_deprecation.py
      pytest form: skips cleanly in venvs without the ddgs runtime dep (the
      fleet unit-test venv has no ddgs) instead of failing collection.
"""

import asyncio
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # pytest is OPTIONAL: ddgs_lite's own venv runs this file standalone
    import pytest
except ImportError:  # pragma: no cover — standalone mode
    pytest = None  # type: ignore[assignment]  # optional dep: None in the standalone venv (use site is guarded)

try:
    import server
except Exception as _import_error:  # pragma: no cover — env-dependent
    if pytest is not None:
        pytest.skip(
            f"ddgs_lite server not importable in this venv: {_import_error}",
            allow_module_level=True,
        )
    raise  # standalone mode: a broken import IS the failure

EXPECTED_PREFIX = "ddgs_lite is deprecated — use searxng_mcp"
EXPECTED_TAIL = "archive pending (fleet decision D17)"


class _StubSearcher:
    """Stands in for the network-backed Metasearcher (offline smoke)."""

    async def search(self, *args, **kwargs):
        return [], ""

    def format_results(self, results, error_info=""):
        return "stub-search-ok"


class _StubFetcher:
    """Stands in for the network-backed WebContentFetcher (offline smoke)."""

    async def fetch_and_parse(self, *args, **kwargs):
        return "stub-fetch-ok"


def _call_text(tool, **kwargs):
    """Call one tool through the real MCP dispatch and return the text the
    client sees (call_tool returns the converted CallToolResult)."""
    result = asyncio.run(server.mcp.call_tool(tool, kwargs))
    if isinstance(result, str):
        return result
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and len(structured) == 1:
        only = next(iter(structured.values()))
        if isinstance(only, str):
            return only
    content = getattr(result, "content", None)
    if content and hasattr(content[0], "text"):
        return content[0].text
    return result


def _assertions():
    """The actual checks — shared by the pytest test and the standalone run.

    The stubs keep the smoke offline: the warning fires at tool-call time,
    BEFORE the tool body would touch the network, and tool behavior (the
    return value) is unchanged — that is the whole point of D17 stage 1.
    """
    real_searcher, real_fetcher = server.searcher, server.fetcher
    server.searcher = _StubSearcher()
    server.fetcher = _StubFetcher()
    try:
        # Reset the once-per-process latch so the run observes the FIRST
        # warning (a fresh process would start un-warned).
        server._deprecation_warned = False
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            first = _call_text("search", query="deprecation smoke")
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1, (
            f"expected exactly 1 DeprecationWarning on the first tool call, "
            f"got {len(deprecations)}: {[str(w.message) for w in caught]}"
        )
        message = str(deprecations[0].message)
        assert message.startswith(EXPECTED_PREFIX), message
        assert EXPECTED_TAIL in message, message
        assert first == "stub-search-ok", first  # behavior unchanged

        # One PER PROCESS: the second call — through the OTHER tool — adds
        # no warning, and its behavior is also unchanged.
        with warnings.catch_warnings(record=True) as caught_2:
            warnings.simplefilter("always")
            second = _call_text("fetch_content", url="https://example.invalid/x")
        assert not [w for w in caught_2 if issubclass(w.category, DeprecationWarning)], [
            str(w.message) for w in caught_2
        ]
        assert second == "stub-fetch-ok", second
    finally:
        server.searcher, server.fetcher = real_searcher, real_fetcher
        server._deprecation_warned = False  # leave the module as found


def test_deprecation_warning_once_per_process():
    _assertions()


if __name__ == "__main__":
    _assertions()
    print(
        "ddgs_lite deprecation smoke: PASS "
        "(1 DeprecationWarning on the first tool call, 0 on the second, "
        "tool behavior unchanged)"
    )
