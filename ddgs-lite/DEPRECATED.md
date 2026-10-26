# DDGS — DEPRECATED (fleet decision, 2026-09)

This chart is EFFECTIVELY DEPRECATED:

- Superseded by `searxng_mcp` (self-hosted SearXNG metasearch; the LLM
  gateway registry's `searxng` entry points at
  `searxng-mcp-service.searxng-mcp.svc.cluster.local:9090`).
- **No security work is spent here**: the fleet MCP network zone
  (NetworkPolicy / `authorizedClients`) is deliberately NOT added to this
  chart. Do not enable on new clusters; do not port new features.

Migration path: point any remaining consumer at searxng_mcp, then archive
this chart. See also: mcp-netzone workbench workspace (RUNBOOK.md,
DDGS-DEPRECATED.md) — exempt-by-deprecation, not exempt-by-design.
