from fastmcp import FastMCP

_mcp = FastMCP("statistical-visualization-mcp")


def get_mcp():
    return _mcp


class _McpProxy:
    """Proxy that defers mcp lookup until decorator is called."""
    def tool(self):
        return get_mcp().tool

    def run(self, **kwargs):
        return get_mcp().run(**kwargs)


mcp = _McpProxy()