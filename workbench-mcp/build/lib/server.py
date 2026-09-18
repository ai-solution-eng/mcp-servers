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
Workspaces are isolated from each other (fleet decision D7): every tool —
including ``run_command``'s argv — resolves against the addressed workspace
only, and ``WORKBENCH_SHARED_PATHS`` is the operator's escape hatch for
deliberately shared directories.  Command execution is intentional (it is
the point of a workbench) but is governed by an argv allowlist/denylist
(resolved against the server's own PATH, immune to workspace PATH
overrides), timeouts, output caps, and a JSONL audit log.

Workspace templates (Wave-5 F3, ADDITIVE, opt-in): ``workspace_create``
accepts an optional ``template`` name from the operator-defined
``WORKBENCH_TEMPLATES`` env (chart ``workbench.templates`` — rendered ONLY
when set).  A template can only WIDEN that workspace's exec allowlist with
operator-supplied binaries (the denylist still wins) and pre-run canned
setup argv commands INSIDE the new workspace through the exact
``run_command`` machinery (D7 confinement, PATH-erosion defense,
allowlist resolution, audit — each setup run is audited with the template
name).  No ``WORKBENCH_TEMPLATES`` configured = today's behavior,
byte-identical.

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

import mcp_auth
from mcp_fleet_common import health as fleet_health
from mcp_fleet_common import metrics as _fleet_common_metrics

# Wave-6 G1 (decision D16): workbench is the mcp-fleet-common PILOT. The
# binding below reproduces the old ``mcp_metrics`` module surface
# (instrument/endpoint/TOOL_REQUESTS/…) on the shared, parameterized
# implementation — vendored at ./mcp_fleet_common/ by
# pcai_utils/fleet_common_sync.sh (copies + MANIFEST.sha256 drift-check, NOT
# the hardlink mesh). Every existing ``mcp_metrics.*`` reference below (and
# in the tests) is untouched; the one behavior delta is documented in
# mcp_fleet_common/metrics.py: unknown tool names count under the
# "unknown" label (bounded cardinality) instead of the raw probed name.
mcp_metrics = _fleet_common_metrics.bind_app_metrics(metric_prefix="workbench_mcp", display_name="workbench-mcp")

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
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "PIP_CERT",
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


# ---------------------------------------------------------------------------
# workspace isolation (fleet decision D7)
# ---------------------------------------------------------------------------
#
# Every operation resolves against the ADDRESSED workspace only.  Before D7 a
# workspace's commands could read every other workspace's files and env values
# and the fleet-audit JSONL (same-uid chmod is theater — the pod runs 10001
# everywhere), because run_command confined only the cwd.  Now:
#
#   - file/env tools are confined to the addressed workspace (symlink and
#     traversal escapes were already refused; the error now names the escape
#     hatch),
#   - run_command additionally screens its argv: any path-shaped token that
#     resolves into the workbench root but outside the addressed workspace is
#     refused with a clear error (arguments, --opt=path forms, and embedded
#     absolute/`../` paths inside tokens such as python one-liners),
#   - WORKBENCH_SHARED_PATHS is the operator escape hatch: colon-separated
#     absolute paths that ALL workspaces may read/write.
#
# Honest limits (documented in README): argv screening is a policy layer at
# the MCP boundary, not a kernel sandbox — an allow-listed interpreter can
# still compute paths at runtime (python3 is in the default allowlist).  The
# pod's real defense-in-depth is unchanged: no secrets mounted, read-only
# rootfs, no SA token, network egress governed outside this server.


def _shared_roots() -> tuple[Path, ...]:
    """WORKBENCH_SHARED_PATHS — colon-separated absolute paths shared across
    ALL workspaces.  Read per call (env re-read, the fleet rotation pattern)
    so tests and operators can retarget without a restart."""
    raw = (os.environ.get("WORKBENCH_SHARED_PATHS") or "").strip()
    if not raw:
        return ()
    roots: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        if not part.startswith("/"):
            logger.warning("WORKBENCH_SHARED_PATHS entry %r is not an absolute path — ignored", part)
            continue
        roots.append(Path(part).resolve())
    return tuple(roots)


