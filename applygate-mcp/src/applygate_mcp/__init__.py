"""applygate_mcp — the fleet's governed Kubernetes write-path MCP server.

Package layout (fleet src convention):

  * ``applygate_mcp.server`` — the MCP server (tools, guardrail policy,
    plan/apply/delete fences, hash-chained audit trail) and its console
    script entry point (``applygate_mcp.server:main``).
  * ``applygate_mcp.webui`` — the read-only web console + JSON API.

``mcp_auth`` stays a TOP-LEVEL module at the repo root (fleet hardlink to
pcai_utils/mcp_auth.py) and ships as a top-level py-module; it is imported
as ``import mcp_auth``, never relatively.
"""
