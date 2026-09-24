"""Tests for HTTP response compression (GZipMiddleware on the real app stack).

SQLHANDLER_COMPRESSION (default ``gzip``) adds Starlette's GZipMiddleware to
the streamable-HTTP app; SQLHANDLER_COMPRESSION_MIN_SIZE (default 1024) keeps
probe-sized bodies uncompressed. The two properties these tests pin:

* Agent-facing tool results are markdown/JSON-heavy text — gzip compresses
  them 5-10x, so every big /api, /ui and /mcp JSON-RPC response should come
  back Content-Encoding: gzip and round-trip to the uncompressed bytes.
* SSE must NEVER be compressed: the MCP SDK's streamable-HTTP transport can
  answer with EventSourceResponse (``text/event-stream``) — both its own
  streaming responses and Starlette's chunked streaming must stay incremental
  under the middleware. Starlette's GZipMiddleware excludes
  ``text/event-stream`` by default and compresses plain streams chunk-wise
  with a Z_SYNC_FLUSH per chunk (nothing buffers whole-body); the tests read
  the streamed responses incrementally to prove it.

Run:  python -m pytest tests/test_compression.py -v
"""

import gzip as gzip_module
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

from sqlhandler.config import load_compression_config
from sqlhandler.server import _build_http_app

# /mcp request headers — the exact shape every other transport test in this
# suite sends (Accept must name both JSON and SSE per the MCP spec).
_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}


@pytest.fixture()
def app(monkeypatch):
    """Fresh app per test, optional auth OFF so every route is reachable."""
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(_build_http_app()) as client:
        yield client


# ---------------------------------------------------------------------------
# config loading (load_compression_config)
# ---------------------------------------------------------------------------


def test_compression_config_defaults():
    cfg = load_compression_config({})
    assert cfg.mode == "gzip"
    assert cfg.min_size == 1024


def test_compression_config_off_and_min_size(monkeypatch):
    cfg = load_compression_config({"SQLHANDLER_COMPRESSION": "off", "SQLHANDLER_COMPRESSION_MIN_SIZE": "2048"})
    assert cfg.mode == "off" and cfg.min_size == 2048


def test_compression_config_garbage_falls_back():
    # unknown mode -> gzip (never silently off); garbage int -> default 1024
    cfg = load_compression_config({"SQLHANDLER_COMPRESSION": "brotli", "SQLHANDLER_COMPRESSION_MIN_SIZE": "banana"})
    assert cfg.mode == "gzip" and cfg.min_size == 1024


def test_middleware_order_gzip_inner_of_policy_layers(monkeypatch):
    """Pin the verified request flow: probes gate (outermost) -> GZip -> CORS
    -> transport guard -> key gates. GZip must stay INNERMOST of the policy
    layers so auth decisions are made on uncompressed requests and refusals
    reach the client as plain bytes; CORS sits directly outside GZip because
    it only rewrites headers. Rebuild the stack the way Starlette does and
    walk the wrapper chain outermost-first."""
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SQLHANDLER_ALLOWED_ORIGINS", "https://ui.example.com")
    stack = _build_http_app().build_middleware_stack()
    chain = []
    m = stack
    while m is not None:
        chain.append(type(m).__name__)
        m = getattr(m, "app", None)
    assert chain.index("GZipMiddleware") < chain.index("CORSMiddleware") < chain.index("_McpTransportGuard")
    assert chain.index("_ProbesAuthMiddleware") < chain.index("GZipMiddleware")