def _path_is_shared(target: Path) -> bool:
    """True if *target* is an explicitly shared path (itself or under one)."""
    for root in _shared_roots():
        if target == root or root in target.parents:
            return True
    return False


def _allowlist() -> set[str]:
    raw = os.environ.get("WORKBENCH_EXEC_ALLOWLIST", _DEFAULT_ALLOW)
    return {t.strip() for t in raw.split(",") if t.strip()}


def _denylist() -> set[str]:
    raw = os.environ.get("WORKBENCH_EXEC_DENYLIST", _DEFAULT_DENY)
    return {t.strip() for t in raw.split(",") if t.strip()}


# ---------------------------------------------------------------------------
# workspace templates (Wave-5 F3 — ADDITIVE, opt-in, none by default)
# ---------------------------------------------------------------------------
#
# WORKBENCH_TEMPLATES (env; the chart renders values.workbench.templates into
# it ONLY when set) is a JSON object of named operator templates:
#
#   {"pytools": {"description": "git scratch with a prepared src/ tree",
#                "extra_allowed": ["pytest"],
#                "canned_setup": [["mkdir", "src"], ["git", "init", "-q"]]}}
#
# workspace_create(name, template="pytools") then:
#   - WIDENS the exec allowlist for THAT workspace only: run_command inside
#     it accepts base-allowlist ∪ template.extra_allowed.  The denylist still
#     wins — a template can never re-enable a denied binary.  extra_allowed
#     entries must be bare binary names (they match argv[0] basenames).
#   - PRE-RUNS canned_setup as argv commands INSIDE the new workspace through
#     the exact _run_command machinery: D7 argv confinement, allowlist
#     resolution against the SERVER's PATH (PATH-erosion defense), timeouts,
#     output caps, and audit all apply.  Each setup run is audited as a
#     workspace_template_setup event carrying the template name.
#
# Only the template NAME is persisted in the workspace
# (.workbench-template.json); the widened allowlist is re-derived from the
# CURRENT WORKBENCH_TEMPLATES on every call, so a workspace can never be
# wider than what the operator's env defines right now (template removed
# from the env → its extras vanish; corrupt metadata file → base allowlist).
# With WORKBENCH_TEMPLATES unset or empty, the optional parameter is refused
# and every other behavior is byte-identical to the pre-template server.


