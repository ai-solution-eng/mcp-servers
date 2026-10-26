"""Tiny, honest self-metrics for the MCP fleet — the shared implementation.

Wave-6 G1 (decision D16) consolidation of the FOUR byte-identical per-app
``mcp_metrics.py`` copies (workbench / logsearch / prometheus / searxng,
verified byte-identical except the app-name prefix on 2026-09-13). The only
intended behavior DELTA vs the copies is the B3-queued fix, applied here and
documented below: **unknown tool names normalize to the label value
``"unknown"``** (bounded cardinality — before, an unauthenticated caller
could mint unbounded label series, one per probed name).

Design rules (inherited unchanged from the Wave-3 C3 copies,
FLEET-EXECUTION-PLAN-2026-09 §7 C3):

* ADDITIVE and chart-gated DEFAULT-OFF. Nothing here changes any request's
  behavior; the /metrics ROUTE exists only when the app's chart sets
  ``<APP>_METRICS_ENABLED=true`` (values: metrics.enabled), so the default
  deployment has no metrics surface at all.
* prometheus-client when importable, a dependency-free fallback otherwise
  (the SQLhandler import-guard fleet pattern): a metrics failure must never
  break a tool call, and the server must not grow a hard dependency for
  metrics. Which backend is active changes nothing else about the server.
* Small and honest: ONE counter family — per-tool request counts with an
  outcome label (ok | error). No arguments, file names, workspace names,
  env values, or error text are ever exported. Labels are bounded by
  construction: real tool names plus the single ``"unknown"`` value.

Counting scope (unchanged): MCP-protocol tool calls (wrapped
``MCPServer.call_tool`` — one insertion point covering every registered
tool, present and future). The web consoles reuse the same core functions
directly (not via the MCP layer), so UI traffic is intentionally not
counted; these numbers measure agent/MCP traffic.

Migration seam: a consumer binds its app once —

    from mcp_fleet_common import metrics as fleet_metrics
    mcp_metrics = fleet_metrics.bind_app_metrics(
        metric_prefix="workbench_mcp", display_name="workbench-mcp")

``mcp_metrics`` is an :class:`AppMetrics` facade with the same surface the
old per-app module had (``instrument``, ``endpoint``, ``TOOL_REQUESTS``,
``_HAVE_PROMETHEUS_CLIENT``, ``_MiniCounter``), so import swaps are one line
and existing call sites/assertions stay untouched.
"""

from __future__ import annotations

import logging
import threading

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
    """One counter family, two interchangeable backends (see module doc).

    ``metric_prefix`` is the per-app Prometheus name prefix, e.g.
    ``"workbench_mcp"`` → counter ``workbench_mcp_tool_requests_total`` with
    HELP ``"<display_name> MCP tool calls, by tool and outcome."`` — exactly
    the names the four consolidated copies render today. ``backend`` pins the
    backend for tests (``"auto"`` = the module import guard, ``"prometheus"``,
    ``"fallback"``); consumers use the default.
    """

    def __init__(
        self,
        *,
        metric_prefix: str,
        display_name: str,
        backend: str = "auto",
    ) -> None:
        self.metric_prefix = metric_prefix
        self.display_name = display_name
        self.tool_requests_name = metric_prefix + "_tool_requests_total"
        self.tool_requests_help = f"{display_name} MCP tool calls, by tool and outcome."
        self._logger = logging.getLogger(f"{display_name}.metrics")
        self._registry: CollectorRegistry | None
        self._prom: _PromCounter | None
        self._mini: _MiniCounter | None
        use_prom = _HAVE_PROMETHEUS_CLIENT if backend == "auto" else backend == "prometheus"
        if use_prom:
            # Private registry: no process/GC collectors, and no
            # double-registration if the module is imported twice (tests).
            self._registry = CollectorRegistry()
            self._prom = _PromCounter(
                self.tool_requests_name,
                self.tool_requests_help,
                ("tool", "outcome"),
                registry=self._registry,
            )
            self._mini = None
        else:
            self._registry = None
            self._prom = None
            self._mini = _MiniCounter(
                self.tool_requests_name,
                self.tool_requests_help,
                ("tool", "outcome"),
            )

    # -- the one counter family ------------------------------------------------

    def inc(self, tool: str, outcome: str) -> None:
        """Count one tool call (best-effort: never raises)."""
        try:
            if self._prom is not None:
                self._prom.labels(tool=tool, outcome=outcome).inc()
            else:
                assert self._mini is not None
                self._mini.inc((tool, outcome))
        except Exception:  # metrics must never break a tool call
            self._logger.debug("metrics increment failed", exc_info=True)

    def render(self) -> tuple[str, str]:
        """Exposition body + content type (best-effort: never raises)."""
        try:
            if self._prom is not None:
                assert self._registry is not None
                from prometheus_client import generate_latest

                return generate_latest(self._registry).decode("utf-8"), CONTENT_TYPE
            assert self._mini is not None
            return self._mini.render(), CONTENT_TYPE
        except Exception:  # metrics must never break a request
            self._logger.debug("metrics render failed", exc_info=True)
            return "", CONTENT_TYPE

    # -- HTTP surface -----------------------------------------------------------

    async def endpoint(self, request):
        """Starlette handler for GET /metrics (mounted only when enabled)."""
        from starlette.responses import Response

        body, content_type = self.render()
        return Response(content=body, media_type=content_type)


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
        logging.getLogger("mcp-fleet-common.metrics").debug("metrics error_result predicate failed", exc_info=True)
        return False


