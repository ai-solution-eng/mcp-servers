"""Read-only web UI + JSON API for the LogSearch MCP server.

Serves the self-contained HPE-branded log-search console (``ui/index.html``)
and a small JSON API that calls the EXACT same code paths the MCP tools
call — the tool coroutines in ``server.py`` themselves. The UI therefore has
**no powers the MCP tools don't have**: the namespace policy, the pod/line
caps, the regex handling, the chronological merge, and the per-pod error
isolation are all the server's own; this module only adapts those answers
to HTTP (status codes + ``{"error": ...}`` bodies). Nothing here can write
to the cluster — every endpoint wraps a readOnlyHint tool.

Endpoints (all JSON unless noted):

  GET  /            -> the HTML UI (also at /ui)
  GET  /api/status  -> {"status", "policy", "caps"} — what this server can
                       reach and how hard it is capped (self-describing,
                       same env vars the MCP error strings name)
  GET  /api/sources -> list_log_sources: pods + containers + restarts + age
                       (?namespace=&label_selector=)
  POST /api/search  -> search_logs: fan-out regex search, matches with
                       "pod/container: " provenance, chronological, capped
                       {"namespace", "pattern", ...}
  POST /api/count   -> count_matches: per-pod match counts, descending
                       {"namespace", "pattern", ...}

Error mapping (errors arrive as the tools' clean self-describing strings,
never tracebacks):

  403  namespace denied by the policy (the string names the env vars)
  400  bad caller input (bad regex, non-positive caps, missing namespace)
  502  the cluster side failed (unreachable API, namespace gone, pod 404)
  500  anything unexpected (same shape: {"error": "Type: message"})
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import server

_HTML_CANDIDATES = (
    Path(__file__).parent / "ui" / "index.html",  # source tree / editable install
    Path(__file__).parent.parent / "ui" / "index.html",
    Path("/app/ui/index.html"),  # Docker image (WORKDIR /app)
)

# Marker the tools use for every self-describing failure ("Error: ...").
_ERR_PREFIX = "Error: "
_DENIED_MARKER = "denied by this server's namespace policy"


def _load_html() -> str:
    override = os.environ.get("LOGSEARCH_UI_HTML", "").strip()
    candidates = ([Path(override)] if override else []) + list(_HTML_CANDIDATES)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        "<!doctype html><meta charset='utf-8'><title>LogSearch MCP</title>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>LogSearch MCP — UI asset not found</h2>"
        "<p>The <code>ui/index.html</code> file was not located next to the "
        "server module. Set <code>LOGSEARCH_UI_HTML</code> to its absolute "
        "path, or rebuild the image (the Dockerfile copies <code>ui/</code> "
        "into <code>/app</code>). The MCP endpoint and JSON API are "
        "unaffected.</p>"
    )


def _error_status(message: str) -> int:
    """HTTP status for one of the tools' 'Error: ...' strings.

    The tools are MCP-first: they return clean strings instead of raising.
    Denied namespaces and bad input are the CALLER's fault (403/400);
    everything else the tools report (unreachable API, namespace gone,
    per-pod 404s are non-fatal) means the cluster side failed (502).
    """
    if _DENIED_MARKER in message:
        return 403
    if "invalid regex" in message or "must be > 0" in message:
        return 400
    return 502


def _err_payload(exc: Exception) -> JSONResponse:
    if isinstance(exc, server.LogSearchError):
        return JSONResponse({"error": f"Error: {exc}"}, status_code=502)
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def _tool_response(raw: str) -> JSONResponse:
    """A tool's return string -> HTTP: JSON payload -> 200, an
    'Error: ...' string -> the mapped status with the same clean text."""
    if raw.startswith(_ERR_PREFIX):
        return JSONResponse({"error": raw}, status_code=_error_status(raw))
    return JSONResponse(json.loads(raw))


async def _run_tool(coro):
    """Await a tool coroutine, mapping unexpected exceptions to the same
    {'error': ...} shape (the tools already convert their own failures)."""
    try:
        return _tool_response(await coro)
    except (ValueError, TypeError) as exc:
        # Bad query/body values (e.g. since_minutes='abc') — caller input.
        return JSONResponse({"error": f"Error: {exc}"}, status_code=400)
    except Exception as exc:
        return _err_payload(exc)


def build_ui_routes() -> list[Route]:
    """Routes for the web UI + JSON API.

    No arguments on purpose: the endpoints call the tool coroutines on the
    ``server`` module directly, so the UI shares — rather than re-implements
    — the policy, the caps, and the seams (and tests monkeypatch the seams
    exactly as they do for the MCP tools).
    """

    async def ui(_request):
        return HTMLResponse(_load_html())

    async def status(_request):
        allowed = server._env_csv(server.ENV_ALLOWED)
        blocked = server._env_csv(server.ENV_BLOCKED)
        return JSONResponse(
            {
                "status": "ok",
                "server": "logsearch-mcp",
                "mcp_endpoint": "/mcp",
                "policy": {
                    # [] means ALL namespaces allowed (the MCP error strings
                    # and the values.yaml comments explain the same thing).
                    "allowed": allowed,
                    "blocked": blocked,
                    "env_allowed": server.ENV_ALLOWED,
                    "env_blocked": server.ENV_BLOCKED,
                },
                "caps": {
                    "max_pods": server._max_pods(),
                    "max_lines_per_pod": server._max_lines_per_pod(),
                    "max_total_lines": server._default_max_total_lines(),
                    "max_output_chars": server._MAX_OUTPUT_CHARS,
                },
            }
        )

    async def sources(request):
        namespace = (request.query_params.get("namespace") or "").strip()
        if not namespace:
            return JSONResponse({"error": "Error: namespace is required."}, status_code=400)
        label_selector = (request.query_params.get("label_selector") or "").strip()
        return await _run_tool(server.list_log_sources(namespace, label_selector))

    async def search(request):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": f"Error: {exc}"}, status_code=400)
        namespace = str(body.get("namespace") or "").strip()
        pattern = str(body.get("pattern") or "")
        if not namespace:
            return JSONResponse({"error": "Error: namespace is required."}, status_code=400)
        if not pattern:
            return JSONResponse({"error": "Error: pattern is required (e.g. 'ERROR|Traceback')."}, status_code=400)
        kwargs: dict = {}
        if body.get("label_selector"):
            kwargs["label_selector"] = str(body["label_selector"]).strip()
        if body.get("pod_regex"):
            kwargs["pod_regex"] = str(body["pod_regex"]).strip()
        if body.get("since_minutes") is not None:
            kwargs["since_minutes"] = body["since_minutes"]
        if body.get("tail_lines") is not None:
            kwargs["tail_lines"] = body["tail_lines"]
        if body.get("case_insensitive") is not None:
            kwargs["case_insensitive"] = bool(body["case_insensitive"])
        if body.get("container"):
            kwargs["container"] = str(body["container"]).strip()
        if body.get("max_total_lines") is not None:
            kwargs["max_total_lines"] = body["max_total_lines"]
        return await _run_tool(server.search_logs(namespace, pattern, **kwargs))

    async def count(request):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": f"Error: {exc}"}, status_code=400)
        namespace = str(body.get("namespace") or "").strip()
        pattern = str(body.get("pattern") or "")
        if not namespace:
            return JSONResponse({"error": "Error: namespace is required."}, status_code=400)
        if not pattern:
            return JSONResponse({"error": "Error: pattern is required (e.g. 'OutOfMemory')."}, status_code=400)
        kwargs: dict = {}
        if body.get("label_selector"):
            kwargs["label_selector"] = str(body["label_selector"]).strip()
        if body.get("since_minutes") is not None:
            kwargs["since_minutes"] = body["since_minutes"]
        if body.get("case_insensitive") is not None:
            kwargs["case_insensitive"] = bool(body["case_insensitive"])
        return await _run_tool(server.count_matches(namespace, pattern, **kwargs))

    return [
        Route("/", ui),
        Route("/ui", ui),
        Route("/api/status", status),
        Route("/api/sources", sources),
        Route("/api/search", search, methods=["POST"]),
        Route("/api/count", count, methods=["POST"]),
    ]
