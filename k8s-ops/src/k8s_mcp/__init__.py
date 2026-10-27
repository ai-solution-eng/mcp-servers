"""k8s_mcp - read-only, namespace-scoped Kubernetes MCP server.

src-layout (2026-10 refactor): the server module lives at src/k8s_mcp/server.py
and is launched as "python -m k8s_mcp.server" (see the Dockerfile CMD and
pytest.ini pythonpath).

The fleet-shared module (mcp_auth, where present) and the vendored
mcp_fleet_common package stay at the REPO ROOT, outside this package, because
they are managed by the fleet hardlink mesh / fleet_common_sync.sh and must not
move.
"""
