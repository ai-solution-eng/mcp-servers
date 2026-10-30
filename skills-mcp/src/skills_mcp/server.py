"""Skills MCP Server — the team's markdown skills, served over MCP 2.0.

A *skill* is a directory containing a `SKILL.md`: a name, a one-line
description, and instructions. This server makes that folder discoverable by
any agent:

  list_skills       the catalogue — name + description for every skill (~2 KB)
  get_skill         one skill's full markdown, plus the list of its files
  get_skill_file    one file inside a skill (scripts, templates, assets)

THREE TOOLS, FOREVER. Skill names are arguments, never tools. The PCAI LLM
gateway grants MCP tools per key, default-deny, with no wildcard — one tool
per skill would mean every new skill is invisible until a human adds a grant
row. The catalogue is data, not tools.

This is the step-1 skeleton: transport, auth and health only. The loader
arrives in step 2, the three tools in step 3.
"""

import argparse
import sys

from mcp.server import MCPServer

# The fleet's shared API-key middleware — a hardlinked module, identical (same
# sha256) in every fleet server. Imported as a TOP-LEVEL module: it ships in
# the wheel via pyproject's py-modules, not inside the package. Do not edit it.
import mcp_auth

SERVER_NAME = "skills-mcp"

# Comma-separated keys; the fleet-universal MCP_API_KEYS is honoured too (the
# sets are unioned, compared in constant time). No keys configured = FAIL OPEN,
# which is fine locally and unacceptable in the cluster — so the chart always
# wires a Secret.
API_KEYS_ENV = "SKILLS_MCP_API_KEYS"
AUTH_ENV_NAMES = (mcp_auth.UNIVERSAL_API_KEYS_ENV, API_KEYS_ENV)

mcp = MCPServer(SERVER_NAME)

# Host-header (DNS-rebinding) protection. Unset MCP_HOSTNAME / MCP_EXTRA_ALLOWED_HOSTS
# (local dev) => the SDK's loopback-only default applies. The chart sets them
# when the Istio gateway fronts us; without the pin, every gateway request
# answers 421 "Invalid Host header".
_mcp_transport_security = mcp_auth.transport_security_from_env()


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL: the MCP session manager needs its lifespan to run. Mounting the
    routes without ``lifespan=http_app.router.lifespan_context`` yields a
    server whose /healthz probes stay GREEN while EVERY /mcp request 500s with
    "Task group is not initialized". It is the most expensive trap in the fleet
    template — do not "simplify" it away.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({"status": "ok", "server": SERVER_NAME})

    # json_response=True keeps plain-HTTP clients on single JSON responses;
    # stateless_http=True drops session affinity (any replica serves anything).
    http_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_mcp_transport_security,
    )
    routes: list = [
        Route("/health", health),
        Route("/healthz", health),
    ]
    routes.extend(http_app.routes)
    # API-key auth on /mcp only — the probes must stay public for kubelet.
    return mcp_auth.ApiKeyAuthMiddleware(
        Starlette(routes=routes, lifespan=http_app.router.lifespan_context),
        env_names=AUTH_ENV_NAMES,
        protected=lambda p: p.startswith("/mcp"),
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Skills MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    mcp_auth.warn_if_open(SERVER_NAME, AUTH_ENV_NAMES)

    app = _build_http_app()
    print(f"Skills MCP streamable-http endpoint: http://{args.host}:{args.port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
