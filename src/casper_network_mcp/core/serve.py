"""Run an MCP server over stdio or local HTTP.

Adapted from hpe-networking-mcp's ``shared.run_server``,
``_configure_http_transport`` and ``_serve_with_pool_cleanup``. Changes: transport, host and port come
from the caller (the command line), never the environment; HTTP serves this
machine only (127.0.0.1, ::1, localhost), because this version has no HTTP
login of its own; no metrics route and no credentials-file readiness check.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from casper_network_mcp.core.http import aclose_pooled_clients
from casper_network_mcp.core.url_validation import is_loopback_host

__all__ = ["DEFAULT_HTTP_PORT", "TRANSPORTS", "UnsafeHttpBindingError", "register_health_routes", "run_server"]

DEFAULT_HTTP_PORT = 8010
TRANSPORTS = ("stdio", "http")
_HEALTH_ROUTES_ATTR = "_casper_health_routes_installed"


class UnsafeHttpBindingError(RuntimeError):
    """HTTP was asked to listen beyond this machine."""


def register_health_routes(mcp_instance: Any) -> None:
    """Add ``GET /livez`` and ``GET /healthz`` (no network calls). Safe to call twice."""
    if getattr(mcp_instance, _HEALTH_ROUTES_ATTR, False):
        return

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp_instance.custom_route("/livez", methods=["GET"], include_in_schema=False)(ok)
    mcp_instance.custom_route("/healthz", methods=["GET"], include_in_schema=False)(ok)
    setattr(mcp_instance, _HEALTH_ROUTES_ATTR, True)


async def serve_with_pool_cleanup(serve: Callable[[], Awaitable[None]]) -> None:
    """Run ``serve`` and always close pooled HTTP clients when it returns."""
    try:
        await serve()
    finally:
        await aclose_pooled_clients()


def run_server(
    mcp_instance: Any,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = DEFAULT_HTTP_PORT,
) -> None:
    """Serve ``mcp_instance`` until it stops, then close pooled clients.

    ``transport`` is ``stdio`` or ``http`` (streamable HTTP on ``host:port``).
    """
    import anyio

    if transport == "stdio":
        anyio.run(serve_with_pool_cleanup, mcp_instance.run_stdio_async)
        return
    if transport != "http":
        raise ValueError(f"Unknown transport: {transport} (use stdio or http)")
    if not is_loopback_host(host):
        raise UnsafeHttpBindingError(
            f"HTTP can only listen on this machine (127.0.0.1, ::1 or localhost), not {host!r}."
        )
    register_health_routes(mcp_instance)

    async def _serve_http() -> None:
        # transport_security=None lets the SDK apply its own loopback-only
        # Host/Origin allow-list, which is right because host is loopback.
        await mcp_instance.run_streamable_http_async(host=host, port=port, transport_security=None)

    anyio.run(serve_with_pool_cleanup, _serve_http)
