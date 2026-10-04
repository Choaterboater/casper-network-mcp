"""Serving over stdio and local HTTP.

Ported from hpe-networking-mcp ``tests/unit/test_shared_transport.py``. Transport, host and port are arguments now
(command-line flags), never environment variables; HTTP serves this machine
only, so the allow-list and bearer cases are gone.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from casper_network_mcp.core.http import _POOL, pooled_client
from casper_network_mcp.core.serve import (
    UnsafeHttpBindingError,
    register_health_routes,
    run_server,
    serve_with_pool_cleanup,
)
from casper_network_mcp.core.url_validation import is_loopback_host


class _DummyMCP:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(log_level="INFO")
        self.run_calls: list[dict] = []
        self.custom_routes: list[tuple[str, list[str]]] = []

    async def run_stdio_async(self):
        self.run_calls.append({"transport": "stdio"})

    async def run_streamable_http_async(self, **kwargs):
        self.run_calls.append({"transport": "http", **kwargs})

    def custom_route(self, path, methods, name=None, include_in_schema=True):
        def decorator(fn):
            self.custom_routes.append((path, methods))
            return fn

        return decorator


def test_http_threads_host_and_port():
    server = _DummyMCP()
    run_server(server, transport="http", host="127.0.0.1", port=9000)
    call = server.run_calls[0]
    assert (call["host"], call["port"]) == ("127.0.0.1", 9000)
    assert call["transport_security"].enable_dns_rebinding_protection is True


def test_http_defaults_to_8010_on_loopback():
    server = _DummyMCP()
    run_server(server, transport="http")
    assert server.run_calls[0]["host"] == "127.0.0.1" and server.run_calls[0]["port"] == 8010


def test_http_registers_health_routes_once():
    server = _DummyMCP()
    run_server(server, transport="http")
    register_health_routes(server)
    assert sorted(p for p, _ in server.custom_routes) == ["/healthz", "/livez"]


def test_stdio_dispatches_stdio_runner():
    server = _DummyMCP()
    run_server(server)
    assert server.run_calls == [{"transport": "stdio"}]


def test_unknown_transport_rejected():
    server = _DummyMCP()
    with pytest.raises(ValueError, match="Unknown transport: bogus"):
        run_server(server, transport="bogus")
    assert server.run_calls == []


@pytest.mark.parametrize("host", ["0.0.0.0", "198.51.100.7", "mcp.example.net", "::"])
def test_http_refuses_to_listen_beyond_this_machine(host):
    server = _DummyMCP()
    with pytest.raises(UnsafeHttpBindingError):
        run_server(server, transport="http", host=host)
    assert server.run_calls == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.5.5.5"])
def test_loopback_hosts(host):
    assert is_loopback_host(host) is True


def test_serve_with_pool_cleanup_drains_pool_on_clean_exit():
    created = {}

    async def serve():
        created["client"] = pooled_client("test-clean-exit")

    asyncio.run(serve_with_pool_cleanup(serve))
    assert created["client"].is_closed and "test-clean-exit" not in _POOL


def test_serve_with_pool_cleanup_drains_pool_when_serve_raises():
    created = {}

    async def serve():
        created["client"] = pooled_client("test-error-exit")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(serve_with_pool_cleanup(serve))
    assert created["client"].is_closed and "test-error-exit" not in _POOL


def test_in_process_restart_leaves_no_unclosable_clients():
    server = _DummyMCP()
    clients = []

    async def run_stdio_with_pool():
        clients.append(pooled_client("test-restart"))

    server.run_stdio_async = run_stdio_with_pool
    run_server(server)
    run_server(server)
    assert len(clients) == 2 and all(c.is_closed for c in clients) and "test-restart" not in _POOL


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"nothing listening on {host}:{port}")


def test_real_mcp_round_trip_over_local_http():
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.mcpserver import MCPServer

    host, port = "127.0.0.1", _free_loopback_port()
    server = MCPServer("http-transport-smoke")

    @server.tool()
    def ping() -> str:
        """Ping."""
        return "pong"

    register_health_routes(server)
    app = server.streamable_http_app(host=host, transport_security=None)
    uv_server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

    async def _run():
        task = asyncio.create_task(uv_server.serve())
        try:
            await _wait_for_port(host, port)
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                livez = await client.get("/livez")
                assert livez.status_code == 200 and livez.json() == {"status": "ok"}
            async with (
                streamable_http_client(f"http://{host}:{port}/mcp") as (read, write),
                ClientSession(read, write) as session,
            ):
                init = await session.initialize()
                assert init.server_info.name == "http-transport-smoke"
                assert {t.name for t in (await session.list_tools()).tools} == {"ping"}
                call = await session.call_tool("ping", {})
                assert call.content[0].text == "pong"
        finally:
            uv_server.should_exit = True
            await task

    asyncio.run(_run())


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "[::1]", "::1", "LOCALHOST", "localhost.localdomain"])
def test_every_loopback_host_turns_on_host_checks(host):
    from mcp.server.mcpserver import MCPServer
    from starlette.testclient import TestClient

    from casper_network_mcp.core.serve import loopback_transport_security

    settings = loopback_transport_security(host)
    assert settings.enable_dns_rebinding_protection is True
    app = MCPServer("rebind").streamable_http_app(host=host, transport_security=settings)
    bare = host.strip("[]").lower()
    own = f"[{bare}]:8010" if ":" in bare else f"{bare}:8010"
    with TestClient(app) as client:

        def post(host_header):
            headers = {"host": host_header, "content-type": "application/json", "accept": "application/json"}
            return client.post("/mcp", headers=headers, content="{}").status_code

        assert post("evil.example:8010") == 421
        assert post(own) != 421


def test_run_server_checks_hosts_for_a_loopback_the_sdk_does_not_list():
    server = _DummyMCP()
    run_server(server, transport="http", host="127.0.0.2", port=9000)
    settings = server.run_calls[0]["transport_security"]
    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.2:*" in settings.allowed_hosts
