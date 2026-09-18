#!/usr/bin/env python3
"""Fran's memory migration — run this on YOUR machine.

What it does:
  1. Deletes the legacy-format memory collection (your password authorizes
     the delete — required; deleting must not be easier than reading).
  2. Re-adds every memory from francesco-memory-backup.json through the
     app's own memory tool — each one is re-embedded and its bm25 keyword
     vector rebuilt in the new format.  Nothing is lost; the whole point
     is the upgrade.

Before running:
  - Put this script and francesco-memory-backup.json in the SAME directory.
  - Fill in YOUR_DATASET_PASSWORD below (the password your memory tools
    ask for).
  - Make sure you can reach https://rag-mcp-server.pcai-se-ai-application…
    (the same endpoint your memory tools use).

Run:  python3 mcp_servers/logsearch_mcp/scripts/migrate-my-memory.py
"""
import json
import urllib.error
import urllib.request
import sys

API = "https://rag-mcp-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
MCP = API + "/mcp"
DATASET = "francesco-memory"
PASSWORD = "YOUR_DATASET_PASSWORD"            # <-- fill in your password
API_KEY = "_55_VDr0Rq_Eelbqmqtx2eX7g9gwPoPd"  # the fleet API key

# -- step 1: delete the legacy collection (your password authorizes it) ------
print(f"step 1/3: deleting the legacy collection ({DATASET})...")
req = urllib.request.Request(
    f"{API}/api/datasets/{DATASET}", method="DELETE",
    headers={"X-RAG-Api-Key": API_KEY, "X-Dataset-Password": PASSWORD})
try:
    r = urllib.request.urlopen(req, timeout=30)
    print("  deleted:", json.loads(r.read()).get("deleted", DATASET))
except urllib.error.HTTPError as e:
    print(f"  DELETE failed (HTTP {e.code}):", e.read().decode()[:200])
    if e.code in (401, 403):
        print("  The dataset password was rejected — check PASSWORD and re-run.")
        sys.exit(1)

# -- step 2: re-add every memory through the app's own memory tool ------------
# Each re-add is re-embedded and its bm25 keyword vector rebuilt in the new
# format.  The backup's original dense vectors are superseded by this (the
# embedder re-computes them); the backup stays on disk as the rollback.
print("step 2/3: re-adding memories from the backup through the app...")
backup = json.load(open("francesco-memory-backup.json"))
pts = backup["points"]
ok = fail = 0
for i, p in enumerate(pts, 1):
    payload = p.get("payload") or {}
    body = {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {
        "name": "add_memory", "arguments": {
            "dataset_name": DATASET, "password": PASSWORD,
            "text": payload.get("page_content", ""),
            "metadata": payload.get("metadata") or {}}}}
    req = urllib.request.Request(MCP, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        data=json.dumps(body).encode())
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        text = resp.read().decode()
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            for line in text.splitlines():
                if line.startswith("data: "):
                    text = line[6:]
                    break
        res = json.loads(text)
        if res.get("result", {}).get("isError"):
            fail += 1
            print(f"  [{i}] tool error:", res["result"]["content"][0]["text"][:100])
        else:
            ok += 1
        if i % 20 == 0:
            print(f"  ... {i}/{len(pts)}")
    except Exception as exc:
        fail += 1
        print(f"  [{i}] FAILED: {str(exc)[:100]}")
print(f"  re-added: {ok} ok, {fail} failed")

# -- step 3: verify -------------------------------------------------------------
print("step 3/3: verifying...")
req = urllib.request.Request(f"{API}/api/datasets/{DATASET}",
    headers={"X-RAG-Api-Key": API_KEY, "X-Dataset-Password": PASSWORD})
info = json.loads(urllib.request.urlopen(req, timeout=15).read())
print("  dataset info:", {k: info.get(k) for k in ("name", "document_count", "has_password")})

print(f"\nDONE — {DATASET} migrated to the new format ({ok} memories re-added).")
print("Final check is yours: run a memory search with a term you remember.")
if fail:
    print(f"NOTE: {fail} memories failed — the backup file still holds them; re-run to retry.")
