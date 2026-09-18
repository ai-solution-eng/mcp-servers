"""End-to-end smoke check: drives the real MCP protocol (in-memory client
AND the stateless streamable-http app) with faked k8s seams — no cluster,
no kubernetes package. Validates what unit tests can't: tool registration
over the SDK, JSON tool results over the wire, stateless /mcp handling,
/healthz wiring, and the audit file.

Usage:
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python tests/smoke_check.py
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APPLYGATE_ALLOWED_NAMESPACES"] = "team-a"
os.environ["APPLYGATE_AUDIT_FILE"] = os.path.join(tempfile.mkdtemp(prefix="applygate-smoke-"), "audit.jsonl")

import server


def fake_apply(ns, doc, dry_run):
    return {"metadata": {"name": doc["metadata"]["name"], "namespace": ns, "resourceVersion": "99"}}


def fake_status(ns, kind, name):
    return {"metadata": {"name": name}, "status": {"replicas": 2, "readyReplicas": 2}}


def fake_delete(ns, kind, name):
    return {"deleted": name}


server._ssa_apply = fake_apply
server._get_status = fake_status
server._delete = fake_delete

# The server module must be imported (and its k8s seams patched) before the
# client-side pieces are imported.
from mcp.client._memory import InMemoryTransport  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402

MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\ndata:\n  k: v\n"


async def in_memory():
    async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as s:
        await s.initialize()
        tools = await s.list_tools()
        print("[stdio] tools:", sorted(t.name for t in tools.tools))
        r = await s.call_tool("plan_apply", {"namespace": "team-a", "manifest": MANIFEST})
        print("[stdio] plan ok:", not r.is_error, "|", json.loads(r.content[0].text)["summary"])
        r = await s.call_tool("apply_manifest", {"namespace": "team-a", "manifest": MANIFEST})
        print("[stdio] apply without confirm refused:", json.loads(r.content[0].text)["refused"] is True)
        # D11: apply is bound to the planned bytes — plan first, carry the sha.
        planned = json.loads(
            (await s.call_tool("plan_apply", {"namespace": "team-a", "manifest": MANIFEST})).content[0].text
        )
        r = await s.call_tool(
            "apply_manifest",
            {
                "namespace": "team-a",
                "manifest": MANIFEST,
                "confirm_apply": True,
                "plan_sha256": planned["manifest_sha256"],
            },
        )
        print("[stdio] apply planned+confirmed ok:", json.loads(r.content[0].text)["ok"] is True)
        r = await s.call_tool("get_resource_status", {"namespace": "team-a", "kind": "Deployment", "name": "web"})
        print("[stdio] status excerpt:", json.loads(r.content[0].text)["status"]["summary"])


async def main():
    await in_memory()

    app = server._build_http_app()
    import uvicorn

    cfg = uvicorn.Config(app, host="127.0.0.1", port=9189, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    await asyncio.sleep(2)

    await asyncio.to_thread(_http_checks)

    srv.should_exit = True
    t.join(timeout=5)

    with open(os.environ["APPLYGATE_AUDIT_FILE"]) as fh:
        lines = [json.loads(line) for line in fh]
    print("[audit] lines:", [(e["tool"], e["outcome"]) for e in lines])
    assert any(e["outcome"] == "refused" for e in lines)
    assert sum(1 for e in lines if e["outcome"] == "applied") == 2
    print("SMOKE OK")


def _http_checks():
    """Blocking HTTP assertions against the uvicorn server thread.

    Runs via asyncio.to_thread so the event loop is never blocked
    (the urllib calls here are deliberately sequential and blocking)."""
    health = json.loads(urllib.request.urlopen("http://127.0.0.1:9189/healthz").read())
    print("[http] /healthz:", health)
    assert health["status"] == "ok" and health["namespaces_enabled"] is True

    # The read-only web console: HTML shell + the JSON API it drives.
    ui = urllib.request.urlopen("http://127.0.0.1:9189/").read().decode()
    print("[http] / renders:", len(ui), "bytes")
    assert "ApplyGate MCP" in ui and "Read-only console" in ui
    st = json.loads(urllib.request.urlopen("http://127.0.0.1:9189/api/status").read())
    print("[http] /api/status:", st["namespaces_enabled"], st["policy"]["allowed_namespaces"])
    assert st["namespaces_enabled"] is True
    plan = urllib.request.Request(
        "http://127.0.0.1:9189/api/plan",
        data=json.dumps({"namespace": "team-a", "manifest": MANIFEST}).encode(),
        headers={"Content-Type": "application/json"},
    )
    plan = json.loads(urllib.request.urlopen(plan).read())
    print("[http] /api/plan dry-run ok:", plan["ok"] is True and plan["dry_run"] is True)
    assert plan["dry_run"] is True and plan["summary"]["ok"] == 1
    audit = json.loads(urllib.request.urlopen("http://127.0.0.1:9189/api/audit?lines=5").read())
    print("[http] /api/audit:", audit["n_total"], "line(s)")
    assert audit["n_shown"] >= 1
    try:
        urllib.request.urlopen("http://127.0.0.1:9189/api/audit?file=/etc/passwd")
        raise AssertionError("path traversal must be refused")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        print("[http] /api/audit path traversal refused: 400")

    def rpc(payload):
        req = urllib.request.Request(
            "http://127.0.0.1:9189/mcp",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    listing = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = sorted(t["name"] for t in listing["result"]["tools"])
    print("[http] tools/list:", names)
    planned = rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "plan_apply", "arguments": {"namespace": "team-a", "manifest": MANIFEST}},
        }
    )["result"]["content"][0]["text"]
    planned = json.loads(planned)
    print("[http] plan manifest_sha256:", planned["manifest_sha256"][:16], "…")
    call = rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "apply_manifest",
                "arguments": {
                    "namespace": "team-a",
                    "manifest": MANIFEST,
                    "confirm_apply": True,
                    "plan_sha256": planned["manifest_sha256"],
                },
            },
        }
    )
    print("[http] tools/call apply ok:", json.loads(call["result"]["content"][0]["text"])["ok"] is True)


asyncio.run(main())
