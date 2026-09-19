"""Data acquisition for the statistical-visualization MCP server (v0.1).

Three sources, in preference order:
1. ``sql``  → run a read-only SELECT through the sqlhandler MCP server
   (stateless MCP 2.0 streamable-http, httpx2 client, auth via bearer key
   when configured). The model never carries rows through the tool call.
2. ``data_url`` → fetch JSON records or CSV from an https:// URL, with an
   SSRF guard (no loopback/RFC1918/metadata targets).
3. ``data`` → inline records (tiny datasets only; warned about above
   MAX_INLINE_ROWS).

Every path returns a plain list[dict] suitable for ``utils.data._df_from_records``.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urlparse

import httpx2

from schema import MAX_FETCH_ROWS, MAX_INLINE_ROWS

# --- configuration -------------------------------------------------------------

SQLHANDLER_MCP_URL = os.environ.get(
    "SQLHANDLER_MCP_URL",
    "http://sqlhandler.sqlhandler.svc.cluster.local:9097/mcp",
)
SQL_MCP_TIMEOUT_S = float(os.environ.get("SQL_MCP_TIMEOUT_S", "120"))
FETCH_TIMEOUT_S = float(os.environ.get("SEABORN_FETCH_TIMEOUT_S", "30"))
API_KEY_ENV = "SEABORN_API_KEYS"


def _bearer_headers() -> dict[str, str]:
    """Bearer token for the sqlhandler MCP call (fleet mcp_auth convention:
    comma-separated keys in the universal env; first one is used)."""
    raw = os.environ.get(API_KEY_ENV, "") or os.environ.get(
        "MCP_API_KEYS", ""
    )
    for candidate in raw.split(","):
        candidate = candidate.strip()
        if candidate:
            return {"Authorization": f"Bearer {candidate}"}
    return {}


# --- sql path ------------------------------------------------------------------


def _extract_sql_rows(result: Any) -> list[dict[str, Any]]:
    """Pull rows out of sqlhandler's run_sql response shapes.

    run_sql (json output) → {"columns": [...], "rows": [...]}.
    Some renderings wrap the payload in {"content": [{"text": json}]}.
    """
    # unwrap MCP content envelope
    if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
        texts = [c.get("text", "") for c in result["content"] if isinstance(c, dict)]
        if texts:
            try:
                result = json.loads(texts[0])
            except (json.JSONDecodeError, TypeError):
                pass

    if isinstance(result, dict):
        rows = result.get("rows")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        # {"data": {"columns": [...], "rows": [...]}} variant
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return [r for r in data["rows"] if isinstance(r, dict)]
    raise ValueError(
        f"Could not parse rows from sqlhandler response (got {type(result).__name__}); "
        "expected run_sql JSON output with a 'rows' list."
    )


async def _rows_from_sql(sql: str) -> tuple[list[dict[str, Any]], str]:
    """Execute a read-only SELECT via the sqlhandler MCP server (MCP 2.0,
    stateless streamable-http) and return (rows, source_description)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(
            timeout=httpx2.Timeout(SQL_MCP_TIMEOUT_S, connect=10.0),
            follow_redirects=False,
            headers=_bearer_headers(),
        ) as http,
        streamable_http_client(SQLHANDLER_MCP_URL, http_client=http) as (read, write),
    ):
        session = ClientSession(read, write, read_timeout_seconds=SQL_MCP_TIMEOUT_S)
        async with session:
            await session.initialize()
            result = await session.call_tool(
                "run_sql",
                {"sql": sql, "output_format": "json", "limit": MAX_FETCH_ROWS},
            )
            if getattr(result, "isError", False):
                detail = ""
                content = getattr(result, "content", None) or []
                for c in content:
                    text = getattr(c, "text", "")
                    if text:
                        detail = text[:500]
                        break
                raise ValueError(f"sqlhandler run_sql failed: {detail}")
            rows = _extract_sql_rows(_content_to_plain(result))
            return rows, f"sqlhandler ({len(rows)} rows)"


