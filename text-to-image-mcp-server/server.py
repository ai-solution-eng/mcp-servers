import os
import base64
import hashlib
from typing import Dict, Any
import asyncio

from dotenv import load_dotenv
from fastapi import Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from openai import OpenAI

_IMAGE_LOCK = asyncio.Lock()

load_dotenv()


def _allowed_hosts() -> list[str]:
    extra = os.getenv("MCP_ALLOWED_HOSTS", "")
    extra_hosts = [h.strip() for h in extra.split(",") if h.strip()]
    return [
        "0.0.0.0:*",
        "127.0.0.1:*",
        "localhost:*",
        "host.docker.internal:*",
        *extra_hosts,
    ]


mcp = FastMCP(
    os.environ.get("MCP_TEXT_TO_IMAGE_SERVER_NAME", "text2image-mcp"),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(),
    ),
)


def _get_text2image_client() -> OpenAI:
    base_url = (os.environ.get("TEXT2IMAGE_BASE_URL") or "").strip()
    api_key = (os.environ.get("TEXT2IMAGE_API_KEY") or "").strip()

    if not base_url:
        raise RuntimeError("TEXT2IMAGE_BASE_URL is not set")
    if not api_key:
        raise RuntimeError("TEXT2IMAGE_API_KEY is not set")

    return OpenAI(base_url=base_url, api_key=api_key)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _warmup():
    try:
        client = _get_text2image_client()
        model_name = (os.environ.get("TEXT2IMAGE_MODEL_NAME") or "").strip()

        if model_name:
            client.images.generate(
                model=model_name,
                prompt="warmup",
                n=1,
                size=os.environ.get("IMAGE_RESOLUTION", "512x512"),
                response_format="b64_json",
            )
            print("[text2image-mcp] Warm-up completed")
    except Exception as e:
        print(f"[text2image-mcp] Warm-up skipped: {e}")


@mcp.tool()
async def generate_image(
    prompt: str,
    size: str = os.environ.get("IMAGE_RESOLUTION", "512x512"),
    n: int = 1,
) -> Dict[str, Any]:
    async with _IMAGE_LOCK:
        client = _get_text2image_client()
        model_name = (os.environ.get("TEXT2IMAGE_MODEL_NAME") or "").strip()

        if not model_name:
            raise RuntimeError("TEXT2IMAGE_MODEL_NAME not set")

        resp = client.images.generate(
            model=model_name,
            prompt=prompt,
            n=n,
            size=size,
            response_format="b64_json",
        )

        b64_png = resp.data[0].b64_json
        img_bytes = base64.b64decode(b64_png)

        return {
            "mime_type": "image/png",
            "b64_png": b64_png,
            "sha256": _sha256_bytes(img_bytes),
            "model": model_name,
            "size": size,
            "n": n,
        }


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "status": "healthy",
            "server": os.environ.get("MCP_TEXT_TO_IMAGE_SERVER_NAME", "text2image-mcp"),
        }
    )


if __name__ == "__main__":
    import uvicorn

    _warmup()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    print(f"[text2image-mcp] Starting on {host}:{port}")

    app = mcp.streamable_http_app()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=True,
    )