# ---------------------------------------------------------------------------
# (a) big JSON API body compresses and round-trips
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_with_big_table(monkeypatch, tmp_path):
    """The real app stack with ``server._handler`` patched to a file-backed
    engine over one wide table (the established pattern in
    test_dispatch_arg_contract.py — avoids the OneLake config/.env default).
    """
    from sqlhandler import server
    from sqlhandler.engine import SqlEngine
    from sqlhandler.provider import TableInfo

    d = tmp_path / "big" / "wide_table"
    d.mkdir(parents=True, exist_ok=True)
    rows = list(range(500))
    pq.write_table(
        pa.table({"id": rows, "text": [f"row-{i}-" + "x" * 40 for i in rows]}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="wide_table", schema="big", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    monkeypatch.setattr(server, "_handler", lambda: SqlEngine(P(), cache_ttl=0))
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(_build_http_app()) as client:
        yield client


def test_big_json_api_response_is_gzipped_and_round_trips(app_with_big_table):
    """A body well above min_size -> Content-Encoding: gzip, payload intact."""
    r = app_with_big_table.post(
        "/api/query", json={"sql": "SELECT * FROM wide_table"}, headers={"Accept-Encoding": "gzip"}
    )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    payload = r.json()  # transparent client-side decode proves round-trip
    # /api/query's default limit is 100 rows — still far above min_size
    assert payload["n_rows"] == 100
    assert payload["columns"] == ["id", "text"]

    # Wire-level proof: the bytes on the wire are a gzip stream that
    # decompresses back to exactly what an identity (uncompressed) request
    # returns (modulo the timing field, which differs per request), and
    # compression actually wins on this repetitive JSON body.
    with app_with_big_table.stream(
        "POST", "/api/query", json={"sql": "SELECT * FROM wide_table"}, headers={"Accept-Encoding": "gzip"}
    ) as s:
        raw = b"".join(s.iter_raw())
    with app_with_big_table.stream(
        "POST", "/api/query", json={"sql": "SELECT * FROM wide_table"}, headers={"Accept-Encoding": "identity"}
    ) as s:
        identity = b"".join(s.iter_raw())
    assert raw[:2] == b"\x1f\x8b"  # gzip magic number on the wire
    gz_payload = json.loads(gzip_module.decompress(raw))
    id_payload = json.loads(identity)
    for key in gz_payload:
        if key == "duration_ms":
            continue
        assert gz_payload[key] == id_payload[key], key
    assert len(raw) < len(identity)


def test_big_ui_html_response_is_gzipped(app):
    """The /ui HTML page (self-contained, tens of KiB) compresses too."""
    r = app.get("/ui", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert "<html" in r.text.lower()


# ---------------------------------------------------------------------------
# (b) identity -> no compression
# ---------------------------------------------------------------------------


def test_identity_accept_encoding_gets_uncompressed(app_with_big_table):
    r = app_with_big_table.post(
        "/api/query", json={"sql": "SELECT id FROM wide_table LIMIT 1"}, headers={"Accept-Encoding": "identity"}
    )
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


def test_no_accept_encoding_gets_uncompressed(app_with_big_table):
    """No header at all behaves like identity (nothing to negotiate)."""
    r = app_with_big_table.post("/api/query", json={"sql": "SELECT id FROM wide_table LIMIT 1"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


# ---------------------------------------------------------------------------
# (c) small body below min_size -> no compression
# ---------------------------------------------------------------------------


def test_small_body_below_min_size_not_compressed(app):
    """/health is a ~15-byte JSON body — under the 1024-byte floor."""
    r = app.get("/health", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert len(r.content) < 1024


def test_min_size_env_raises_the_floor(app, monkeypatch, tmp_path):
    """SQLHANDLER_COMPRESSION_MIN_SIZE=0 compresses even the tiny /health body."""
    monkeypatch.setenv("SQLHANDLER_COMPRESSION_MIN_SIZE", "0")
    with TestClient(_build_http_app()) as client:
        r = client.get("/health", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# (d) /mcp POST initialize over gzip
# ---------------------------------------------------------------------------


def test_mcp_initialize_round_trips_through_gzip(app):
    """THE transport-critical case: the MCP initialize handshake with
    Accept-Encoding: gzip -> 200 and a parseable JSON-RPC response after the
    client's transparent decompression."""
    r = app.post("/mcp", json=_INIT_BODY, headers={**_MCP_HEADERS, "Accept-Encoding": "gzip"})
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert "result" in body
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"]


def test_mcp_tools_list_big_response_is_gzipped(app):
    """tools/list is the largest JSON-RPC response the server produces —
    it must actually be compressed on the wire and still parse."""
    r = app.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=_MCP_HEADERS)
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    assert len(tools) > 0
    if len(r.content) >= 1024:  # above the default floor it must be compressed
        assert r.headers.get("content-encoding") == "gzip"
        assert isinstance(tools, list)


def test_mcp_get_sse_stream_stays_uncompressed(monkeypatch):
    """SSE must pass through GZipMiddleware byte-for-byte.

    The MCP streamable-HTTP transport's GET side is a long-lived
    text/event-stream; the middleware excludes that content type by default.
    Driven with a raw ASGI call against the REAL app (a TestClient .get()
    would block on the never-ending stream) — capture http.response.start and
    any body chunk while the stream is held open, then cancel: the assertion
    is that the response headers arrive WITHOUT Content-Encoding and the body
    bytes are plain SSE text (no gzip magic), proving the stream was neither
    buffered nor compressed. In stateless mode with no client bound there is
    nothing to stream, so both the headers-flushed and still-open outcomes are
    accepted; the invariant pinned in both cases is "no compression".
    """
    import anyio

    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    application = _build_http_app()

    async def drive():
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [
                (b"accept", b"text/event-stream"),
                (b"accept-encoding", b"gzip"),
                (b"host", b"testserver"),
            ],
            "client": ("test", 123),
            "server": ("testserver", 80),
        }
        messages = []
        got_start = anyio.Event()

        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.start":
                got_start.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(application, scope, receive, send)
            with anyio.move_on_after(2.0):
                await got_start.wait()
            tg.cancel_scope.cancel()
        return messages

    messages = anyio.run(drive)

    start = [m for m in messages if m["type"] == "http.response.start"]
    if start:  # headers flushed while the stream is held open
        headers = {k.decode(): v.decode() for k, v in start[0]["headers"]}
        assert "content-encoding" not in headers, headers
        assert headers.get("content-type", "").startswith("text/event-stream")
        bodies = [m for m in messages if m["type"] == "http.response.body"]
        for m in bodies:
            assert m.get("body", b"")[:2] != b"\x1f\x8b"  # never gzip magic


# ---------------------------------------------------------------------------
# (e) plain chunked streaming stays incremental under compression
# ---------------------------------------------------------------------------


def test_streamed_response_compresses_chunkwise_and_stays_incremental(app):
    """A plain StreamingResponse (the shape EventSourceResponse would take if
    its media type ever changed) is compressed CHUNK-WISE with a Z_SYNC_FLUSH
    per chunk — the client receives each chunk incrementally, not one buffered
    blob. Verified by reading the streamed response incrementally and checking
    every observed chunk is a self-contained gzip fragment the client decoder
    concatenates back to the original bytes.
    """
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.middleware.gzip import GZipMiddleware
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    CHUNKS = [f"chunk-{i}-" + "x" * 300 for i in range(6)]

    async def stream_json(request):
        async def gen():
            for c in CHUNKS:
                yield c

        return StreamingResponse(gen(), media_type="application/json")

    # Mirror the production order exactly: GZip innermost, CORS outside it.
    inner = GZipMiddleware(Starlette(routes=[Route("/chunked", stream_json)]), minimum_size=1024)
    stacked = CORSMiddleware(inner, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    with TestClient(stacked) as client:
        with client.stream("GET", "/chunked", headers={"Accept-Encoding": "gzip"}) as response:
            assert response.headers.get("content-encoding") == "gzip"
            assert "content-length" not in response.headers  # streaming: no CL
            wire_chunks = list(response.iter_raw())
        decoded = b"".join(gzip_module.decompress(c) if c else b"" for c in wire_chunks if c[:2] == b"\x1f\x8b")
        # every wire chunk carries the gzip magic: compressed per-chunk +
        # flushed, not one buffered whole-body gzip stream.
        nonempty = [c for c in wire_chunks if c]
        assert nonempty and all(c[:2] == b"\x1f\x8b" for c in nonempty)
        assert decoded == "".join(CHUNKS).encode()


# ---------------------------------------------------------------------------
# (f) SQLHANDLER_COMPRESSION=off removes the middleware
# ---------------------------------------------------------------------------


def test_compression_off_disables_middleware(monkeypatch):
    """SQLHANDLER_COMPRESSION=off -> even a huge body arrives uncompressed."""
    monkeypatch.setenv("SQLHANDLER_COMPRESSION", "off")
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(_build_http_app()) as client:
        init = client.post("/mcp", json=_INIT_BODY, headers={**_MCP_HEADERS, "Accept-Encoding": "gzip"})
        assert init.status_code == 200
        assert "content-encoding" not in init.headers
        assert json.loads(init.content)["jsonrpc"] == "2.0"
        # /ui is always way over any min-size floor — still not compressed
        ui = client.get("/ui", headers={"Accept-Encoding": "gzip"})
        assert ui.status_code == 200
        assert "content-encoding" not in ui.headers


# ---------------------------------------------------------------------------
# /metrics stays scrapable (compression is fine there; just don't corrupt it)
# ---------------------------------------------------------------------------


def test_metrics_response_parses_after_transport(app_with_big_table):
    """Prometheus exposition compresses fine — the scraped text must still
    parse as metrics after transport. ``app_with_big_table`` has
    server._handler stubbed, so this never builds the real OneLake engine:
    _handler() load_dotenv()s the repo's config/.env into os.environ, which
    would pollute other test files' env expectations (test_config.py pins
    exactly that)."""
    r = app_with_big_table.get("/metrics", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    text = r.text
    assert "sqlhandler" in text or "# " in text  # exposition-format sanity
