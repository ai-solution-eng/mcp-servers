import asyncio
from typing import Dict, Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import pdb
import json
from typing import Any, Dict


def mcp_result_to_dict(res: Any) -> Dict[str, Any]:
    """
    Normalize MCP call_tool result to a Python dict.
    Handles:
      - res.structured (newer SDK / structured output)
      - res.content as TextContent blocks containing JSON
    """

    # 1) Best case: structured
    if hasattr(res, "structured") and res.structured is not None:
        if isinstance(res.structured, dict):
            return res.structured
        # sometimes it's already a JSON string
        if isinstance(res.structured, str):
            return json.loads(res.structured)

    # 2) Content blocks (common): TextContent(text="...json...")
    if hasattr(res, "content") and res.content:
        first = res.content[0]
        # TextContent has `.text`
        if hasattr(first, "text") and isinstance(first.text, str):
            txt = first.text.strip()
            # strip code fences just in case
            txt = txt.replace("```json", "").replace("```", "").strip()
            return json.loads(txt)

    raise RuntimeError(f"Unexpected MCP result format: {res!r}")


async def _call_tool(server_url: str, args: Dict[str, Any]) -> Dict[str, Any]:
    async with streamablehttp_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            res = await session.call_tool("generate_image", args)

            return mcp_result_to_dict(res)


def generate_image_via_mcp(server_url: str, **kwargs) -> Dict[str, Any]:
    return asyncio.run(_call_tool(server_url, kwargs))
