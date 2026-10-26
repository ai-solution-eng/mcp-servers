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
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

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

logging.basicConfig(level=os.environ.get("WORKBENCH_LOG_LEVEL", "INFO"))
logger = logging.getLogger("workbench-mcp")

mcp = MCPServer("workbench-mcp")
# Host-header (DNS-rebinding) protection from env (fleet-shared mcp_auth):
# MCP_HOSTNAME pins the public FQDN, MCP_EXTRA_ALLOWED_HOSTS adds in-cluster
# service-DNS hosts.  With NEITHER set (local dev) the helper returns None —
# the SDK's implicit default applies untouched: protection auto-enables
# loopback-only when the app binds 127.0.0.1 (the dev default), which is the
# K8S-MCP fleet semantics (the old explicit
# ``TransportSecuritySettings(enable_dns_rebinding_protection=False)``
# disabled that SDK auto-enable; loopback dev clients send a loopback Host
# header, so the practical dev behavior is unchanged — only a client that
# forges a non-loopback Host against a loopback-bound server now gets 421,
# which is the SDK's own default posture, not a regression).  Chart wiring:
# mcpHostname / extraAllowedHosts → these two env vars.
_mcp_transport_security = mcp_auth.transport_security_from_env()

# ---------------------------------------------------------------------------
# configuration (read lazily so tests can retarget the root)
# ---------------------------------------------------------------------------

_WS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Fleet decision D18 (2026-09-13, operator-ratified): NO interpreter and no
# package manager in the default allowlist — python3/pip/pip3 removed. argv
# screening is not a kernel sandbox, and an allow-listed interpreter can
# compute paths at runtime (split-string paths never appear in argv), so the
# default is narrow argv tools only. Operators re-add interpreters explicitly
# via WORKBENCH_EXEC_ALLOWLIST / values.execAllowlist when their threat model
# accepts the documented residual (README "Honest limits").
_DEFAULT_ALLOW = "ls,cat,head,tail,grep,find,wc,du,df,mkdir,touch,cp,mv,tar,git,diff,sort,uniq"
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
# still compute paths at runtime (no interpreter is in the default allowlist — D18; operators re-add via WORKBENCH_EXEC_ALLOWLIST).  The
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


def _exec_max_concurrency() -> int:
    """Worker width of the dedicated run_command pool (WORKBENCH_EXEC_MAX_
    CONCURRENCY, default 4, env re-read per call — the fleet convention).

    run_command jobs can legitimately run to WORKBENCH_EXEC_TIMEOUT_MAX
    (600s).  Unbounded asyncio.to_thread offload put them on the DEFAULT
    executor, so a burst of long commands starved every other tool's
    offload (file/env tools included).  run_command now runs on its OWN
    pool; when all workers are busy it returns a structured busy response
    instead of queueing (no queue, no reordering), and file/env tools stay
    on the default executor untouched.
    """
    return max(1, _int_env("WORKBENCH_EXEC_MAX_CONCURRENCY", 4))


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


def _audit_max_bytes() -> int:
    """Rotation threshold for the audit JSONL (WORKBENCH_AUDIT_MAX_BYTES,
    default 100 MiB, env re-read per call — the fleet convention)."""
    return _int_env("WORKBENCH_AUDIT_MAX_BYTES", 100 * 1024 * 1024)