def _templates() -> dict[str, dict]:
    """Parse WORKBENCH_TEMPLATES into {name: {description, extra_allowed,
    canned_setup}}.  Read per call (the fleet env-re-read pattern).  Unset,
    empty, or malformed JSON → {} (no templates; malformed input is logged
    loudly and disables templating rather than half-applying it)."""
    raw = (os.environ.get("WORKBENCH_TEMPLATES") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("WORKBENCH_TEMPLATES is not valid JSON (%s) — no templates configured", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "WORKBENCH_TEMPLATES must be a JSON object {name: {description, "
            "extra_allowed, canned_setup}} — no templates configured"
        )
        return {}
    out: dict[str, dict] = {}
    for tname, spec in data.items():
        cleaned = _template_cleaned(tname, spec)
        if cleaned is not None:
            out[tname] = cleaned
    return out


def _template_cleaned(tname: str, spec) -> dict | None:
    """Validate one template spec; None (with a loud warning) when malformed —
    a broken template is skipped entirely rather than half-applied."""
    where = f"WORKBENCH_TEMPLATES[{tname!r}]"
    if not isinstance(tname, str) or not tname.strip() or not isinstance(spec, dict):
        logger.warning("%s: names must be strings, specs objects — template skipped", where)
        return None
    description = spec.get("description", "")
    if not isinstance(description, str):
        logger.warning("%s.description must be a string — template skipped", where)
        return None
    extra_raw = spec.get("extra_allowed", [])
    if not isinstance(extra_raw, list) or not all(isinstance(b, str) for b in extra_raw):
        logger.warning("%s.extra_allowed must be a list of strings — template skipped", where)
        return None
    extra_allowed: list[str] = []
    for binname in extra_raw:
        binname = binname.strip()
        if not binname:
            continue
        if Path(binname).name != binname:
            logger.warning(
                "%s.extra_allowed entry %r is not a bare binary name "
                "(allowlist matching is on argv[0] basenames) — entry dropped",
                where,
                binname,
            )
            continue
        extra_allowed.append(binname)
    setup_raw = spec.get("canned_setup", [])
    if not isinstance(setup_raw, list):
        logger.warning("%s.canned_setup must be a list of argv lists — template skipped", where)
        return None
    canned_setup: list[list[str]] = []
    for i, argv in enumerate(setup_raw):
        if not isinstance(argv, list) or not argv or not all(isinstance(tok, str) and tok for tok in argv):
            logger.warning(
                "%s.canned_setup[%d] must be a non-empty list of string argv tokens — template skipped",
                where,
                i,
            )
            return None
        canned_setup.append(list(argv))
    return {
        "description": description,
        "extra_allowed": extra_allowed,
        "canned_setup": canned_setup,
    }


def _template_meta_path(ws: Path) -> Path:
    return ws / ".workbench-template.json"


def _ws_template_name(ws: Path) -> str | None:
    """The template name a workspace was created with, or None.  A missing,
    corrupt, or foreign metadata file fails CLOSED (base allowlist only)."""
    meta = _template_meta_path(ws)
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tname = data.get("template")
    return tname if isinstance(tname, str) and tname else None


def _ws_extra_allowed(ws: Path) -> set[str]:
    """The workspace's template-widened extra allowlist entries, RE-DERIVED
    from the current WORKBENCH_TEMPLATES (never read from the workspace file,
    which a workspace command could otherwise edit).  {} unless the
    workspace's template still exists in the operator config."""
    tname = _ws_template_name(ws)
    if not tname:
        return set()
    spec = _templates().get(tname)
    if spec is None:
        return set()
    return {b for b in spec["extra_allowed"] if b}


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
        raise WorkbenchError(f"invalid workspace name {name!r}: must match {_WS_NAME.pattern}")
    ws = _root() / name
    if not ws.is_dir():
        raise WorkbenchError(f"workspace {name!r} does not exist (create it with workspace_create)")
    return ws


def _safe_join(ws: Path, rel: str) -> Path:
    """Join *rel* under workspace *ws*, refusing escapes and symlink tricks.

    One deliberate exception (D7 escape hatch): a path that resolves under a
    WORKBENCH_SHARED_PATHS entry is allowed even though it is outside the
    addressed workspace — the operator explicitly shared it.
    """
    if rel == "":
        # An empty path is an escape attempt like any other — surface the
        # same "escapes the workspace" error the traversal cases produce.
        raise WorkbenchError(
            f"path escapes the workspace (refusing {rel!r} — paths must be "
            "workspace-relative; traversal and symlink escapes are blocked "
            "by design; to reach a directory outside this workspace the "
            "operator must list it in WORKBENCH_SHARED_PATHS)"
        )
    target = (ws / rel).resolve()
    ws_real = ws.resolve()
    if target != ws_real and ws_real not in target.parents:
        if _path_is_shared(target):
            return target
        raise WorkbenchError(
            f"path escapes the workspace (refusing {rel!r} — traversal and "
            "symlink escapes are blocked by design; to reach a directory "
            "outside this workspace the operator must list it in "
            "WORKBENCH_SHARED_PATHS)"
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


def _ws_create(name: str, template: str | None = None) -> dict:
    if not _WS_NAME.match(name):
        raise WorkbenchError(f"invalid workspace name {name!r}: must match {_WS_NAME.pattern}")
    ws = _root() / name
    if ws.exists():
        raise WorkbenchError(f"workspace {name!r} already exists")
    templates = _templates()
    spec: dict | None = None
    if template is not None:
        if not isinstance(template, str) or not template.strip():
            raise WorkbenchError(
                "template must be a non-empty template name from "
                "WORKBENCH_TEMPLATES (or omitted entirely for a plain workspace)"
            )
        spec = templates.get(template)
        if spec is None:
            configured = sorted(templates)
            listing = ", ".join(f"'{n}'" for n in configured) if configured else "NONE"
            raise WorkbenchError(
                f"unknown template {template!r} — templates are operator-defined "
                "via WORKBENCH_TEMPLATES (JSON object: {name: {description, "
                f"extra_allowed, canned_setup}}); configured templates: {listing}. "
                "Omit the template parameter for a plain workspace."
            )
    ws.mkdir(parents=True)
    event: dict = {"event": "workspace_create", "workspace": name}
    if spec is not None:
        event["template"] = template
    _audit(event)
    result: dict = {"workspace": name, "path": str(ws), "created": True}
    if spec is None:
        return result
    # Template: persist the NAME only — the widened allowlist is re-derived
    # from the operator's WORKBENCH_TEMPLATES on every call (see
    # _ws_extra_allowed), so a workspace command editing this file can at
    # most point the workspace at a DIFFERENT operator-defined template.
    meta = {"template": template, "description": spec["description"]}
    _template_meta_path(ws).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    result["template"] = template
    # Canned setup, INSIDE the new workspace, through the exact run_command
    # machinery (D7 confinement, server-PATH allowlist resolution, caps,
    # audit) — the template's extra_allowed binaries are already active
    # because the metadata file exists.  A failing/refused setup command
    # never fails the create: the workspace exists and is usable, and the
    # response (plus the audit trail) reports exactly what happened.
    setup: list[dict] = []
    setup_ok = True
    for i, argv in enumerate(spec["canned_setup"]):
        try:
            out = _run_command(
                name,
                list(argv),
                _timeout_default(),
                audit_event="workspace_template_setup",
                audit_extra={"template": template, "setup_index": i},
            )
            setup.append(
                {
                    k: out[k]
                    for k in (
                        "argv",
                        "exit_code",
                        "timed_out",
                        "duration_ms",
                        "stdout",
                        "stderr",
                        "truncated",
                    )
                }
            )
            if out["exit_code"] != 0 or out["timed_out"]:
                setup_ok = False
        except WorkbenchError as exc:
            setup_ok = False
            setup.append({"argv": list(argv), "error": str(exc)})
            _audit(
                {
                    "event": "workspace_template_setup_error",
                    "workspace": name,
                    "template": template,
                    "argv": list(argv),
                    "error": str(exc)[:500],
                }
            )
    result["setup"] = setup
    result["setup_ok"] = setup_ok
    if not setup_ok:
        result["setup_note"] = (
            "one or more template setup commands failed or were refused — the "
            "workspace still exists and the template's extra allowlist entries "
            "remain active; inspect setup[] and re-run the failed commands "
            "with run_command"
        )
    return result


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
        raise WorkbenchError("refusing to delete: pass confirm=true (destructive, irreversible)")
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
    if max_bytes < 0:
        raise WorkbenchError(f"max_bytes must be >= 0 (got {max_bytes}) — the read cap cannot be negative")
    if not target.is_file():
        raise WorkbenchError(f"no such file: {rel!r} in workspace {ws_name!r}")
    # Stream the requested slice: open + read(max_bytes) only ever pulls the
    # cap into memory.  (The old read_bytes()[:max_bytes] loaded the WHOLE
    # file first — a multi-GB file OOMed the pod even for a 1 KiB read.)
    # Semantics preserved: content is the first max_bytes bytes, truncated
    # iff the file is larger than the slice, same errors for missing/binary.
    size = target.stat().st_size
    with open(target, "rb") as fh:
        data = fh.read(max_bytes)
    truncated = size > len(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"{rel!r} is not valid UTF-8 text ({exc}); this server reads text files only") from exc
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
        try:
            relpath = str(p.relative_to(ws))
        except ValueError:
            # Entries under a WORKBENCH_SHARED_PATHS base live outside the
            # workspace — report them by absolute path instead of crashing.
            relpath = str(p)
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
        raise WorkbenchError("refusing to delete: pass confirm=true (destructive, irreversible)")
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


# ---------------------------------------------------------------------------
# run_command argv confinement (workspace isolation, D7)
# ---------------------------------------------------------------------------

# Absolute-path-shaped substrings inside an argv token: /data/x, --out=/etc/y,
# "cat /data/.audit.jsonl" inside a python one-liner.  The lookbehind keeps
# this off relative paths (a/b), division (1/2) and URL authorities
# (https://host/... — the char before /path is a word char).
_ARGV_ABS_PATH = re.compile(r"(?<![\w./@+-])(/[\w.@+-]+(?:/[\w.@+-]+)*)")
# `../`-traversal substrings anywhere in a token: ../other-ws/env.json,
# --file=../.audit.jsonl, open('../ws/f').
_ARGV_UPDIR = re.compile(r"(?<![\w./@+-])((?:\.\./)+(?:[\w.@+-]+(?:/[\w.@+-]+)*/)?)")
# A bare "." / ".." token (e.g. `ls ..`) is a path reference like any other.
_ARGV_DOT = re.compile(r"\.{1,2}")


def _argv_path_candidates(token: str) -> list[str]:
    """Filesystem references inside one argv token (whole token + embedded)."""
    cands: list[str] = []
    if "/" in token or _ARGV_DOT.fullmatch(token):
        cands.append(token)
    cands.extend(m.group(1) for m in _ARGV_ABS_PATH.finditer(token))
    cands.extend(m.group(1) for m in _ARGV_UPDIR.finditer(token))
    return cands


def _argv_escape_target(ws: Path, candidate: str) -> Path | None:
    """The resolved path if *candidate* points into the workbench root but
    outside the addressed workspace (and outside WORKBENCH_SHARED_PATHS);
    None when the reference is harmless (inside ws, or not workbench space)."""
    p = Path(candidate)
    if not p.is_absolute():
        p = ws / p
    try:
        resolved = p.resolve()
    except OSError:  # symlink loop etc. — fall back to the lexical path
        resolved = Path(os.path.normpath(p))
    root = _root()
    if resolved != root and root not in resolved.parents:
        return None  # outside the workbench volume entirely — not ours to gate
    ws_real = ws.resolve()
    if resolved == ws_real or ws_real in resolved.parents:
        return None  # inside the addressed workspace
    if _path_is_shared(resolved):
        return None  # operator-shared via WORKBENCH_SHARED_PATHS
    return resolved


def _assert_argv_confined(ws_name: str, ws: Path, command: list[str]) -> None:
    """Refuse commands whose argv references workbench files outside the
    addressed workspace (fleet decision D7).  Screens every token: direct
    path arguments, --opt=path forms, and embedded absolute/`../` paths."""
    for token in command:
        for cand in _argv_path_candidates(token):
            outside = _argv_escape_target(ws, cand)
            if outside is not None:
                reason = f"argument {token!r} references {str(outside)!r} outside workspace {ws_name!r}"
                _audit(
                    {
                        "event": "run_command_refused",
                        "workspace": ws_name,
                        "argv": list(command),
                        "reason": reason,
                    }
                )
                raise WorkbenchError(
                    f"{reason} — workspace isolation confines commands to "
                    "their own workspace (other workspaces' files, env "
                    "values and the audit JSONL are not reachable). To "
                    "share a directory across workspaces, the operator "
                    "lists it in WORKBENCH_SHARED_PATHS (colon-separated "
                    "absolute paths)"
                )


def _run_command(
    ws_name: str,
    command: list[str],
    timeout_s: int,
    audit_event: str = "run_command",
    audit_extra: dict | None = None,
) -> dict:
    ws = _ws_dir(ws_name)
    if not command or not isinstance(command, list) or not all(isinstance(a, str) for a in command):
        raise WorkbenchError("command must be a non-empty list of string argv tokens")
    # D7 BEFORE anything else: cross-workspace references are refused (and
    # audited) regardless of the allowlist verdict.
    _assert_argv_confined(ws_name, ws, command)
    binary = Path(command[0]).name  # allow "../python3"-style paths by basename
    allow, deny = _allowlist(), _denylist()
    # Workspace templates (Wave-5 F3, opt-in): a template WIDENS the
    # allowlist for its workspace with operator-supplied binaries, re-derived
    # from WORKBENCH_TEMPLATES on every call.  The denylist still wins below.
    extras = _ws_extra_allowed(ws)
    if extras:
        allow = allow | extras
    if binary in deny:
        raise WorkbenchError(f"command {binary!r} is deny-listed (WORKBENCH_EXEC_DENYLIST)")
    if binary not in allow:
        if extras:
            tname = _ws_template_name(ws)
            raise WorkbenchError(
                f"command {binary!r} is not in the allowlist for workspace "
                f"{ws_name!r} (WORKBENCH_EXEC_ALLOWLIST + template {tname!r} "
                f"extras = {sorted(allow)}) — argv[0] only, no shells"
            )
        raise WorkbenchError(
            f"command {binary!r} is not in the allowlist "
            f"(WORKBENCH_EXEC_ALLOWLIST={sorted(allow)}) — argv[0] only, no shells"
        )
    # PATH-erosion defense: the allowlist/lookup resolves against the
    # SERVER's own PATH — never against a workspace-env PATH override.  A
    # workspace PATH still flows into the child's environment (below), it
    # just cannot decide WHICH binary the allowlist lets us execute.
    server_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if "/" in command[0]:
        # Path-typed argv[0]: executed as given (cwd-relative forms resolve
        # against the workspace cwd, as before); argv confinement above has
        # already refused any form that lands outside the workspace.
        exec_argv = list(command)
    else:
        resolved = shutil.which(command[0], path=server_path)
        if resolved is None:
            raise WorkbenchError(
                f"command {command[0]!r} was not found on the server PATH — "
                "the exec allowlist resolves against the server's own PATH; "
                "a workspace PATH override flows into the child environment "
                "but never into allowlist resolution"
            )
        # Spawn the resolved absolute binary so a poisoned workspace PATH can
        # never redirect execution to a look-alike.
        exec_argv = [resolved, *command[1:]]
    timeout_s = min(max(int(timeout_s), 1), _timeout_max())
    env = {
        "PATH": server_path,
        "HOME": str(ws),
        "LANG": "C.UTF-8",
        "WORKBENCH_WORKSPACE": ws_name,
        # Operator-configured proxy / TLS-trust settings (see
        # _PROXY_PASSTHROUGH) so `pip` and friends reach the network on
        # corporate-proxy clusters.
        **{k: v for k in _PROXY_PASSTHROUGH if (v := os.environ.get(k))},
        # Per-workspace env wins over the passthrough — including PATH, which
        # reaches ONLY this child process's environment (see above).
        **_env_load(ws),
    }
    started = time.monotonic()
    try:
        proc = subprocess.run(
            exec_argv,
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
            "event": audit_event,
            "workspace": ws_name,
            "argv": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            **(audit_extra or {}),
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
async def workspace_create(name: str, template: str | None = None) -> str:
    """Create a persistent named workspace (a directory under WORKBENCH_ROOT).

    Workspaces survive pod restarts (PVC-backed) and are the container for
    all other workbench tools: files, env vars, and command runs.

    template (optional): name of an operator-defined workspace template
    (WORKBENCH_TEMPLATES env; chart values workbench.templates).  A template
    WIDENS this workspace's exec allowlist with the operator-supplied extra
    binaries (the denylist still wins) and pre-runs its canned setup
    commands inside the new workspace through the same governed runner —
    D7 confinement, server-PATH allowlist resolution, output caps, and
    audit (each setup run is audited with the template name) all apply.
    The response reports each setup command's result (setup[] / setup_ok).
    Unknown template name → an error listing the configured ones; no
    templates configured → the parameter is refused; omit it for a plain
    workspace.
    """
    return json.dumps(await asyncio.to_thread(_ws_create, name, template))


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
    """Read a UTF-8 text file from the workspace, capped at max_bytes.

    Streams only the requested slice (bounded memory — multi-GB files are
    safe to read); paths outside the addressed workspace are refused unless
    the operator shared them via WORKBENCH_SHARED_PATHS.
    """
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
    (WORKBENCH_EXEC_ALLOWLIST) and not deny-listed; the allowlist resolves
    against the server's own PATH (a workspace PATH override reaches only
    the child environment).  Workspace isolation: argv may not reference
    files outside this workspace (WORKBENCH_SHARED_PATHS excepted).  The
    persisted workspace env is injected; output is capped; audit-logged.
    """
    t = _timeout_default() if timeout_s is None else int(timeout_s)
    return json.dumps(await asyncio.to_thread(_run_command, workspace, command, t))


# ---------------------------------------------------------------------------
# self-metrics (Wave-3 C3 — additive, chart-gated DEFAULT-OFF)
# ---------------------------------------------------------------------------

# Count every MCP-protocol tool call (per-tool {ok,error} counters — see
# mcp_fleet_common/metrics.py, the shared implementation). Unconditional and
# inert: the counters exist from import time, but nothing exposes them unless
# the /metrics route below is mounted, which requires the chart to set
# WORKBENCH_METRICS_ENABLED (metrics.enabled, default false). No behavior
# change when metrics are off.
mcp_metrics.instrument(mcp)


def _metrics_enabled() -> bool:
    """Serve the /metrics endpoint? (WORKBENCH_METRICS_ENABLED, default off.)

    The chart renders this env — and the ServiceMonitor — ONLY when
    ``metrics.enabled: true`` (values), so a default deployment has no
    /metrics route at all. Read per call (env re-read, the fleet pattern)
    so tests can flip it without reimporting.
    """
    raw = os.environ.get("WORKBENCH_METRICS_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# entrypoints
# ---------------------------------------------------------------------------


# ─── API-key authentication (fleet pattern, shared module: pcai_utils/mcp_auth.py) ───
#
# Workbench's own HTTP surface includes /api/ws/{ws}/run — arbitrary process
# execution inside the pod — plus file write/delete and env mutation. Until
# now ALL of it trusted network position alone (fleet-audit CRITICAL: this
# was unauthenticated workspace RCE). Unlike applygate (where only /mcp
# mutates and the console is read-only), here EVERYTHING except the probes
# is behind the key, and auth is MANDATORY in deployment: the chart wires
# the key env from an operator-created Secret and the pod fails loud until
# it exists.
#
# * One-address wiring: the UNIVERSAL MCP_API_KEYS is honored alongside the
#   per-server WORKBENCH_API_KEYS (key sets unioned, constant-time compares).
#   Comma-separated keys within either var are the rotation mechanism —
#   append the new key, move clients over, drop the old one, no downtime.
# * The bundled console sends the key from a login prompt (sessionStorage
#   only, never localStorage — the K8S-MCP console pattern).
# * No keys configured: everything is open (local development mode);
#   main() screams about it once at startup.

WORKBENCH_API_KEYS_ENV = "WORKBENCH_API_KEYS"

AUTH_ENV_NAMES = (mcp_auth.UNIVERSAL_API_KEYS_ENV, WORKBENCH_API_KEYS_ENV)


def _configured_api_keys() -> list:
    return mcp_auth.configured_keys(AUTH_ENV_NAMES)


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL (fleet lesson): the MCP session manager needs its lifespan to
    run — the task group that serves /mcp requests — even in stateless mode.
    Mounting routes without ``lifespan=http_app.router.lifespan_context``
    yields /health 200 with EVERY /mcp request failing "RuntimeError: Task
    group is not initialized" (the prometheus-mcp v0.1.0 deployment bug).
    """
    from starlette.applications import Starlette
    from starlette.routing import Route

    # Stateless + JSON-response: MCP 2.0 is natively stateless (no
    # initialize handshake, no Mcp-Session-Id), so any replica can serve
    # any request; the lifespan wiring is still required.
    http_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_mcp_transport_security,
    )
    # The probe pair comes from the shared package (Wave-6 G1 pilot): the
    # same two routes, the same JSONResponse body — {"status": "ok",
    # "server": "workbench-mcp"} — the probes stay public and key-free.
    routes = list(fleet_health.health_routes({"status": "ok", "server": "workbench-mcp"}))
    if _metrics_enabled():
        # Prometheus self-metrics (Wave-3 C3, ADDITIVE, default OFF): per-tool
        # request counters ONLY — no arguments, file names, workspace names,
        # env values, or error text are exported (see
        # mcp_fleet_common/metrics.py). Served
        # key-free like the probes so the ServiceMonitor can scrape it; the
        # data is non-sensitive, and the route exists at all only when the
        # chart opted in (metrics.enabled=true → WORKBENCH_METRICS_ENABLED).
        routes.append(Route("/metrics", mcp_metrics.endpoint))
    if _ui_enabled():
        # HPE-branded web UI + read/write JSON API at / and /api/* — a human
        # front-end over the SAME core functions that back the MCP tools
        # (path confinement, caps, allowlist and audit all still apply).
        # WORKBENCH_UI_ENABLED=false (chart: webui.enabled=false) removes
        # the UI; /mcp and the health endpoints are unaffected.
        from webui import build_ui_routes

        routes.extend(build_ui_routes())
    routes.extend(list(http_app.routes))
    # The API-key middleware wraps the assembled app (default public paths =
    # /health+/healthz only, so the console, every /api/* route — including
    # run — and /mcp all require the key) — built in here, not in main(), so
    # ANY consumer of _build_http_app gets the authenticated app. The console
    # JS sends the key via X-API-Key from its login prompt. When the chart
    # opted into metrics, /metrics joins the public set (non-sensitive
    # counters, ServiceMonitor-scrapeable); default (metrics off) passes
    # public_paths=None → exactly the pre-metrics behavior.
    public_paths = None
    if _metrics_enabled():
        public_paths = (*mcp_auth.DEFAULT_PUBLIC_PATHS, "/metrics")
    return mcp_auth.ApiKeyAuthMiddleware(
        Starlette(routes=routes, lifespan=http_app.router.lifespan_context),
        env_names=AUTH_ENV_NAMES,
        public_paths=public_paths,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Workbench MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9103)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # LOUD: an open run-command endpoint is fine only on a laptop. The
    # middleware passes everything through when no keys are configured, so a
    # silent empty key list looks exactly like an authenticated server.
    mcp_auth.warn_if_open("workbench-mcp", AUTH_ENV_NAMES)

    import uvicorn

    app = _build_http_app()
    # No CORSMiddleware: the old allow_origins=["*"] let any website the
    # operator visits read the API cross-origin (fleet audit S-6). The
    # console is same-origin; MCP clients are not browsers (CORS is
    # browser-enforced only); browser-based clients must go through the
    # gateway, which enforces origin policy.
    logger.info(
        "Workbench MCP streamable-http endpoint: http://%s:%s/mcp (root=%s, shared_paths=%s)",
        args.host,
        args.port,
        _root(),
        [str(p) for p in _shared_roots()] or "none",
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