def _label_resolver(mcp_server, unknown_label: str):
    """Build the tool-name → label-value resolver used by :func:`instrument`.

    The B3-queued fix (Wave-3 verification note; applied in the W6 shared
    module): names that are NOT registered tools must not become label
    values — an anonymous caller probing ``no_such_tool_1``, ``..._2``, …
    would otherwise mint unbounded label series (cardinality blow-up and a
    small information leak about which names exist). Unregistered names
    collapse onto *unknown_label* (``"unknown"``); registered tools keep
    their real name. Resolution uses the MCP ``ToolManager.get_tool``
    membership check (public accessor, sync, dict-backed, cheap); when no
    tool manager is discoverable the resolver is the identity — the metric
    degrades to the pre-fix behavior rather than mislabeling anything.
    """
    tool_manager = getattr(mcp_server, "_tool_manager", None)
    get_tool = getattr(tool_manager, "get_tool", None)
    if not callable(get_tool):
        return lambda name: name

    def resolve(name: str) -> str:
        try:
            return name if get_tool(name) is not None else unknown_label
        except Exception:  # membership check must never break a tool call
            return name

    return resolve


def instrument(
    mcp_server,
    metrics: Metrics,
    *,
    error_result=None,
    unknown_label: str = "unknown",
) -> None:
    """Count every MCP-protocol tool call made through *mcp_server*.

    One insertion point instead of a decorator on every tool: wraps the
    instance's ``call_tool`` — ``MCPServer._handle_call_tool`` resolves
    ``self.call_tool`` at call time, so the wrapper covers every registered
    tool on both stdio and streamable-http transports.

    outcome="error" counts calls that RAISE at the MCP layer (argument
    validation, unknown tools, and tool errors); when *error_result* is
    given, a returned result matching it also counts as "error" (servers
    that report tool failures as strings instead of raising).

    THE ONE DELTA vs the four consolidated copies: an unknown tool name is
    counted under *unknown_label* (default ``"unknown"``) instead of the raw
    probed name — bounded cardinality (see :func:`_label_resolver`).

    Idempotent (double instrumentation is a no-op), and instrumenting is
    UNCONDITIONAL: the counters exist from import time; what is gated
    default-off is the /metrics ROUTE that exposes them.
    """
    if getattr(mcp_server, "_mcp_metrics_instrumented", False):
        return
    original = mcp_server.call_tool
    resolve_label = _label_resolver(mcp_server, unknown_label)

    async def counted_call_tool(name, arguments=None, context=None):
        label = resolve_label(name)
        try:
            result = await original(name, arguments if arguments is not None else {}, context)
        except BaseException:
            metrics.inc(label, "error")
            raise
        outcome = "error" if _result_is_error(result, error_result) else "ok"
        metrics.inc(label, outcome)
        return result

    mcp_server.call_tool = counted_call_tool
    mcp_server._mcp_metrics_instrumented = True


class AppMetrics:
    """Per-app binding — the drop-in replacement for a server's metrics module.

    Exposes the same names the consolidated ``mcp_metrics.py`` copies exposed
    (module-level constants + the module functions bound to this app's
    instance), so a consumer's ``import mcp_metrics`` becomes a one-line
    binding and every call site / test assertion stays untouched:

    * ``METRICS``            — this app's process-wide :class:`Metrics`
    * ``METRIC_PREFIX`` / ``TOOL_REQUESTS`` / ``TOOL_REQUESTS_HELP`` /
      ``CONTENT_TYPE``       — the exposition constants
    * ``instrument(mcp, …)`` — wraps the app's ``call_tool``
    * ``endpoint(request)``  — the GET /metrics handler
    * ``_HAVE_PROMETHEUS_CLIENT`` / ``_MiniCounter`` — backend seams (tests)
    """

    def __init__(self, *, metric_prefix: str, display_name: str) -> None:
        self.METRIC_PREFIX = metric_prefix
        self.DISPLAY_NAME = display_name
        self.TOOL_REQUESTS = metric_prefix + "_tool_requests_total"
        self.TOOL_REQUESTS_HELP = f"{display_name} MCP tool calls, by tool and outcome."
        self.CONTENT_TYPE = CONTENT_TYPE
        self._HAVE_PROMETHEUS_CLIENT = _HAVE_PROMETHEUS_CLIENT
        self._MiniCounter = _MiniCounter
        self.METRICS = Metrics(metric_prefix=metric_prefix, display_name=display_name)

    def instrument(self, mcp_server, *, error_result=None) -> None:
        instrument(mcp_server, self.METRICS, error_result=error_result)

    async def endpoint(self, request):
        return await self.METRICS.endpoint(request)

    @property
    def metrics(self) -> Metrics:
        """The underlying :class:`Metrics` instance (direct increments/tests)."""
        return self.METRICS


def bind_app_metrics(*, metric_prefix: str, display_name: str) -> AppMetrics:
    """Bind the shared implementation to one app — see :class:`AppMetrics`.

    ``metric_prefix`` is the Prometheus name prefix as spelled in the
    consolidated copies (``workbench_mcp``, ``logsearch_mcp``,
    ``prometheus_mcp``, ``searxng_mcp``); ``display_name`` is the
    human-readable server name used in the HELP line and the logger name.
    """
    return AppMetrics(metric_prefix=metric_prefix, display_name=display_name)
