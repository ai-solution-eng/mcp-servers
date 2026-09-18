"""The fleet's shared ``/health`` + ``/healthz`` probe routes.

Wave-6 G1 (decision D16) extraction of the route pair that five servers
build byte-identically today (verified 2026-09-13): workbench, logsearch,
prometheus and applygate each define, inside their app-assembly function,

    async def health(_request):
        return JSONResponse(<payload>)

    routes = [Route("/health", health), Route("/healthz", health)]

The payloads differ per app — that is the one parameter:

* workbench / logsearch: ``{"status": "ok", "server": "<name>"}`` (the two
  bodies are byte-identical modulo the server name);
* prometheus: ``{"status": "ok", "prometheus": config.base_url}``;
* applygate: a richer body (``namespaces_enabled``, ``field_manager``) whose
  values are re-read per request — pass a zero-arg callable and it is
  evaluated per request exactly like applygate's closure does today.

Routes and response bytes are otherwise identical: both paths on every
consumer, JSON serialization through ``starlette.responses.JSONResponse``,
no auth (the probes stay public — the auth middlewares' public-path sets
already name them), no lifespan coupling.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Union

__all__ = ["DEFAULT_HEALTHZ_PATH", "DEFAULT_HEALTH_PATH", "health_routes"]

DEFAULT_HEALTH_PATH = "/health"
DEFAULT_HEALTHZ_PATH = "/healthz"

PayloadLike = Union[Mapping, Callable[[], Mapping]]


def health_routes(
    payload: PayloadLike | None = None,
    *,
    server_name: str | None = None,
    health_path: str = DEFAULT_HEALTH_PATH,
    healthz_path: str = DEFAULT_HEALTHZ_PATH,
):
    """Build the ``(Route("/health"), Route("/healthz"))`` probe pair.

    *payload* is the JSON body mapping — or a zero-arg callable returning the
    mapping, evaluated per request (for bodies that re-read configuration,
    the applygate pattern). When *payload* is omitted, *server_name* builds
    the fleet's common shape ``{"status": "ok", "server": <name>}``.

    Returns a 2-tuple of ``starlette.routing.Route`` — spread or list() it
    into the consumer's route table in the same position the inline pair
    occupied (first, ahead of /metrics and the app routes).
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if payload is None:
        if server_name is None:
            raise ValueError("health_routes needs payload= or server_name=")
        payload = {"status": "ok", "server": server_name}

    async def health(_request):
        body = payload() if callable(payload) else payload
        return JSONResponse(dict(body))

    return Route(health_path, health), Route(healthz_path, health)