def _content_to_plain(result: Any) -> Any:
    """Best-effort conversion of an MCP CallToolResult into plain Python."""
    if isinstance(result, dict):
        return result
    content = getattr(result, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    if hasattr(result, "structuredContent"):
        return result.structuredContent
    return result


# --- url path ------------------------------------------------------------------


def _assert_public_https(url: str) -> None:
    """SSRF guard: https only, no loopback/RFC1918/link-local/metadata hosts."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("data_url must use https://")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("data_url has no host")
    bad_names = {"localhost", "metadata", "metadata.google.internal"}
    if host in bad_names or host.endswith(".svc") or host.endswith(".internal"):
        raise ValueError(f"refusing internal/cluster host: {host}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve data_url host: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            raise ValueError(f"refusing non-public address {addr} for host {host}")


def _rows_from_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    return [{k: v for k, v in row.items() if k is not None} for row in reader]


async def _rows_from_url(url: str) -> tuple[list[dict[str, Any]], str]:
    """Fetch JSON records (list or {"rows": [...]}/{"data": [...]}) or CSV."""
    _assert_public_https(url)
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(FETCH_TIMEOUT_S, connect=10.0),
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        body = resp.text
    rows: list[dict[str, Any]] | None = None
    if "csv" in ctype or (not body.lstrip().startswith(("[", "{")) and "," in body.splitlines()[0] if body else False):
        rows = _rows_from_csv(body)
    else:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            rows = _rows_from_csv(body)
        else:
            if isinstance(payload, list):
                rows = [r for r in payload if isinstance(r, dict)]
            elif isinstance(payload, dict):
                for key in ("rows", "data", "records", "results"):
                    if isinstance(payload.get(key), list):
                        rows = [r for r in payload[key] if isinstance(r, dict)]
                        break
    if not rows:
        raise ValueError("data_url returned no recognizable JSON records or CSV rows")
    return rows[:MAX_FETCH_ROWS], f"{urlparse(url).netloc} ({len(rows)} rows)"


# --- inline path ---------------------------------------------------------------


def _rows_inline(data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if len(data) > MAX_INLINE_ROWS:
        # Not fatal — but the response will warn. Cap at the fetch cap.
        data = data[:MAX_FETCH_ROWS]
    if not data:
        raise ValueError("inline data is empty; provide sql, data_url, or non-empty data")
    return data, f"inline ({len(data)} rows)"


# --- dispatcher ----------------------------------------------------------------


async def load_rows(
    *,
    sql: str | None,
    data_url: str | None,
    data: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Resolve exactly one data source. Returns (rows, source_desc, warnings)."""
    warnings: list[str] = []
    provided = [name for name, val in (("sql", sql), ("data_url", data_url), ("data", data)) if val]
    if len(provided) > 1:
        raise ValueError(f"provide exactly one data source; got {provided}")
    if not provided:
        raise ValueError("no data source: pass sql, data_url, or data")

    if sql is not None:
        statement = sql.strip()
        low = statement.lower()
        if not (low.startswith("select") or low.startswith("with")):
            raise ValueError("only read-only SELECT/WITH statements are allowed")
        if any(tok in low.split() for tok in ("insert", "update", "delete", "drop", "attach", "copy")):
            raise ValueError("only read-only SELECT/WITH statements are allowed")
        rows, source = await _rows_from_sql(statement)
    elif data_url is not None:
        rows, source = await _rows_from_url(data_url)
    else:
        rows, source = _rows_inline(data or [])

    if len(rows) > MAX_INLINE_ROWS and data:
        warnings.append(
            f"inline data has {len(rows)} rows (> {MAX_INLINE_ROWS}); "
            "prefer the sql or data_url source so rows are not round-tripped through the model"
        )
    return rows, source, warnings
