import os
import base64
from datetime import datetime
from pathlib import Path

from langchain_core.tools import tool

from src.mcp_text2image_client import generate_image_via_mcp
from dotenv import load_dotenv

load_dotenv()

VERBOSE = os.environ.get("VERBOSE", False)
OUTPUT_DIR = Path(os.environ.get("GENERATED_IMAGE_DEST", "output_images"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def save_b64_png(b64_png: str, out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    img_bytes = base64.b64decode(b64_png)

    with open(out_path, "wb") as f:
        f.write(img_bytes)

    if not out_path.exists():
        raise RuntimeError(f"Image was not saved: {out_path}")

    if out_path.stat().st_size == 0:
        raise RuntimeError(f"Saved image is empty: {out_path}")
    if VERBOSE:
        print(
            f"[tools_image] Saved image to {out_path} ({out_path.stat().st_size} bytes)"
        )
    return str(out_path)


@tool
def generate_mascot_image(prompt: str) -> str:
    """
    Generate a mascot image from a text prompt using the image MCP server.
    Returns the local PNG file path.
    """

    server_url = os.environ["MCP_TEXT2IMAGE_URL"]
    image_resolution = os.getenv("IMAGE_RESOLUTION", "768x768")

    if VERBOSE:
        print(f"[tools_image] MCP server URL: {server_url}")
        print(f"[tools_image] Output dir: {OUTPUT_DIR}")
        print(f"[tools_image] Requested size: {image_resolution}")
        print(f"[tools_image] Prompt: {prompt}")

    payload = generate_image_via_mcp(
        server_url=server_url, prompt=prompt, size=image_resolution, n=1
    )

    b64_png = payload.get("b64_png")
    if not b64_png:
        raise RuntimeError(f"Image MCP did not return b64_png: {payload}")
    if VERBOSE:
        print(f"[tools_image] MCP payload keys: {list(payload.keys())}")

    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_mascot.png"
    out_path = OUTPUT_DIR / filename
    return save_b64_png(b64_png, out_path)