def _audit_rotate(active: Path) -> None:
    """Rotate a full audit JSONL: rename to ``<name>.1`` (single generation —
    the previous .1 is OVERWRITTEN), and record the rotation so the chain
    across generations is discoverable.

    LOCAL rotation by design: workbench's audit writer is a plain best-effort
    JSONL appender (server._audit), NOT the vendored
    mcp_fleet_common.audit.HashChainedAuditLog (nothing in this server binds
    the shared writer, and it is FROZEN — out of scope).  There is therefore
    no hash-chain contract to satisfy; the first entry of a new generation
    carries ``rotated_from`` (the sha256 of the last line of the rotated
    file) so a reader can still PROVE which generation preceded it, with the
    break between generations explicit in the README.
    """
    rotated = active.with_name(active.name + ".1")
    last_sha = ""
    try:
        with open(active, "rb") as fh:
            for line in fh:  # stream: never loads a 100 MiB trail into memory
                stripped = line.strip()
                if stripped:
                    last_sha = hashlib.sha256(stripped).hexdigest()
        os.replace(active, rotated)  # atomic on POSIX; overwrites previous .1
    except OSError:
        return  # best-effort: keep appending to the oversized file rather than lose events
    # Best-effort marker line at the head of the fresh generation.
    try:
        with open(active, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "event": "audit_rotated",
                        "rotated_to": rotated.name,
                        "rotated_from": last_sha,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def _audit(event: dict) -> None:
    try:
        active = _root() / ".audit.jsonl"
        try:
            if active.is_file() and active.stat().st_size >= _audit_max_bytes():
                _audit_rotate(active)
        except OSError:
            pass  # size probe failed: append anyway (best-effort audit)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
        with open(active, "a", encoding="utf-8") as fh:
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


def _ws_list(deep: bool = False) -> list[dict]:
    """List workspaces under the root.

    deep=False (the MCP tool default) is LAZY: names only, with
    ``files``/``bytes`` = None and a ``counts`` marker — no rglob, so the
    listing stays O(#workspaces) even on an NFS-backed PVC holding hundreds
    of thousands of inodes (the old unconditional rglob+stat walked the
    ENTIRE tree on every call).  deep=True preserves the exact pre-lazy
    behavior (per-workspace file count and total bytes); the console's
    /api/workspaces handler uses it so the UI is unchanged.
    """
    root = _root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not deep:
            out.append({"workspace": entry.name, "files": None, "bytes": None, "counts": "lazy (pass deep=true)"})
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


def _file_write(ws_name: str, rel: str, content: str, append: bool = False) -> dict:
    ws = _ws_dir(ws_name)
    target = _safe_join(ws, rel)
    payload = content.encode("utf-8")
    limit = _max_file_bytes()
    if append:
        # Chunking path (fleet audit: the old over-cap error told agents to
        # "write in chunks" while every write REPLACED the file).  The cap is
        # enforced CUMULATIVELY — existing size + incoming payload must fit —
        # so append is a supported chunking path, never a cap bypass.
        try:
            existing = target.stat().st_size if target.is_file() else 0
        except OSError:
            existing = 0
        total = existing + len(payload)
        if total > limit:
            raise WorkbenchError(
                f"append of {len(payload)} bytes would make {rel!r} {total} bytes "
                f"(existing {existing}); cap is {limit} (WORKBENCH_MAX_FILE_BYTES) — "
                "the append flag enforces the cap cumulatively (existing size + "
                "incoming payload ≤ cap) and cannot bypass it; split the payload "
                "into smaller appends or raise the cap"
            )
    else:
        if len(payload) > limit:
            raise WorkbenchError(
                f"content is {len(payload)} bytes; cap is {limit} "
                "(WORKBENCH_MAX_FILE_BYTES) — write in chunks with "
                "write_file(append=true) (the supported chunking path: each "
                "append enforces the cap against the file's new total size) "
                "or raise the cap"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with open(target, "ab") as fh:
            fh.write(payload)
        return {
            "workspace": ws_name,
            "path": rel,
            "bytes": len(payload),
            "written": True,
            "appended": True,
            "total_bytes": existing + len(payload),
        }
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

# Dedicated executor for run_command ONLY (fleet-audit finding: 10 unbounded
# asyncio.to_thread offload sites; long run_commands — up to
# WORKBENCH_EXEC_TIMEOUT_MAX = 600s — on the DEFAULT executor starved every
# other tool's offload).  File/env/list tools stay on the default executor.
# The pool is created lazily on first use so tests that reimport/reset
# module state keep working, and _EXEC_WIDTH tracks the width the pool was
# built with (the env knob is re-read per call; a changed width rebuilds the
# pool rather than misreporting busy/running counts).
#
# Occupancy is tracked with OUR OWN counter (_EXEC_INFLIGHT), not the
# stdlib's _idle_semaphore: the semaphore is eventually-consistent (tokens
# are restored by the worker's NEXT loop iteration, and a fresh pool reads 0
# until its first item completes), which would false-busy the very first
# calls.  in-flight submits − completions is exact: submit and completion
# bookkeeping happen inside the wrapped callable, in worker threads, under
# _EXEC_LOCK.
_EXEC_POOL: ThreadPoolExecutor | None = None
_EXEC_WIDTH: int = 0
_EXEC_LOCK = threading.Lock()
_EXEC_INFLIGHT = 0


def _exec_pool() -> ThreadPoolExecutor:
    global _EXEC_POOL, _EXEC_WIDTH
    width = _exec_max_concurrency()
    with _EXEC_LOCK:
        if _EXEC_POOL is None or _EXEC_WIDTH != width:
            if _EXEC_POOL is not None:
                _EXEC_POOL.shutdown(wait=False)
            _EXEC_POOL = ThreadPoolExecutor(
                max_workers=width, thread_name_prefix="workbench-exec"
            )
            _EXEC_WIDTH = width
        return _EXEC_POOL


async def _exec_run(coro_fn, *args) -> dict | str:
    """Offload *coro_fn* onto the dedicated run_command pool, or return a
    structured busy response WITHOUT executing when all workers are busy.

    Simplest honest semantics: no queue, no reordering — a busy pool tells
    the model so it can retry shortly, and the audit log records the busy
    outcome like any other non-run.
    """
    global _EXEC_INFLIGHT
    with _EXEC_LOCK:
        if _EXEC_INFLIGHT >= _exec_max_concurrency():
            running_n = _EXEC_WIDTH or _exec_max_concurrency()
            _audit(
                {
                    "event": "run_command_busy",
                    "running": running_n,
                    "max_concurrency": running_n,
                }
            )
            return {
                "ok": False,
                "busy": True,
                "running": running_n,
                "hint": "retry shortly",
            }
        _EXEC_INFLIGHT += 1
    pool = _exec_pool()
    loop = asyncio.get_running_loop()

    def _accounted():
        global _EXEC_INFLIGHT
        try:
            return coro_fn(*args)
        finally:
            with _EXEC_LOCK:
                _EXEC_INFLIGHT -= 1

    return await asyncio.shield(loop.run_in_executor(pool, _accounted))


@mcp.tool(
    title="Create Workspace",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
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
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def workspace_list(deep: bool = False) -> str:
    """List workspaces with file counts and total bytes.

    deep=false (default) is lazy: workspace NAMES only — each entry carries
    ``files: null`` / ``bytes: null`` and ``"counts": "lazy (pass deep=true)"``.
    Counting walks the whole PVC tree (rglob + stat per file), which is
    O(all inodes) on an NFS-backed volume; pass deep=true only when you
    actually need the sizes.  Response keys are present in BOTH modes.
    """
    return json.dumps(await asyncio.to_thread(_ws_list, deep))


@mcp.tool(
    title="Delete Workspace",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
)
async def workspace_delete(name: str, confirm: bool = False) -> str:
    """Delete a workspace and everything in it. Requires confirm=true."""
    return json.dumps(await asyncio.to_thread(_ws_delete, name, confirm))


@mcp.tool(
    title="Write File",
    annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
)
async def write_file(workspace: str, path: str, content: str, append: bool = False) -> str:
    """Write a UTF-8 text file into a persistent workbench workspace (parents
    auto-created).  Choose this over a plain session write tool when the file
    must outlive the conversation — workspaces are PVC-backed and survive pod
    restarts — or when a workbench run_command, or another fleet server via
    WORKBENCH_SHARED_PATHS, needs to read it next.  For throwaway scratch
    files that only this session touches, a session write tool is fine.

    append=true appends to the file instead of replacing it (creating it on
    first use) — the supported way to write content larger than the
    WORKBENCH_MAX_FILE_BYTES cap in chunks.  The cap still applies
    CUMULATIVELY: existing size + incoming payload must fit, so an append can
    never smuggle a file past the cap.  An appending response carries
    "appended": true and "total_bytes" (the file's new size).

    Paths are workspace-relative; traversal and symlink escapes are refused.
    """
    return json.dumps(await asyncio.to_thread(_file_write, workspace, path, content, append))


@mcp.tool(
    title="Read File",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
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
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def list_files(workspace: str, path: str = "") -> str:
    """List files/dirs under a workspace path (recursive, bounded)."""
    return json.dumps(await asyncio.to_thread(_file_list, workspace, path))


@mcp.tool(
    title="Delete File",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
)
async def delete_file(workspace: str, path: str, confirm: bool = False) -> str:
    """Delete a file or directory inside the workspace. Requires confirm=true."""
    return json.dumps(await asyncio.to_thread(_file_delete, workspace, path, confirm))


@mcp.tool(
    title="Set Env",
    annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
)
async def set_env(workspace: str, key: str, value: str) -> str:
    """Persist an env var for this workspace — it survives pod restarts (stored
    with the workspace, not the session) and run_command injects it into every
    child process it runs in this workspace."""
    return json.dumps(await asyncio.to_thread(_env_set, workspace, key, value))


@mcp.tool(
    title="Get Env",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def get_env(workspace: str, key: str) -> str:
    """Read one persisted env var for this workspace."""
    return json.dumps(await asyncio.to_thread(_env_get, workspace, key))


@mcp.tool(
    title="Run Command",
    annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
)
async def run_command(workspace: str, command: list[str], timeout_s: int | None = None) -> str:
    """Run an argv command inside the workspace (cwd = workspace root) — the
    audited, workspace-persistent way to run git/tar/grep-style tools on
    workspace files (every run is audit-logged with the caller's identity,
    and the workspace's persisted env vars are injected).

    argv-list only — no shell interpolation.  argv[0] must be allow-listed
    (WORKBENCH_EXEC_ALLOWLIST) and not deny-listed; the allowlist resolves
    against the server's own PATH (a workspace PATH override reaches only
    the child environment).  Workspace isolation: argv may not reference
    files outside this workspace (WORKBENCH_SHARED_PATHS excepted).  Output
    is capped.

    Exec policy (fleet decision D18): the DEFAULT allowlist is narrow argv
    tools only — ls, cat, head, tail, grep, find, wc, du, df, mkdir, touch,
    cp, mv, tar, git, diff, sort, uniq.  NO interpreters (python3) and no
    package managers (pip/pip3) are available by default: an allow-listed
    interpreter could compute paths at runtime and sidestep the workspace
    confinement, so compute belongs outside the workbench (or in a
    dedicated runner).  If a command is refused as "not in the allowlist",
    that is this policy, not a malfunction — ask the operator to add the
    binary to WORKBENCH_EXEC_ALLOWLIST (chart values execAllowlist /
    workbench.templates extra_allowed) if it is genuinely needed.

    Concurrency: commands run on a dedicated pool of
    WORKBENCH_EXEC_MAX_CONCURRENCY (default 4) workers so long runs never
    starve the other tools.  When every worker is busy the call returns
    {"ok": false, "busy": true, "running": N, "hint": "retry shortly"}
    WITHOUT executing (no queue, no reordering) — retry shortly.
    """
    t = _timeout_default() if timeout_s is None else int(timeout_s)
    return json.dumps(await _exec_run(_run_command, workspace, command, t))


# ---------------------------------------------------------------------------
# sandbox_exec (v1 — no-network python execution pods, ADDITIVE, DEFAULT OFF)
# ---------------------------------------------------------------------------
#
# D18 keeps interpreters out of run_command's allowlist: an allow-listed
# interpreter inside the workbench pod could sidestep the argv-level D7
# confinement.  But agents DO need python.  The v1 answer is a separate,
# always-running executor pool in the SAME namespace (chart
# helm/templates/executors.yaml, values executors.* — DEFAULT OFF): hardened
# pods running the SAME image with a `python3 -c "time.sleep(...)"` command,
# wrapped in a deny-all NetworkPolicy, with no SA token, no GPU request, and
# a read-only rootfs.  `sandbox_run` dispatches ONE python FILE from the
# addressed workspace into the least-loaded Ready executor via the
# Kubernetes exec subresource (kubernetes.stream), writes the script bytes to
# stdin of `python3 -I --`, and returns stdout/stderr.
#
# What the executor deliberately is NOT (honest limits, README): NOT gVisor —
# the isolation tier is the container boundary itself plus no-network (the
# deny-all netpol), no-credentials (no automounted token), no-GPU (no
# resource request) and non-root.  v1 runs a single file with argv; no
# filesystem mounts, no network access, no state between runs.
#
# The tool is registered ALWAYS (fleet refusals-are-answers doctrine) and
# refuses self-describingly when WORKBENCH_SANDBOX_EXEC is off (default) or
# when the chart could not create the exec Role (the 403 surfaces as a
# self-describing RBAC error — a "no" is a result, the k8s-mcp auth can-i
# lesson).

_SANDBOX_EXEC_ENABLED_ENV = "WORKBENCH_SANDBOX_EXEC"
_SANDBOX_LABEL_ENV = "WORKBENCH_SANDBOX_LABEL"
_SANDBOX_CONTAINER_ENV = "WORKBENCH_SANDBOX_CONTAINER"
_SANDBOX_TIMEOUT_ENV = "WORKBENCH_SANDBOX_TIMEOUT_S"
_SANDBOX_MAX_OUTPUT_ENV = "WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS"

# The workload hardening means the run is untrusted-container-tier, not
# gVisor-tier; the timeout also bounds it (clamp 1..600, the run_command
# timeout-max convention).
_SANDBOX_TIMEOUT_MIN_S = 1
_SANDBOX_TIMEOUT_MAX_S = 600


def _sandbox_exec_enabled() -> bool:
    """Master switch of the sandbox_run tool (WORKBENCH_SANDBOX_EXEC, default
    off).  Read per call (env re-read, the fleet pattern).  The chart renders
    this env ONLY when workbench.sandboxExec is true — which is documented as
    a PAIR with executors.enabled (the executor pool itself)."""
    raw = os.environ.get(_SANDBOX_EXEC_ENABLED_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _sandbox_label() -> str:
    """Pod label selector sandbox_run balances over (default app=workbench-exec
    — the chart's executors.appName)."""
    return os.environ.get(_SANDBOX_LABEL_ENV, "").strip() or "app=workbench-exec"


def _sandbox_container() -> str:
    """Exec target container inside each executor pod (default `python` — the
    chart's executors.containerName)."""
    return os.environ.get(_SANDBOX_CONTAINER_ENV, "").strip() or "python"


def _sandbox_timeout_s() -> int:
    """Per-run wall clock (WORKBENCH_SANDBOX_TIMEOUT_S, default 120, clamped
    1..600).  Also drives the exec CONNECT and READ timeouts."""
    raw = _int_env(_SANDBOX_TIMEOUT_ENV, 120)
    return min(max(raw, _SANDBOX_TIMEOUT_MIN_S), _SANDBOX_TIMEOUT_MAX_S)


def _sandbox_max_output_chars() -> int:
    """stdout/stderr cap per run (WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS, default
    50000, clamped >= 1 — the fleet k8s-mcp exec convention; a degenerate
    value must not disable truncation)."""
    return max(1, _int_env(_SANDBOX_MAX_OUTPUT_ENV, 50000))


def _sandbox_namespace() -> str:
    """The namespace to exec into: the pod's OWN namespace when running in
    cluster (the service-account namespace file), else the RELEASE_NAMESPACE
    env (chart-rendered, for tests), else "default".  The executor pool lives
    in the SAME namespace as the workbench by construction."""
    try:
        ns = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text(encoding="utf-8").strip()
        if ns:
            return ns
    except OSError:
        pass
    return (os.environ.get("RELEASE_NAMESPACE") or "").strip() or "default"


_SANDBOX_ARGV_MAX_BYTES = 8192  # total argv bytes per exec (fleet k8s-mcp screen discipline)


def _sandbox_truncate(text: str, cap: int) -> tuple[str, bool]:
    """Cap one output stream at *cap* chars, appending the fleet's explicit
    truncation marker when the cap bites (" ...[truncated N chars]")."""
    cap = max(1, cap)  # degenerate 0/negative must not disable truncation
    if len(text) <= cap:
        return text, False
    return text[:cap] + f" ...[truncated {len(text) - cap} chars]", True


def _sandbox_refuse(
    outcome: str,
    message: str,
    workspace: str | None = None,
    pod: str | None = None,
    **extra,
) -> None:
    """Audit a refusal/error outcome, then RAISE the self-describing message
    the model sees — the sibling refusal shape (WorkbenchError raised, the
    MCP layer surfaces it verbatim)."""
    event = {
        "event": "sandbox_run",
        "outcome": outcome,
        **({"workspace": workspace} if workspace else {}),
        **({"pod": pod} if pod else {}),
        **({"path": extra.pop("path")} if "path" in extra else {}),
        "error": message[:500],
        **extra,
    }
    _audit(event)
    raise WorkbenchError(message)


# In-flight occupancy per executor pod — the run_command pool's _EXEC_INFLIGHT
# pattern, per-pod: incremented when a run is dispatched to a pod and
# decremented in a try/finally around the exec (BOTH success and exception
# paths — a leaked count would permanently shrink the usable pool).  Under
# _SANDBOX_LOCK like the pool counters.
_SANDBOX_LOCK = threading.Lock()
_SANDBOX_INFLIGHT: dict[str, int] = {}


def _sandbox_select_pod(ready_pods: list[str]) -> str:
    """Pick the least-loaded Ready pod under the lock, increment its count,
    and return the name.  Raises WorkbenchError("busy") when every Ready pod
    already carries >= 1 in-flight run (no queue, the run_command busy
    semantics) — the caller must NOT have incremented in that case."""
    with _SANDBOX_LOCK:
        if not ready_pods:
            raise WorkbenchError("no executor pods Ready")
        counts = {p: _SANDBOX_INFLIGHT.get(p, 0) for p in ready_pods}
        idle = sorted(p for p, c in counts.items() if c == 0)
        if not idle:
            raise WorkbenchError("all executor pods busy")
        pod = idle[0]  # deterministic: least-loaded (all 0 here), name order
        _SANDBOX_INFLIGHT[pod] = _SANDBOX_INFLIGHT.get(pod, 0) + 1
        return pod


def _sandbox_release_pod(pod: str) -> None:
    """Decrement the in-flight count — ALWAYS via try/finally in the caller
    so both success and exception paths release the slot."""
    with _SANDBOX_LOCK:
        n = _SANDBOX_INFLIGHT.get(pod, 0)
        if n <= 1:
            _SANDBOX_INFLIGHT.pop(pod, None)
        else:
            _SANDBOX_INFLIGHT[pod] = n - 1


def _sandbox_exec(
    workspace_name: str,
    ws: Path,
    rel: str,
    argv: list[str],
) -> dict:
    """The sandbox_run core: validate the file, select an executor, exec
    `python3 -I -- *argv` with the script on stdin, collect output.

    The kubernetes client is LAZY-imported here (not at module top) so the
    test suite and stdio-dev mode never require it, and the seam the tests
    stub is `_k8s_exec_stream` / `_k8s_list_pods` (module-level functions).
    """
    # -- fence (SAME helpers run_command/read_file use; refusal shapes identical)
    limit = _max_file_bytes()
    target = _safe_join(ws, rel)
    if not target.is_file():
        _sandbox_refuse(
            "refused",
            f"no such file: {rel!r} in workspace {workspace_name!r}",
            workspace=workspace_name,
            path=rel,
        )
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise WorkbenchError(f"cannot stat {rel!r} in workspace {workspace_name!r}: {exc}") from exc
    if size > limit:
        msg = (
            f"script {rel!r} is {size} bytes; cap is {limit} "
            "(WORKBENCH_MAX_FILE_BYTES) — the sandbox runs a single file; "
            "split it or raise the cap"
        )
        _sandbox_refuse("refused", msg, workspace=workspace_name, path=rel)
    script = target.read_bytes()

    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        msg = "argv must be a list of string argv tokens (possibly empty)"
        _sandbox_refuse("refused", msg, workspace=workspace_name, path=rel)
    # Fleet k8s-mcp exec screen: NUL/empty argv tokens fail at the API server
    # with an OPAQUE error — refuse self-describingly instead (and keep them
    # out of the audit line).
    if any("\x00" in a or a == "" for a in argv):
        msg = "argv tokens must be non-empty and contain no NUL bytes"
        _sandbox_refuse("refused", msg, workspace=workspace_name, path=rel)
    # One exec launch request carries the whole argv (URL-length bound) and it
    # lands verbatim in the audit line — cap it like every other bounded field.
    argv_bytes = sum(len(a.encode("utf-8")) for a in argv)
    if argv_bytes > _SANDBOX_ARGV_MAX_BYTES:
        msg = (
            f"argv is {argv_bytes} bytes; cap is {_SANDBOX_ARGV_MAX_BYTES} "
            "(WORKBENCH_SANDBOX_ARGV_MAX_BYTES) — pass arguments via the "
            "script file instead"
        )
        _sandbox_refuse("refused", msg, workspace=workspace_name, path=rel)

    timeout_s = _sandbox_timeout_s()
    cap = _sandbox_max_output_chars()
    started = time.monotonic()

    # -- discovery + selection (Ready pods matching the label, least-loaded)
    try:
        ready = _k8s_list_pods(_sandbox_label(), _sandbox_namespace())
    except Exception as exc:  # surfaced verbatim, never a crash path
        _sandbox_refuse(
            "error",
            f"executor discovery failed ({type(exc).__name__}: {exc}) — is "
            "executors.enabled true in the chart values and is the cluster "
            "reachable?",
            workspace=workspace_name,
        )
    if not ready:
        _sandbox_refuse(
            "error",
            "no executor pods Ready — is executors.enabled true and the pool "
            f"rolled out? (label selector {_sandbox_label()!r} in namespace "
            f"{_sandbox_namespace()!r}; check `kubectl -n <ns> get pods -l "
            f"{_sandbox_label()}`)",
            workspace=workspace_name,
        )
    try:
        pod = _sandbox_select_pod(ready)
    except WorkbenchError:
        running_n = len(ready)
        _audit(
            {
                "event": "sandbox_run_busy",
                "workspace": workspace_name,
                "running": running_n,
                "pods": ready,
            }
        )
        return json.dumps(
            {"ok": False, "busy": True, "running": running_n, "hint": "retry shortly"}
        )
    try:
        # -- dispatch (try/finally around the exec: the in-flight slot is
        #    released on BOTH success and exception paths)
        try:
            proc_rc, stdout_bytes, stderr_bytes = _k8s_exec_stream(
                pod,
                _sandbox_namespace(),
                _sandbox_container(),
                ["python3", "-I", "--", *argv],
                script,
                timeout_s,
            )
            elapsed_s = round(time.monotonic() - started, 3)
            stdout, stdout_cut = _sandbox_truncate(
                stdout_bytes.decode("utf-8", errors="replace").replace("\x00", ""), cap
            )
            stderr, stderr_cut = _sandbox_truncate(
                stderr_bytes.decode("utf-8", errors="replace").replace("\x00", ""), cap
            )
            result = {
                "ok": True,
                "exit_code": proc_rc,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_cut or stderr_cut,
                "pod": pod,
                "elapsed_s": elapsed_s,
            }
            _audit(
                {
                    "event": "sandbox_run",
                    "outcome": "ok" if not result["truncated"] else "ok_truncated",
                    "workspace": workspace_name,
                    "pod": pod,
                    "exit_code": proc_rc,
                    "duration_s": elapsed_s,
                    "argv": argv,
                }
            )
            return json.dumps(result)
        except _SandboxExecTimeout as exc:
            elapsed_s = round(time.monotonic() - started, 3)
            partial_out, partial_err = exc.partial_stdout, exc.partial_stderr
            out, out_cut = _sandbox_truncate(partial_out.decode("utf-8", errors="replace"), cap)
            err, err_cut = _sandbox_truncate(partial_err.decode("utf-8", errors="replace"), cap)
            result = {
                "ok": False,
                "timeout": True,
                "timeout_s": timeout_s,
                "stdout": out,
                "stderr": err,
                "truncated": out_cut or err_cut,
                "partial": bool(partial_out or partial_err),
                "pod": pod,
                "elapsed_s": elapsed_s,
            }
            _audit(
                {
                    "event": "sandbox_run",
                    "outcome": "timeout",
                    "workspace": workspace_name,
                    "pod": pod,
                    "timeout_s": timeout_s,
                    "duration_s": elapsed_s,
                    "argv": argv,
                }
            )
            return json.dumps(result)
        except _SandboxRbacError as exc:
            _sandbox_refuse(
                "refused",
                f"RBAC: {exc} — the chart could not (or did not) create the "
                f"sandbox exec Role in namespace {_sandbox_namespace()!r}; an "
                "operator must apply helm/templates/executors.yaml "
                "(executors.enabled: true) which grants the workbench "
                "service account get/list on pods + create on pods/exec in "
                "its OWN namespace. Check with: kubectl -n <ns> auth can-i "
                "create pods/exec --as=system:serviceaccount:<ns>:<sa>",
                workspace=workspace_name,
                pod=pod,
            )
        except Exception as exc:  # the exec path must never 500
            _sandbox_refuse(
                "error",
                f"sandbox exec failed ({type(exc).__name__}: {exc}) — the "
                "executor pod is stateless and unharmed; retry, and if this "
                "persists check the executor pool's rollout status",
                workspace=workspace_name,
                pod=pod,
            )
    finally:
        _sandbox_release_pod(pod)


class _SandboxExecTimeout(Exception):
    """The exec stream exceeded WORKBENCH_SANDBOX_TIMEOUT_S.  Carries any
    partial output captured before the deadline; a timed-out exec leaves the
    stateless executor pod unharmed (the sleep-command container ignores the
    dead process)."""

    def __init__(self, timeout_s: int, partial_stdout: bytes = b"", partial_stderr: bytes = b""):
        super().__init__(f"sandbox exec exceeded {timeout_s}s")
        self.timeout_s = timeout_s
        self.partial_stdout = partial_stdout
        self.partial_stderr = partial_stderr


class _SandboxRbacError(Exception):
    """The API server refused the exec (403): the sandbox exec Role is
    missing in this namespace.  Surfaced self-describingly — a "no" is a
    result (the k8s-mcp auth can-i lesson)."""


def _k8s_list_pods(label_selector: str, namespace: str) -> list[str]:
    """Names of Ready pods matching *label_selector* in *namespace*.

    REAL IMPLEMENTATION — the lazy kubernetes client, in-cluster config (the
    pod's own SA).  Returns [] when nothing matches.  Tests stub this seam.
    """
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config
    except ImportError as exc:  # pragma: no cover — guarded by packaging tests
        raise WorkbenchError(
            "the kubernetes client is not installed in this environment — "
            "sandbox_run requires the image build (pyproject dependencies "
            "include `kubernetes`); stdio-dev mode without it cannot reach "
            "an executor pool"
        ) from exc
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException as exc:
        raise WorkbenchError(
            "no in-cluster kubernetes config — sandbox_run runs only inside "
            "the cluster (the workbench pod's own service account); local "
            "stdio/dev mode has no executor pool to dispatch to"
        ) from exc
    v1 = k8s_client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)
    out: list[str] = []
    for item in pods.items:
        if item.status.phase != "Running":
            continue
        for cond in item.status.conditions or []:
            if cond.type == "Ready" and cond.status == "True":
                out.append(item.metadata.name)
                break
    return out


def _k8s_exec_stream(
    pod: str,
    namespace: str,
    container: str,
    argv: list[str],
    script: bytes,
    timeout_s: int,
) -> tuple[int | None, bytes, bytes]:
    """Exec *argv* in *pod*/*container*, write *script* to stdin, collect
    stdout/stderr.  Returns (exit_code_or_None, stdout, stderr).

    REAL IMPLEMENTATION — kubernetes.stream.stream with _preload_content
    False so we get the WSClient and can drive channels ourselves with
    deadlines (the preloaded mode returns everything-or-nothing and has no
    partial output on timeout).  Exit code comes from the exec protocol's
    error channel (status "Success" → 0, else the Status detail carries the
    code); when the protocol does not yield one (e.g. a server that closed
    the stream without a terminal Status), exit_code is null — recorded
    honestly rather than invented.  The deadline is enforced from connect
    through read; on expiry _SandboxExecTimeout carries the partial output.
    Tests stub this seam (module attribute), never the kubernetes internals.
    """
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config
        from kubernetes.stream import stream as k8s_stream
    except ImportError as exc:  # pragma: no cover — guarded by packaging tests
        raise WorkbenchError(
            "the kubernetes client is not installed in this environment — "
            "sandbox_run requires the image build (pyproject dependencies "
            "include `kubernetes`)"
        ) from exc
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException as exc:
        raise WorkbenchError(
            "no in-cluster kubernetes config — sandbox_run runs only inside "
            "the cluster"
        ) from exc
    v1 = k8s_client.CoreV1Api()
    deadline = time.monotonic() + timeout_s
    try:
        resp = k8s_stream(
            v1.connect_get_namespaced_pod_exec,
            pod,
            namespace,
            container=container,
            command=argv,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
    except k8s_client.rest.ApiException as exc:
        if exc.status == 403:
            raise _SandboxRbacError(
                f"the API server refused the exec into {namespace}/{pod} "
                f"(403): {exc.reason}"
            ) from exc
        raise
    if not hasattr(resp, "write_stdin"):
        raise WorkbenchError(
            "kubernetes exec stream did not return a channel client — "
            "check the kubernetes client version in the image"
        )
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    exit_code: int | None = None
    stdin_written = False

    def _timed_out() -> bool:
        return time.monotonic() >= deadline

    try:
        # Write the script to stdin, then CLOSE stdin so the remote python's
        # read of sys.stdin hits EOF and runs the program.  The channel
        # protocol has no half-close, so a bare 0x00 frame on channel 0 with
        # empty payload is the close signal the kubelet honors.
        resp.write_stdin(script)
        if script and not script.endswith(b"\n"):
            resp.write_stdin(b"\n")
        resp.write_channel(0, b"")
        stdin_written = True
        while True:
            if _timed_out():
                raise _SandboxExecTimeout(timeout_s, bytes(stdout_buf), bytes(stderr_buf))
            remaining = max(0.05, deadline - time.monotonic())
            got_out = resp.read_channel(1, timeout=min(remaining, 0.25))
            if got_out:
                stdout_buf.extend(got_out.encode("utf-8", "replace") if isinstance(got_out, str) else got_out)
            if _timed_out():
                raise _SandboxExecTimeout(timeout_s, bytes(stdout_buf), bytes(stderr_buf))
            remaining = max(0.05, deadline - time.monotonic())
            got_err = resp.read_channel(2, timeout=min(remaining, 0.25))
            if got_err:
                stderr_buf.extend(got_err.encode("utf-8", "replace") if isinstance(got_err, str) else got_err)
            # Terminal condition: the ERROR channel (3) carries the exec
            # Status.  A short poll — the streams above already waited.
            err_chan = resp.read_channel(3, timeout=0)
            if err_chan:
                exit_code = _sandbox_parse_status(err_chan)
                break
            if not resp.is_open() and not got_out and not got_err:
                # Channel closed with no terminal Status: collect what the
                # protocol cached and report exit_code honestly as None.
                try:
                    tail = resp.read_all()
                    if tail:
                        stdout_buf.extend(tail.encode("utf-8", "replace") if isinstance(tail, str) else tail)
                except Exception:  # best-effort tail drain
                    pass
                break
    finally:
        try:
            if not stdin_written:
                pass  # never wrote: nothing to close
            resp.close()
        except Exception:  # close is best-effort
            pass
    return exit_code, bytes(stdout_buf), bytes(stderr_buf)


def _sandbox_parse_status(status_text: str) -> int | None:
    """Parse the exec protocol's terminal Status (channel 3, JSON or YAML).

    {"status": "Success"} → 0; a Failure Status carries the exit code in
    details.causes[].message.  Anything unparseable → None (recorded
    honestly — never invented)."""
    try:
        import yaml as _yaml

        data = _yaml.safe_load(status_text)
    except Exception:  # malformed status → honest None
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") == "Success":
        return 0
    try:
        return int(data["details"]["causes"][0]["message"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


@mcp.tool(
    title="Sandbox Run",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
)
async def sandbox_run(workspace: str, path: str, argv: list[str] | None = None) -> str:
    """Run a python FILE from this workspace in a dedicated no-network,
    no-GPU, no-credentials executor pod (v1: single file + argv via stdin; no
    filesystem mounts, no network, stateless runs).  Enable via chart
    executors.enabled (the executor pool) TOGETHER WITH workbench.sandboxExec
    (the tool's env master switch, WORKBENCH_SANDBOX_EXEC=1).

    The file must live in the addressed workspace (same fence as
    read_file/write_file: traversal and symlink escapes refused, the
    WORKBENCH_MAX_FILE_BYTES cap applies).  It is executed as
    `python3 -I -- *argv` with the file's bytes on stdin — `-I` is isolated
    mode (no user site-packages, PYTHONPATH ignored — hardening).  argv is an
    optional list of string tokens (sys.argv[1:]).

    Returns {"ok": true, "exit_code": int-or-null (null when the exec
    protocol yields no terminal status — recorded honestly), "stdout",
    "stderr", "truncated", "pod", "elapsed_s"}.  stdout/stderr are capped at
    WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS (default 50000) with an explicit
    " ...[truncated N chars]" marker.  On timeout: a structured {"ok": false,
    "timeout": true, "timeout_s": N, ...partial output...} — the stateless
    executor pod is unharmed.  When every Ready executor already carries a
    run: {"ok": false, "busy": true, "running": N, "hint": "retry shortly"}
    WITHOUT executing (no queue).

    Isolation tier (honest): the container boundary + deny-all NetworkPolicy
    + no SA token + non-root + no GPU request + read-only rootfs — runc-tier
    isolation, NOT gVisor.  Runs are stateless and offline by construction.
    Every outcome is audit-logged (sandbox_run / sandbox_run_busy).
    """
    if not _sandbox_exec_enabled():
        _sandbox_refuse(
            "refused",
            "sandbox exec is not enabled (WORKBENCH_SANDBOX_EXEC) — see "
            "chart values executors.enabled",
        )
    if not isinstance(workspace, str) or not isinstance(path, str):
        _sandbox_refuse(
            "refused",
            "workspace and path must be strings (workspace name + "
            "workspace-relative file path)",
        )
    if argv is None:
        argv = []
    ws = _ws_dir(workspace)
    return await asyncio.to_thread(_sandbox_exec, workspace, ws, path, argv)


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
    # /health+/healthz only, so every /api/* route — including /api/run —
    # and /mcp all require the key) — built in here, not in main(), so ANY
    # consumer of _build_http_app gets the authenticated app. The console JS
    # sends the key via X-API-Key from its login prompt. The CONSOLE ITSELF
    # (GET / and /ui — a single self-contained, inert HTML file) is PUBLIC:
    # the unlock bar lives in that HTML and cannot load behind the key
    # (caught live in the 2026-09-13 deploy — the browser got the 401 JSON
    # instead of the console). Fleet pattern = the K8S-MCP console:
    # public-but-inert HTML, keyed data. With metrics opted in, /metrics
    # joins the public set (non-sensitive counters only — no workspace
    # names, no argv). Default (metrics off) passes public_paths=None →
    # exactly the pre-metrics behavior.
    public_paths = (*mcp_auth.DEFAULT_PUBLIC_PATHS, "/", "/ui")
    if _metrics_enabled():
        public_paths = (*public_paths, "/metrics")
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
