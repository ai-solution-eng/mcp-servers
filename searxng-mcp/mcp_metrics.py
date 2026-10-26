"""Tiny, honest self-metrics for searxng-mcp (Wave-3 C3 — fleet item 11).

Design rules (FLEET-EXECUTION-PLAN-2026-09 §7 C3):

* ADDITIVE and chart-gated DEFAULT-OFF. Nothing here changes any request's
  behavior; the /metrics ROUTE exists only when the chart sets
  SEARXNG_METRICS_ENABLED=true (values: metrics.enabled), so the default
  deployment has no metrics surface at all.
* prometheus-client when importable, a dependency-free fallback otherwise
  (the SQLhandler import-guard fleet pattern): a metrics failure must never
  break a tool call, and the server must not grow a hard dependency for
  metrics. Which backend is active changes nothing else about the server.
* Small and honest: ONE counter family — per-tool request counts with an
  outcome label (ok | error). No arguments, file names, workspace names,
  env values, or error text are ever exported.

Counting scope: MCP-protocol tool calls (wrapped ``MCPServer.call_tool`` —
one insertion point covering every registered tool, present and future).
The web console reuses the same core functions directly (not via the MCP
layer), so UI traffic is intentionally not counted; these numbers measure
agent/MCP traffic.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("searxng-mcp.metrics")

METRIC_PREFIX = "searxng_mcp"
TOOL_REQUESTS = METRIC_PREFIX + "_tool_requests_total"
TOOL_REQUESTS_HELP = "searxng-mcp MCP tool calls, by tool and outcome."
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

try:  # preferred backend: the real prometheus_client, when the env has it
    from prometheus_client import CollectorRegistry
    from prometheus_client import Counter as _PromCounter

    _HAVE_PROMETHEUS_CLIENT = True
except ImportError:  # dependency-free fallback, same 0.0.4 text exposition
    _HAVE_PROMETHEUS_CLIENT = False


class _MiniCounter:
    """Thread-safe labeled counter rendering the Prometheus text format.

    The fallback for environments without prometheus_client (the per-app
    venvs and the fleet unit-test venv): same exposition, zero dependencies.
    """

    def __init__(self, name: str, documentation: str, labelnames: tuple[str, ...]):
        self._name = name
        self._documentation = documentation
        self._labelnames = tuple(labelnames)
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, labelvalues: tuple[str, ...], amount: float = 1.0) -> None:
        with self._lock:
            self._values[labelvalues] = self._values.get(labelvalues, 0.0) + amount

    def render(self) -> str:
        with self._lock:
            values = sorted(self._values.items())
        lines = [
            f"# HELP {self._name} {self._documentation}",
            f"# TYPE {self._name} counter",
        ]
        for labelvalues, value in values:
            labels = ",".join(f'{name}="{val}"' for name, val in zip(self._labelnames, labelvalues))
            lines.append(f"{self._name}{{{labels}}} {value}")
        return "\n".join(lines) + "\n"


class Metrics:
    """One counter family, two interchangeable backends (see module doc)."""

    def __init__(self) -> None:
        self._registry: CollectorRegistry | None
        self._prom: _PromCounter | None
        self._mini: _MiniCounter | None
        if _HAVE_PROMETHEUS_CLIENT:
            # Private registry: no process/GC collectors, and no
            # double-registration if the module is imported twice (tests).
            self._registry = CollectorRegistry()
            self._prom = _PromCounter(
                TOOL_REQUESTS,
                TOOL_REQUESTS_HELP,
                ("tool", "outcome"),
                registry=self._registry,
            )
            self._mini = None
        else:
            self._registry = None
            self._prom = None
            self._mini = _MiniCounter(TOOL_REQUESTS, TOOL_REQUESTS_HELP, ("tool", "outcome"))

    def inc(self, tool: str, outcome: str) -> None:
        """Count one tool call (best-effort: never raises)."""
        try:
            if self._prom is not None:
                self._prom.labels(tool=tool, outcome=outcome).inc()
            elif self._mini is not None:
                self._mini.inc((tool, outcome))
        except Exception:  # metrics must never break a tool call
            logger.debug("metrics increment failed", exc_info=True)

    def render(self) -> tuple[str, str]:
        """Exposition body + content type (best-effort: never raises)."""
        try:
            if self._prom is not None and self._registry is not None:
                from prometheus_client import generate_latest

                return generate_latest(self._registry).decode("utf-8"), CONTENT_TYPE
            if self._mini is not None:
                return self._mini.render(), CONTENT_TYPE
            return "", CONTENT_TYPE
        except Exception:  # metrics must never break a request
            logger.debug("metrics render failed", exc_info=True)
            return "", CONTENT_TYPE


METRICS = Metrics()  # process-wide registry (module import — cheap, inert)


def _result_is_error(result, error_result) -> bool:
    """Decide the outcome label for a (possibly converted) tool result.

    ``call_tool`` returns the MCP conversion of the tool's own return — a
    CallToolResult with an ``is_error`` flag — and tools may ALSO report
    failures as plain "Error: ..." strings with is_error=False. Order:
    protocol-level is_error wins; then *error_result* is applied to the
    tool's own text (unwrapped from the conversion), when one is given.
    """
    if getattr(result, "is_error", False):
        return True
    if error_result is None:
        return False
    if not isinstance(result, str):
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and len(structured) == 1:
            only = next(iter(structured.values()))
            if isinstance(only, str):
                result = only
        if not isinstance(result, str):
            content = getattr(result, "content", None)
            if content and hasattr(content[0], "text"):
                result = content[0].text
    try:
        return bool(error_result(result))
    except Exception:  # a broken predicate must never break a tool call
        logger.debug("metrics error_result predicate failed", exc_info=True)
        return False


def instrument(mcp_server, *, error_result=None) -> None:
    """Count every MCP-protocol tool call made through *mcp_server*.

    One insertion point instead of a decorator on every tool: wraps the
    instance's ``call_tool`` — ``MCPServer._handle_call_tool`` resolves
    ``self.call_tool`` at call time, so the wrapper covers every registered
    tool on both stdio and streamable-http transports.

    outcome="error" counts calls that RAISE at the MCP layer (argument
    validation and tool errors); when *error_result* is given, a returned
    result matching it also counts as "error" (servers that report tool
    failures as strings instead of raising).

    Idempotent (double instrumentation is a no-op), and instrumenting is
    UNCONDITIONAL: the counters exist from import time; what is gated
    default-off is the /metrics ROUTE that exposes them.
    """
    if getattr(mcp_server, "_mcp_metrics_instrumented", False):
        return
    original = mcp_server.call_tool

    async def counted_call_tool(name, arguments=None, context=None):
        try:
            result = await original(name, arguments if arguments is not None else {}, context)
        except BaseException:
            METRICS.inc(name, "error")
            raise
        outcome = "error" if _result_is_error(result, error_result) else "ok"
        METRICS.inc(name, outcome)
        return result

    mcp_server.call_tool = counted_call_tool
    mcp_server._mcp_metrics_instrumented = True


async def endpoint(request):
    """Starlette handler for GET /metrics (mounted only when enabled)."""
    from starlette.responses import Response

    body, content_type = METRICS.render()
    return Response(content=body, media_type=content_type)
