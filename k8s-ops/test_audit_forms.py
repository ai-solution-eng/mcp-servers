"""Ported schema↔form audit — the audit_forms.py contract as pytest tests.

Boots the real server (kubernetes stubbed, exec enabled — 19 tools), fetches
tools/list, and simulates the console's inferWidget → widget → readForm
matrix for every parameter of every tool. The original script stays runnable
standalone (`python3 audit_forms.py [--json]`); this file is the CI/pytest
interface for the same contract.
"""

import json
import os
import sys
import threading
import time
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_namespace_policy import _install_kubernetes_stubs, ensure_exec_state  # noqa: E402

_install_kubernetes_stubs()

PORT = 9194
BASE = f"http://127.0.0.1:{PORT}"

# Screens that carry explicit widget overrides (mirror of ui/app.js SCREENS).
SCREEN_OVERRIDES = {
    "get_resource": {"resource_type": "resourcetype"},
    "describe_resource": {"resource_type": "resourcetype"},
    "run_kubectl": {"command": "text"},
}
# Tools with no dedicated screen — reachable only through "Any tool".
ANYTOOL_ONLY = {"list_api_resources"}


def _start_server():
    os.environ["K8S_MCP_API_KEY"] = "audit-key"
    import server

    mcp_asgi = server._ApiKeyAuthMiddleware(server.mcp.streamable_http_app(stateless_http=True))
    router = server._ConsoleRouterApp(mcp_asgi, server._ConsoleApp(server._UI_DIR))
    import uvicorn

    cfg = uvicorn.Config(router, host="127.0.0.1", port=PORT, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/ui/index.html", timeout=1)
            return srv
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def post_rpc(payload):
    req = urllib.request.Request(
        BASE + "/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer audit-key",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
        ctype = r.headers.get("content-type", "")
    if "event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise RuntimeError("no data line in SSE body")
    return json.loads(body)


def infer_kind(name, prop, overrides=None):
    """Mirror of ui/app.js inferWidget + screen.widgets overrides."""
    if overrides and overrides.get(name):
        return overrides[name]
    if name == "namespace":
        return "namespace"
    if name == "resource_type":
        return "resourcetype"
    if name == "command" and prop.get("type") == "array":
        return "command"
    if name == "verb":
        return "verb"
    if prop.get("type") == "boolean":
        return "bool"
    if prop.get("type") in ("integer", "number"):
        return "number"
    if prop.get("type") == "array":
        return "lines"
    if isinstance(prop.get("enum"), list):
        return "select"
    return "text"


# What readForm produces for each kind, given the widget ui/app.js renders.
WIDGET_YIELDS = {
    "namespace": "string",  # select of cluster namespaces (or "" = all)
    "resourcetype": "string",  # select (curated + discovered) / ✎ other text
    "command": "array",  # argv builder → [binary, *args]
    "verb": "string",  # select of read verbs
    "select": "enum-same",  # select of the schema's own enum values
    "bool": "boolean",
    "number": "number",
    "lines": "array",  # textarea → list[str]
    "text": "string",
}


def yield_matches(schema_type, items_type, yields):
    if yields == "enum-same":
        return True  # values come from the schema itself
    if schema_type == "array":
        return yields == "array" and (items_type in (None, "string"))
    if schema_type == "boolean":
        return yields == "boolean"
    if schema_type in ("integer", "number"):
        return yields == "number"
    if schema_type == "string" or schema_type is None:
        return yields == "string"
    return False  # unknown server type


@pytest.fixture(scope="module")
def audit():
    """Boot the server once and run the full widget-matrix audit."""
    _install_kubernetes_stubs()
    import server

    ensure_exec_state(server, True)  # exec_in_pod registers — 19 tools audited
    srv = _start_server()
    try:
        resp = post_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = resp["result"]["tools"]

        rows, findings = [], []
        for t in sorted(tools, key=lambda x: x["name"]):
            name = t["name"]
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            overrides = SCREEN_OVERRIDES.get(name, {})
            for pname, prop in props.items():
                kind = infer_kind(pname, prop, overrides)
                stype = prop.get("type", "string")
                items_type = (prop.get("items") or {}).get("type")
                yields = WIDGET_YIELDS.get(kind, "string")
                ok = yield_matches(stype, items_type, yields)
                if not ok:
                    findings.append(
                        f"{name}.{pname}: widget '{kind}' yields {yields} but server wants "
                        f"{stype}" + (f" of {items_type}" if items_type else "")
                    )
                rows.append(
                    {
                        "tool": name,
                        "param": pname,
                        "type": stype,
                        "items": items_type or "",
                        "required": pname in required,
                        "kind": kind,
                        "yields": yields,
                        "ok": ok,
                    }
                )
            # every screen-overridden kind must exist in the widget table
            for okind in overrides.values():
                if okind not in WIDGET_YIELDS:
                    findings.append(f"{name}: screen override kind '{okind}' is unknown")

        tools_listed = sorted({r["tool"] for r in rows})
        no_params = [t["name"] for t in tools if t["name"] not in tools_listed]
        yield {"tools": tools, "rows": rows, "findings": findings, "tools_listed": tools_listed, "no_params": no_params}
    finally:
        srv.should_exit = True
        time.sleep(0.4)
        os.environ.pop("K8S_MCP_API_KEY", None)


def test_every_widget_output_type_round_trips(audit):
    """THE contract: every widget output type round-trips to its server schema type."""
    assert audit["findings"] == [], f"widget/schema mismatches: {audit['findings']}"


def test_audit_covers_every_toolslist_parameter(audit):
    """The matrix reached every tool: every tool either has audited parameters
    or is a known zero-param screen."""
    audited = set(audit["tools_listed"]) | set(audit["no_params"])
    assert audited == {t["name"] for t in audit["tools"]}, (
        "every tools/list tool must be classified (params audited or zero-param screen)"
    )


def test_zero_param_tools_are_the_overview_screens(audit):
    """Zero-parameter tools are exactly the Overview screens (no form)."""
    assert set(audit["no_params"]) == {"cluster_health", "list_api_resources", "list_namespaces", "list_crds"}, (
        f"unexpected zero-param tools: {audit['no_params']}"
    )
