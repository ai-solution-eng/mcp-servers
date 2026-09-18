"""Read-only web UI + JSON API for applygate-mcp — the human front-end.

Serves the self-contained HPE-branded console (``ui/index.html``) and a tiny
JSON API that drives the SAME guardrail code paths as the MCP tools. The
module is deliberately tiny in what it CAN do, because applygate is the
fleet's governed WRITE half:

  HARD RULE — THE UI CAN NEVER MUTATE THE CLUSTER. There is no /api endpoint
  for apply or delete here — not even a gated one. The only write-shaped
  surface is the PLAN console, which calls ``server._plan_apply_sync`` — the
  exact code path behind the ``plan_apply`` tool, which is ALWAYS a
  server-side-apply dry-run (``dry_run="All"``) and can never persist
  anything. Real mutations happen only through the MCP tools
  (``apply_manifest`` / ``delete_resource``) with their explicit
  ``confirm_apply`` / ``confirm_delete`` flags, behind gateway authn.

Endpoints (the complete API surface — all read-only):

  GET  /                -> the HTML UI (also at /ui)
  GET  /api/status      -> {"status", "namespaces_enabled", "field_manager",
                            "audit_file", "policy": {...}, "caps": {...}}
  GET  /api/policy      -> the effective namespace allowlist/blocklist + kind
                            allowlist (from the same config helpers the tools
                            use) plus the hard-refusal texts, so a human can
                            see why a plan was refused
  POST /api/plan        -> plan console  {"namespace", "manifest"} — runs
                            ``plan_apply``'s sync body (always dry-run) and
                            returns its per-doc {kind, name, ok, message}
                            verdicts verbatim, refusals included
  GET  /api/resource_status?namespace=&kind=&name=
                        -> ``get_resource_status``'s sync body — same
                            namespace policy + kind gates as the write tools
  GET  /api/audit?lines=N -> the last N lines of the JSONL audit trail,
                            parsed. Reads ONLY the configured
                            APPLYGATE_AUDIT_FILE path — there is no
                            client-selectable path parameter, and any request
                            that tries to smuggle one is refused with 400.

Refusals are the product: the endpoints return the tools' exact
self-describing refusal strings ({"ok": false, "refused": true, "error": ...}
and per-doc messages) with HTTP 200 so the UI can render them as
self-describing error panels instead of hiding them behind a status code.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

_HTML_CANDIDATES = (
    Path(__file__).parent / "ui" / "index.html",  # source tree / editable install
    Path(__file__).parent.parent / "ui" / "index.html",
    Path("/app/ui/index.html"),  # Docker image (WORKDIR /app)
)

# Audit viewer caps: the tail is bounded so a long-lived PVC audit file can
# never flood the browser (or the JSON payload) either.
_MAX_AUDIT_LINES = 500
_DEFAULT_AUDIT_LINES = 200
_MAX_AUDIT_READ_BYTES = 32 * 1024 * 1024  # tail window (audit lives on a 1Gi PVC)

# Query parameters that would try to redirect the audit read to a
# caller-chosen file. The audit path is SERVER configuration, full stop —
# the endpoint refuses any request carrying one of these keys.
_AUDIT_PATH_PARAMS = ("file", "path", "audit", "audit_file", "auditFile")

# The audit entry schema written by server._audit — a parsed line must carry
# all of these keys to count as an entry (anything else is malformed).
_AUDIT_ENTRY_KEYS = {"ts", "tool", "namespace", "kind", "name", "dry_run", "outcome"}


class _AuditReadError(Exception):
    """The configured audit file exists but could not be read."""


def webui_enabled(environ: dict | None = None) -> bool:
    """The APPLYGATE_WEBUI_ENABLED gate for the console routes (chart values
    key: ``webui.enabled``). Unset/empty -> enabled (the chart ships the
    console on); "0"/"false"/"no"/"off" (case-insensitive) disables — / and
    /api/* disappear while /mcp and the health endpoints keep working."""
    env = os.environ if environ is None else environ
    raw = (env.get("APPLYGATE_WEBUI_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "disabled")


def _server():
    """The MCP server module, imported lazily — ``server`` imports THIS module
    inside ``_build_http_app``, so a top-level import would be circular (and
    tests import webui standalone). Imported at call time so the handlers
    always see the live module attributes (monkeypatched seams included)."""
    import server

    return server


def _audit_path() -> str:
    """The ONE file the audit endpoint may ever read: the configured
    APPLYGATE_AUDIT_FILE (default /data/audit.jsonl). No client input can
    influence this — see _AUDIT_PATH_PARAMS."""
    return os.environ.get("APPLYGATE_AUDIT_FILE") or _server().DEFAULT_AUDIT_FILE


def _load_html() -> str:
    override = os.environ.get("APPLYGATE_WEBUI_HTML", "").strip()
    candidates = ([Path(override)] if override else []) + list(_HTML_CANDIDATES)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        "<!doctype html><meta charset='utf-8'><title>ApplyGate MCP</title>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>ApplyGate MCP — UI asset not found</h2>"
        "<p>The <code>ui/index.html</code> file was not located next to the "
        "server module. Set <code>APPLYGATE_WEBUI_HTML</code> to its absolute "
        "path, or rebuild the image (the Dockerfile copies <code>ui/</code> "
        "into <code>/app</code>). The MCP endpoint and JSON API are "
        "unaffected.</p>"
    )


def _policy_snapshot() -> dict:
    """The effective policy, from the SAME config helpers the tools read at
    call time — this is exactly what plan/apply/delete/status are judged
    against, so the panel can explain any refusal."""
    s = _server()
    allowed = s._env_list("APPLYGATE_ALLOWED_NAMESPACES")
    blocked = s._env_list("APPLYGATE_BLOCKED_NAMESPACES")
    kinds_raw = s._env_list("APPLYGATE_ALLOWED_KINDS")
    kinds = [k.strip() for k in (kinds_raw or s.DEFAULT_ALLOWED_KINDS.split(",")) if k.strip()]

    # The two unconditional refusals, produced by the REAL guard function —
    # shown verbatim on the policy panel so a human reads the exact text a
    # refused plan would have produced.
    hard_refusals = {}
    for label, kind in (("secret", "Secret"), ("cluster_scoped_example", "Namespace")):
        try:
            s._check_kind(kind)
            hard_refusals[label] = ""
        except s._Refusal as exc:
            hard_refusals[label] = str(exc)
    return {
        "allowed_namespaces": allowed,
        "blocked_namespaces": blocked,
        "allowed_kinds": kinds,
        "namespaces_enabled": bool(allowed),
        "default_deny": not allowed,
        "kinds_from_env": bool(kinds_raw),
        "hard_refusals": hard_refusals,
    }


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise TypeError("JSON body must be an object")
    return body


def _read_audit_tail(path: str, lines: int) -> dict:
    """Tail the configured audit JSONL: last `lines` non-empty lines, parsed.

    Reads ONLY `path` (which _audit_path derived from server configuration —
    never from the request). A missing file is a normal fresh-deployment
    state, not an error. A window cap bounds the read for very long trails.
    """
    out: dict[str, Any] = {
        "file": path,
        "exists": True,
        "n_total": 0,
        "n_shown": 0,
        "malformed": 0,
        "window_truncated": False,
        "entries": [],
    }
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size > _MAX_AUDIT_READ_BYTES:
                fh.seek(-_MAX_AUDIT_READ_BYTES, os.SEEK_END)
                out["window_truncated"] = True
            else:
                fh.seek(0)
            data = fh.read()
    except FileNotFoundError:
        out["exists"] = False
        return out
    except OSError as exc:
        raise _AuditReadError(f"could not read audit file {path!r}: {exc}") from exc

    all_lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if out["window_truncated"] and all_lines:
        all_lines = all_lines[1:]  # first line of the window is likely partial
    out["n_total"] = len(all_lines)
    # The audit writer emits a fixed 7-key schema ({ts, tool, namespace, kind,
    # name, dry_run, outcome}) — a parsed line without it is malformed, not an
    # entry, so the UI table never renders ghost rows.
    for ln in all_lines[-lines:]:
        try:
            entry = json.loads(ln)
        except ValueError:
            out["malformed"] += 1
            continue
        if isinstance(entry, dict) and _AUDIT_ENTRY_KEYS <= set(entry):
            out["entries"].append(entry)
        else:
            out["malformed"] += 1
    out["n_shown"] = len(out["entries"])
    return out


def build_ui_routes(plan_fn=None, status_fn=None) -> list:
    """Routes for the web UI + JSON API.

    ``plan_fn`` / ``status_fn`` default to the REAL MCP tool bodies
    (``server._plan_apply_sync`` / ``server._status_sync``), resolved lazily
    at call time — dependency-injected so tests can stub them, and so the UI
    provably rides the exact plan/status code paths the MCP tools use
    (monkeypatched seams included). There is deliberately NO apply/delete
    function parameter: that endpoint does not exist.
    """

    def _plan_default(namespace: str, manifest: str) -> str:
        # force=False: the flag is accepted for call-site symmetry by the tool
        # body but NEVER affects the plan — no UI input can reach it anyway.
        return _server()._plan_apply_sync(namespace, manifest, False)

    def _status_default(namespace: str, kind: str, name: str) -> str:
        return _server()._status_sync(namespace, kind, name)

    plan_body = plan_fn or _plan_default
    status_body = status_fn or _status_default

    async def ui(_request):
        return HTMLResponse(_load_html())

    async def api_status(_request):
        try:
            s = _server()
            return JSONResponse(
                {
                    "status": "ok",
                    "server": "applygate-mcp",
                    "namespaces_enabled": bool(s._env_list("APPLYGATE_ALLOWED_NAMESPACES")),
                    "field_manager": s.FIELD_MANAGER,
                    "audit_file": _audit_path(),
                    "policy": _policy_snapshot(),
                    "caps": {
                        "max_docs": s.MAX_DOCS,
                        "max_manifest_kib": s.MAX_MANIFEST_BYTES // 1024,
                        "max_audit_lines": _MAX_AUDIT_LINES,
                        "default_audit_lines": _DEFAULT_AUDIT_LINES,
                    },
                }
            )
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    async def api_policy(_request):
        try:
            return JSONResponse(_policy_snapshot())
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    async def api_plan(request):
        """The PLAN console — the only write-shaped endpoint, and it can never
        write: it runs plan_apply's sync body, which is ALWAYS a dry-run.

        Deliberately reads ONLY {namespace, manifest} from the body: any
        "dry_run"/"confirm_apply"/"force" fields a caller posts are IGNORED,
        so no client input can flip the seam's dry_run=True."""
        try:
            body = await _json_body(request)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        namespace = str(body.get("namespace") or "").strip()
        manifest = body.get("manifest")
        if not namespace:
            return JSONResponse(
                {"error": "namespace is required — the target namespace (allowlist rules apply; see /api/policy)"},
                status_code=400,
            )
        if not isinstance(manifest, str) or not manifest.strip():
            return JSONResponse(
                {"error": "manifest is required — a YAML manifest string (multi-doc OK)"},
                status_code=400,
            )
        try:
            result = await asyncio.to_thread(plan_body, namespace, manifest)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
        try:
            return JSONResponse(json.loads(result))
        except ValueError:
            return JSONResponse({"error": "plan returned a non-JSON result"}, status_code=500)

    async def api_resource_status(request):
        params = request.query_params
        namespace = (params.get("namespace") or "").strip()
        kind = (params.get("kind") or "").strip()
        name = (params.get("name") or "").strip()
        missing = [p for p, v in (("namespace", namespace), ("kind", kind), ("name", name)) if not v]
        if missing:
            return JSONResponse({"error": f"missing required parameter(s): {', '.join(missing)}"}, status_code=400)
        try:
            result = await asyncio.to_thread(status_body, namespace, kind, name)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
        try:
            return JSONResponse(json.loads(result))
        except ValueError:
            return JSONResponse({"error": "status returned a non-JSON result"}, status_code=500)

    async def api_audit(request):
        params = request.query_params
        # Path-traversal refusal FIRST: any client-selected path key is a 400,
        # no matter what it points at. The audit file is server configuration.
        for p in _AUDIT_PATH_PARAMS:
            if p in params:
                return JSONResponse(
                    {
                        "error": f"query parameter '{p}' is refused — the audit endpoint reads ONLY the "
                        "configured APPLYGATE_AUDIT_FILE path; client-selected paths are not supported"
                    },
                    status_code=400,
                )
        raw = (params.get("lines") or "").strip()
        if not raw:
            lines = _DEFAULT_AUDIT_LINES
        else:
            try:
                lines = int(raw)
            except ValueError:
                return JSONResponse({"error": "lines must be an integer"}, status_code=400)
            if lines < 1:
                return JSONResponse({"error": "lines must be >= 1"}, status_code=400)
            lines = min(lines, _MAX_AUDIT_LINES)
        try:
            payload = await asyncio.to_thread(_read_audit_tail, _audit_path(), lines)
        except _AuditReadError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
        return JSONResponse(payload)

    return [
        Route("/", ui),
        Route("/ui", ui),
        Route("/api/status", api_status),
        Route("/api/policy", api_policy),
        Route("/api/plan", api_plan, methods=["POST"]),
        Route("/api/resource_status", api_resource_status),
        Route("/api/audit", api_audit),
    ]
