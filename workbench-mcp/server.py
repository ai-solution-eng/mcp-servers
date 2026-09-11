#!/usr/bin/env python3
"""Workbench MCP server — persistent, per-agent scratch workspaces.

Baseline agent harnesses (opencode, DSH) give the model a stateless shell:
each call is a fresh process and platform temp areas may not survive between
calls.  This server provides the missing durable layer:

  - named workspaces (directories) on a persistent volume (WORKBENCH_ROOT),
  - file read/write/list/delete confined inside the workspace,
  - a small per-workspace env store that ``run_command`` picks up,
  - bounded command execution (argv-only, binary allowlist, timeout, output
    cap) running with the workspace as cwd.

Trust model: the workbench pod is a scratch pad by design.  It mounts no
secrets, runs non-root, and everything it can touch lives under one volume.
Command execution is intentional (it is the point of a workbench) but is
governed by an argv allowlist/denylist, timeouts, and output caps, and every
mutating call is appended to a JSONL audit log.

Run in stdio mode::

    python server.py --transport stdio

Or as a stateless streamable-http server (the fleet default)::

    python server.py --transport streamable-http --host 0.0.0.0 --port 9103
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

logging.basicConfig(level=os.environ.get("WORKBENCH_LOG_LEVEL", "INFO"))
logger = logging.getLogger("workbench-mcp")

mcp = MCPServer("workbench-mcp")
_mcp_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# ---------------------------------------------------------------------------
# configuration (read lazily so tests can retarget the root)
# ---------------------------------------------------------------------------

_WS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

_DEFAULT_ALLOW = "python3,pip,pip3,ls,cat,head,tail,grep,find,wc,du,df,mkdir,touch,cp,mv,tar,git,diff,sort,uniq"
_DEFAULT_DENY = "curl,wget,sudo,su,nc,ncat,ssh,scp,setsid"

# Operator-configured proxy / TLS-trust variables passed through to
# run_command children. run_command builds a fresh, minimal env for child
# processes (the workspace env + a few fixed names), which is the safe
# default — but on corporate-proxy clusters the chart wires
# HTTP_PROXY/HTTPS_PROXY/NO_PROXY onto the pod precisely so that `pip`
# works, and SSL_CERT_FILE/REQUESTS_CA_BUNDLE/PIP_CERT so pip trusts the
# MITM CA. These names come from the deployment, not from the caller, so
# passing them through leaks nothing and weakens nothing; the per-workspace
# env store still overrides them per workspace (its entries win).
_PROXY_PASSTHROUGH = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "NO_PROXY", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT",
)


def _ui_enabled() -> bool:
    """Web UI + /api/* mounted? (WORKBENCH_UI_ENABLED, default true).

    The chart's ``webui.enabled`` values flag is rendered into this env var
    by deployment.yaml; '0'/'false'/'no'/'off' removes the UI routes while
    /mcp, /health and /healthz stay up.
    """
    raw = os.environ.get("WORKBENCH_UI_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _root() -> Path:
    return Path(os.environ.get("WORKBENCH_ROOT", "/data")).resolve()


def _allowlist() -> set[str]:
    raw = os.environ.get("WORKBENCH_EXEC_ALLOWLIST", _DEFAULT_ALLOW)
    return {t.strip() for t in raw.split(",") if t.strip()}


def _denylist() -> set[str]:
    raw = os.environ.get("WORKBENCH_EXEC_DENYLIST", _DEFAULT_DENY)
    return {t.strip() for t in raw.split(",") if t.strip()}


def _timeout_default() -> int:
    return _int_env("WORKBENCH_EXEC_TIMEOUT_DEFAULT", 60)


def _timeout_max() -> int:
    return _int_env("WORKBENCH_EXEC_TIMEOUT_MAX", 600)


def _max_file_bytes() -> int:
    return _int_env("WORKBENCH_MAX_FILE_BYTES", 8 * 1024 * 1024)


def _max_output_bytes() -> int:
    return _int_env("WORKBENCH_MAX_OUTPUT_BYTES", 200 * 1024)


def _max_list_entries() -> int:
    return _int_env("WORKBENCH_MAX_LIST_ENTRIES", 500)


def _int_env(name: str, default: int) -> int:
    """Read a numeric env knob, tolerating float-ish renderings.

    helm 4 (sigs.k8s.io/yaml) decodes chart integers as float64, so
    ``maxFileBytes: 8388608`` can arrive as ``"8.388608e+06"`` — plain
    ``int()`` would raise and take the file/run tools down with it.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("invalid %s=%r — using default %s", name, raw, default)
        return default


class WorkbenchError(Exception):
    """Self-describing error surfaced verbatim to the model."""


# ---------------------------------------------------------------------------
# audit log (best-effort JSONL under the root)
# ---------------------------------------------------------------------------


def _audit(event: dict) -> None:
    try:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
        with open(_root() / ".audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("audit write failed", exc_info=True)


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------


def _ws_dir(name: str) -> Path:
    if not _WS_NAME.match(name):
        raise WorkbenchError(
            f"invalid workspace name {name!r}: must match {_WS_NAME.pattern}"
        )
    ws = _root() / name
    if not ws.is_dir():
        raise WorkbenchError(
            f"workspace {name!r} does not exist (create it with workspace_create)"
        )
    return ws


def _safe_join(ws: Path, rel: str) -> Path:
    """Join *rel* under workspace *ws*, refusing escapes and symlink tricks."""
    if rel.startswith("/") or rel == "":
        # An absolute path is an escape attempt like any other — surface the
        # same "escapes the workspace" error the traversal cases produce.
        raise WorkbenchError(
            f"path escapes the workspace (refusing {rel!r} — paths must be "
            "workspace-relative; traversal and symlink escapes are blocked "
            "by design)"
        )
    target = (ws / rel).resolve()
    ws_real = ws.resolve()
    if target != ws_real and ws_real not in target.parents:
        raise WorkbenchError(
            f"path escapes the workspace (refusing {rel!r} — traversal and "
            "symlink escapes are blocked by design)"
        )
    return target


def _env_file(ws: Path) -> Path:
    return ws / ".workbench-env.json"


def _env_load(ws: Path) -> dict[str, str]:
    f = _env_file(ws)
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# core operations (sync; tools offload to a thread)
# ---------------------------------------------------------------------------


def _ws_create(name: str) -> dict:
    if not _WS_NAME.match(name):
        raise WorkbenchError(
            f"invalid workspace name {name!r}: must match {_WS_NAME.pattern}"
        )
    ws = _root() / name
    if ws.exists():
        raise WorkbenchError(f"workspace {name!r} already exists")
    ws.mkdir(parents=True)
    _audit({"event": "workspace_create", "workspace": name})
    return {"workspace": name, "path": str(ws), "created": True}


def _ws_list() -> list[dict]:
    root = _root()
    out = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        files = 0
        total = 0
        for p in entry.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        out.append({"workspace": entry.name, "files": files, "bytes": total})
    return out


def _ws_delete(name: str, confirm: bool) -> dict:
    if not confirm:
        raise WorkbenchError(
            "refusing to delete: pass confirm=true (destructive, irreversible)"
        )
    ws = _ws_dir(name)
    shutil.rmtree(ws)
    _audit({"event": "workspace_delete", "workspace": name})
    return {"workspace": name, "deleted": True}


def _file_write(ws_name: str, rel: str, content: str) -> dict:
    ws = _ws_dir(ws_name)
    target = _safe_join(ws, rel)
    payload = content.encode("utf-8")
    limit = _max_file_bytes()
    if len(payload) > limit:
        raise WorkbenchError(
            f"content is {len(payload)} bytes; cap is {limit} "
            "(WORKBENCH_MAX_FILE_BYTES) — write in chunks or raise the cap"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {"workspace": ws_name, "path": rel, "bytes": len(payload), "written": True}


def _file_read(ws_name: str, rel: str, max_bytes: int) -> dict:
    ws = _ws_dir(ws_name)
    target = _safe_join(ws, rel)
    if not target.is_file():
        raise WorkbenchError(f"no such file: {rel!r} in workspace {ws_name!r}")
    data = target.read_bytes()[:max_bytes]
    truncated = target.stat().st_size > len(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(
            f"{rel!r} is not valid UTF-8 text ({exc}); this server reads text "
            "files only"
        ) from exc
    return {
        "workspace": ws_name,
        "path": rel,
        "bytes": len(data),
        "truncated": truncated,
        "content": text,
    }


def _file_list(ws_name: str, rel: str) -> dict:
    ws = _ws_dir(ws_name)
    base = _safe_join(ws, rel) if rel else ws
    if not base.is_dir():
        raise WorkbenchError(f"no such directory: {rel!r} in workspace {ws_name!r}")
    entries = []
    for p in sorted(base.rglob("*"))[: _max_list_entries()]:
        relpath = str(p.relative_to(ws))
        is_dir = p.is_dir()
        size = None
        if not is_dir:
            try:
                size = p.stat().st_size
            except OSError:
                size = None  # broken symlink / vanished mid-list: report, don't crash
        entries.append(
            {
                "path": relpath,
                "type": "dir" if is_dir else "file",
                "bytes": size,
            }
        )
    return {"workspace": ws_name, "entries": entries, "count": len(entries)}


def _file_delete(ws_name: str, rel: str, confirm: bool) -> dict:
    if not confirm:
        raise WorkbenchError(
            "refusing to delete: pass confirm=true (destructive, irreversible)"
        )
    ws = _ws_dir(ws_name)
    target = _safe_join(ws, rel)
    if target == ws.resolve():
        raise WorkbenchError("refusing to delete the workspace itself — use workspace_delete")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.is_file():
        target.unlink()
    else:
        raise WorkbenchError(f"no such file: {rel!r}")
    _audit({"event": "file_delete", "workspace": ws_name, "path": rel})
    return {"workspace": ws_name, "path": rel, "deleted": True}


def _env_set(ws_name: str, key: str, value: str) -> dict:
    if not _ENV_KEY.match(key):
        raise WorkbenchError(f"invalid env key {key!r}: must match {_ENV_KEY.pattern}")
    ws = _ws_dir(ws_name)
    env = _env_load(ws)
    env[key] = value
    f = _env_file(ws)
    f.write_text(json.dumps(env, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(f, 0o600)
    return {"workspace": ws_name, "key": key, "set": True}


def _env_get(ws_name: str, key: str) -> dict:
    ws = _ws_dir(ws_name)
    env = _env_load(ws)
    if key not in env:
        raise WorkbenchError(f"env key {key!r} not set in workspace {ws_name!r}")
    return {"workspace": ws_name, "key": key, "value": env[key]}


def _run_command(ws_name: str, command: list[str], timeout_s: int) -> dict:
    ws = _ws_dir(ws_name)
    if not command or not isinstance(command, list) or not all(
        isinstance(a, str) for a in command
    ):
        raise WorkbenchError("command must be a non-empty list of string argv tokens")
    binary = Path(command[0]).name  # allow "../python3"-style paths by basename
    allow, deny = _allowlist(), _denylist()
    if binary in deny:
        raise WorkbenchError(f"command {binary!r} is deny-listed (WORKBENCH_EXEC_DENYLIST)")
    if binary not in allow:
        raise WorkbenchError(
            f"command {binary!r} is not in the allowlist "
            f"(WORKBENCH_EXEC_ALLOWLIST={sorted(allow)}) — argv[0] only, no shells"
        )
    timeout_s = min(max(int(timeout_s), 1), _timeout_max())
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(ws),
        "LANG": "C.UTF-8",
        "WORKBENCH_WORKSPACE": ws_name,
        # Operator-configured proxy / TLS-trust settings (see
        # _PROXY_PASSTHROUGH) so `pip` and friends reach the network on
        # corporate-proxy clusters.
        **{k: v for k in _PROXY_PASSTHROUGH if (v := os.environ.get(k))},
        # Per-workspace env wins over the passthrough.
        **_env_load(ws),
    }
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(ws),
            env=env,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code, timed_out = 124, True
        stdout = exc.stdout or b""
        stderr = (exc.stderr or b"") + f"\n[workbench] timed out after {timeout_s}s".encode()
    duration_ms = int((time.monotonic() - started) * 1000)
    cap = _max_output_bytes()
    out_len, err_len = len(stdout), len(stderr)
    stdout, stderr = stdout[:cap], stderr[:cap]
    _audit(
        {
            "event": "run_command",
            "workspace": ws_name,
            "argv": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
        }
    )
    return {
        "workspace": ws_name,
        "argv": command,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "truncated": out_len > cap or err_len > cap,
    }


# ---------------------------------------------------------------------------
# MCP tools (thin async wrappers; blocking work runs in a thread)
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Create Workspace",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
async def workspace_create(name: str) -> str:
    """Create a persistent named workspace (a directory under WORKBENCH_ROOT).

    Workspaces survive pod restarts (PVC-backed) and are the container for
    all other workbench tools: files, env vars, and command runs.
    """
    return json.dumps(await asyncio.to_thread(_ws_create, name))


@mcp.tool(
    title="List Workspaces",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def workspace_list() -> str:
    """List workspaces with file counts and total bytes."""
    return json.dumps(await asyncio.to_thread(_ws_list))


@mcp.tool(
    title="Delete Workspace",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
)
async def workspace_delete(name: str, confirm: bool = False) -> str:
    """Delete a workspace and everything in it. Requires confirm=true."""
    return json.dumps(await asyncio.to_thread(_ws_delete, name, confirm))


@mcp.tool(
    title="Write File",
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False),
)
async def write_file(workspace: str, path: str, content: str) -> str:
    """Write a UTF-8 text file inside the workspace (parents auto-created).

    Paths are workspace-relative; traversal and symlink escapes are refused.
    """
    return json.dumps(await asyncio.to_thread(_file_write, workspace, path, content))


@mcp.tool(
    title="Read File",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def read_file(workspace: str, path: str, max_bytes: int = 65536) -> str:
    """Read a UTF-8 text file from the workspace, capped at max_bytes."""
    return json.dumps(await asyncio.to_thread(_file_read, workspace, path, max_bytes))


@mcp.tool(
    title="List Files",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def list_files(workspace: str, path: str = "") -> str:
    """List files/dirs under a workspace path (recursive, bounded)."""
    return json.dumps(await asyncio.to_thread(_file_list, workspace, path))


@mcp.tool(
    title="Delete File",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
)
async def delete_file(workspace: str, path: str, confirm: bool = False) -> str:
    """Delete a file or directory inside the workspace. Requires confirm=true."""
    return json.dumps(await asyncio.to_thread(_file_delete, workspace, path, confirm))


@mcp.tool(
    title="Set Env",
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False),
)
async def set_env(workspace: str, key: str, value: str) -> str:
    """Persist an env var for this workspace; run_command injects it."""
    return json.dumps(await asyncio.to_thread(_env_set, workspace, key, value))


@mcp.tool(
    title="Get Env",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def get_env(workspace: str, key: str) -> str:
    """Read one persisted env var for this workspace."""
    return json.dumps(await asyncio.to_thread(_env_get, workspace, key))


@mcp.tool(
    title="Run Command",
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
)
async def run_command(workspace: str, command: list[str], timeout_s: int | None = None) -> str:
    """Run an argv command inside the workspace (cwd = workspace root).

    argv-list only — no shell interpolation.  argv[0] must be allow-listed
    (WORKBENCH_EXEC_ALLOWLIST) and not deny-listed; the persisted workspace
    env is injected; output is capped; audit-logged.
    """
    t = _timeout_default() if timeout_s is None else int(timeout_s)
    return json.dumps(await asyncio.to_thread(_run_command, workspace, command, t))


# ---------------------------------------------------------------------------
# entrypoints
# ---------------------------------------------------------------------------


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL (fleet lesson): the MCP session manager needs its lifespan to
    run — the task group that serves /mcp requests — even in stateless mode.
    Mounting routes without ``lifespan=http_app.router.lifespan_context``
    yields /health 200 with EVERY /mcp request failing "RuntimeError: Task
    group is not initialized" (the prometheus-mcp v0.1.0 deployment bug).
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({"status": "ok", "server": "workbench-mcp"})

    # Stateless + JSON-response: MCP 2.0 is natively stateless (no
    # initialize handshake, no Mcp-Session-Id), so any replica can serve
    # any request; the lifespan wiring is still required.
    http_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_mcp_transport_security,
    )
    routes = [
        Route("/health", health),
        Route("/healthz", health),
    ]
    if _ui_enabled():
        # HPE-branded web UI + read/write JSON API at / and /api/* — a human
        # front-end over the SAME core functions that back the MCP tools
        # (path confinement, caps, allowlist and audit all still apply).
        # WORKBENCH_UI_ENABLED=false (chart: webui.enabled=false) removes
        # the UI; /mcp and the health endpoints are unaffected.
        from webui import build_ui_routes

        routes.extend(build_ui_routes())
    routes.extend(list(http_app.routes))
    return Starlette(routes=routes, lifespan=http_app.router.lifespan_context)


def main() -> None:
    parser = argparse.ArgumentParser(description="Workbench MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9103)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    app = _build_http_app()
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    logger.info("Workbench MCP streamable-http endpoint: http://%s:%s/mcp (root=%s)",
                args.host, args.port, _root())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
