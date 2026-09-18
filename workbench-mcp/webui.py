"""Web UI + JSON API for the Workbench MCP server.

Serves the self-contained HPE-branded scratch-pad console (``ui/index.html``)
and a small read/write JSON API that calls the SAME core functions the MCP
tools use (``_ws_list``, ``_ws_create``, ``_ws_delete``, ``_file_list``,
``_file_read``, ``_file_write``, ``_file_delete``, ``_env_load``,
``_env_set``, ``_run_command``). The UI gets NO new powers: path
confinement, the argv allowlist/denylist, timeouts, output caps, and the
JSONL audit log all live in those core functions and apply identically
whether the caller is an MCP tool or this API. The one addition is
``/api/audit``, a READ-ONLY tail over the same ``.audit.jsonl`` file the
core ``_audit`` writer produces.

Trust model (rendered as a banner in the UI): this is a scratch pad by
design — it executes allow-listed commands. The endpoint must sit behind
gateway authn (the PCAI Istio gateway); nothing here adds an auth layer.

Endpoints (all JSON unless noted):

  GET  /                        -> the HTML UI (also at /ui)
  GET  /api/status              -> {"status", "workbench", "caps": {...}}
  GET  /api/workspaces          -> {"workspaces": [...]}
  POST /api/workspaces          -> create        {"name"}
  POST /api/workspaces/delete   -> delete        {"name", "confirm"}
  GET  /api/ws/{ws}/files       -> file tree     {"path"?}
  GET  /api/ws/{ws}/file        -> read          {"path", "max_bytes"?}
  POST /api/ws/{ws}/file        -> write         {"path", "content"}
  POST /api/ws/{ws}/file/delete -> delete        {"path", "confirm"}
  GET  /api/ws/{ws}/env         -> {"env": {...}}
  POST /api/ws/{ws}/env         -> set           {"key", "value"}
  POST /api/ws/{ws}/run         -> run_command   {"command": [argv], "timeout_s"?}
  GET  /api/audit               -> tail          {"n"?}

The core operations are synchronous (file I/O, subprocess) — handlers
offload to a thread exactly the way the MCP tools do
(``asyncio.to_thread``), so one slow ``run_command`` never blocks the
event loop that serves /mcp.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from server import (
    WorkbenchError,
    _allowlist,
    _denylist,
    _env_load,
    _env_set,
    _file_delete,
    _file_list,
    _file_read,
    _file_write,
    _max_file_bytes,
    _max_list_entries,
    _max_output_bytes,
    _root,
    _run_command,
    _shared_roots,
    _timeout_default,
    _timeout_max,
    _ws_create,
    _ws_delete,
    _ws_dir,
    _ws_list,
)

_HTML_CANDIDATES = (
    Path(__file__).parent / "ui" / "index.html",  # source tree / editable install
    Path(__file__).parent.parent / "ui" / "index.html",
    Path("/app/ui/index.html"),  # Docker image (WORKDIR /app)
)

# /api/audit tail cap — the audit log is append-only JSONL and can grow
# without bound; the UI only ever needs the most recent entries.
_MAX_AUDIT_TAIL = 500


def _load_html() -> str:
    override = os.environ.get("WORKBENCH_UI_HTML", "").strip()
    candidates = ([Path(override)] if override else []) + list(_HTML_CANDIDATES)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        "<!doctype html><meta charset='utf-8'><title>Workbench MCP</title>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>Workbench MCP — UI asset not found</h2>"
        "<p>The <code>ui/index.html</code> file was not located next to the "
        "server module. Set <code>WORKBENCH_UI_HTML</code> to its absolute "
        "path, or rebuild the image (the Dockerfile copies <code>ui/</code> "
        "into <code>/app</code>). The MCP endpoint and JSON API are "
        "unaffected.</p>"
    )


def _err_payload(exc: Exception) -> JSONResponse:
    """WorkbenchError -> 400 (caller error: bad name/path/argv/policy);
    anything else -> 500."""
    if isinstance(exc, WorkbenchError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        # ValueError, not TypeError (TRY004): every /api handler catches exactly
        # ValueError to map a bad body to HTTP 400 — see the `except ValueError`
        # blocks in build_ui_routes(); raising TypeError would surface as 500.
        raise ValueError("JSON body must be an object")  # noqa: TRY004
    return body


def _flag_enabled(name: str, default: bool = True) -> bool:
    """Parse a boolean-ish env flag ('0', 'false', 'no', 'off' -> False)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def build_ui_routes() -> list[Route]:
    """Routes for the web UI + JSON API.

    No dependency injection is needed: the core functions read their
    configuration (WORKBENCH_ROOT, allow/denylists, caps) from the process
    environment at call time, exactly as they do for the MCP tools, so
    tests retarget them with a plain ``WORKBENCH_ROOT`` env override.
    """

    async def ui(_request):
        return HTMLResponse(_load_html())

    async def status(_request):
        return JSONResponse(
            {
                "status": "ok",
                "workbench": str(_root()),
                "caps": {
                    "max_file_bytes": _max_file_bytes(),
                    "max_output_bytes": _max_output_bytes(),
                    "max_list_entries": _max_list_entries(),
                    "timeout_default_s": _timeout_default(),
                    "timeout_max_s": _timeout_max(),
                    "max_audit_tail": _MAX_AUDIT_TAIL,
                },
                "policy": {
                    "allowlist": sorted(_allowlist()),
                    "denylist": sorted(_denylist()),
                    # Workspace isolation (D7): directories shared across ALL
                    # workspaces (WORKBENCH_SHARED_PATHS).
                    "shared_paths": [str(p) for p in _shared_roots()],
                },
            }
        )

    async def workspaces(request):
        if request.method == "POST":
            try:
                body = await _json_body(request)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            try:
                out = await asyncio.to_thread(_ws_create, str(body.get("name") or ""))
            except WorkbenchError as exc:
                return _err_payload(exc)
            return JSONResponse(out)
        try:
            listing = await asyncio.to_thread(_ws_list)
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse({"workspaces": listing})

    async def workspace_delete(request):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # confirm=true must come from the caller; _ws_delete still enforces
        # it (the UI cannot bypass the core policy).
        try:
            out = await asyncio.to_thread(_ws_delete, str(body.get("name") or ""), bool(body.get("confirm")))
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def files(request):
        ws = request.path_params["ws"]
        rel = (request.query_params.get("path") or "").strip()
        try:
            out = await asyncio.to_thread(_file_list, ws, rel)
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def file_read(request):
        ws = request.path_params["ws"]
        rel = (request.query_params.get("path") or "").strip()
        if not rel:
            return JSONResponse({"error": "path is required"}, status_code=400)
        try:
            max_bytes = int(request.query_params.get("max_bytes") or 65536)
        except ValueError:
            return JSONResponse({"error": "max_bytes must be an integer"}, status_code=400)
        try:
            out = await asyncio.to_thread(_file_read, ws, rel, max_bytes)
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def file_write(request):
        ws = request.path_params["ws"]
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        path = str(body.get("path") or "")
        content = body.get("content")
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        if not isinstance(content, str):
            return JSONResponse({"error": "content must be a string"}, status_code=400)
        try:
            out = await asyncio.to_thread(_file_write, ws, path, content)
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def file_delete(request):
        ws = request.path_params["ws"]
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        path = str(body.get("path") or "")
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        try:
            out = await asyncio.to_thread(_file_delete, ws, path, bool(body.get("confirm")))
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def env_get(request):
        ws = request.path_params["ws"]
        try:
            ws_dir = await asyncio.to_thread(_ws_dir, ws)
        except WorkbenchError as exc:
            return _err_payload(exc)
        try:
            env = await asyncio.to_thread(_env_load, ws_dir)
        except WorkbenchError as exc:
            return _err_payload(exc)
        # Sorted for a stable table; values are shown — get_env exposes the
        # same, so the UI has no new read power.
        return JSONResponse({"workspace": ws, "env": dict(sorted(env.items()))})

    async def env_set(request):
        ws = request.path_params["ws"]
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        key, value = str(body.get("key") or ""), body.get("value")
        if not isinstance(value, str):
            return JSONResponse({"error": "value must be a string"}, status_code=400)
        try:
            out = await asyncio.to_thread(_env_set, ws, key, value)
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def run(request):
        ws = request.path_params["ws"]
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        command = body.get("command")
        if not isinstance(command, list):
            return JSONResponse({"error": "command must be a list of argv string tokens"}, status_code=400)
        timeout_raw = body.get("timeout_s")
        try:
            timeout_s = _timeout_default() if timeout_raw is None else int(timeout_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "timeout_s must be an integer"}, status_code=400)
        try:
            out = await asyncio.to_thread(_run_command, ws, command, timeout_s)
        except WorkbenchError as exc:
            return _err_payload(exc)
        return JSONResponse(out)

    async def audit(request):
        try:
            n = int(request.query_params.get("n") or 200)
        except ValueError:
            return JSONResponse({"error": "n must be an integer"}, status_code=400)
        n = max(1, min(n, _MAX_AUDIT_TAIL))
        path = _root() / ".audit.jsonl"
        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except OSError:
            raw = b""
        lines = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        events = []
        for line in lines[-n:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn/partial line at the tail: skip, don't 500
        return JSONResponse({"n_total": len(lines), "n_shown": len(events), "events": events})

    return [
        Route("/", ui),
        Route("/ui", ui),
        Route("/api/status", status),
        Route("/api/workspaces", workspaces, methods=["GET", "POST"]),
        Route("/api/workspaces/delete", workspace_delete, methods=["POST"]),
        Route("/api/ws/{ws}/files", files),
        Route("/api/ws/{ws}/file", file_read),
        Route("/api/ws/{ws}/file", file_write, methods=["POST"]),
        Route("/api/ws/{ws}/file/delete", file_delete, methods=["POST"]),
        Route("/api/ws/{ws}/env", env_get),
        Route("/api/ws/{ws}/env", env_set, methods=["POST"]),
        Route("/api/ws/{ws}/run", run, methods=["POST"]),
        Route("/api/audit", audit),
    ]
