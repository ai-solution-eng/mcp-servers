class MCPServer:
    """Minimal stand-in for the MCP SDK v2 server (smoke tests only):
    serves 404 on /mcp so the HTTP surfaces around it can be exercised
    with a real uvicorn."""

    def __init__(self, name, *args, **kwargs):
        self.name = name
        self.tools = []

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools.append(fn)
            return fn
        return decorator

    def streamable_http_app(self, **kwargs):
        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            body = b'{"error": "mcp sdk stub (smoke test only)"}'
            await send({"type": "http.response.start", "status": 501,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
        return app
