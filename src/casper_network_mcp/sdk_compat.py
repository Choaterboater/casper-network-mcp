"""The single place allowed to touch ``MCPServer``'s private tool manager.

Copied from hpe-networking-mcp ``mcp_servers/_sdk_compat.py`` (MIT,
nowireless4u/hpe-networking-mcp). No other module may reach into
``._tool_manager``; ``tests/test_no_private_sdk_access.py`` enforces that.

Before bumping ``mcp``, read
``tests/test_no_private_sdk_access.py::test_sdk_compat_matches_the_installed_sdk``.
It pins every SDK internal used below -- registry access, the internal-vs-wire
attribute split, verbatim ``Tool`` republication by object identity, raw
dispatch returning a Python value, and the claim/replace/restore interception
seam, including the fact that the public ``MCPServer.call_tool`` still routes
through it. An upstream rename must fail there, as one loud test.

Why this is needed
------------------
``mcp`` 2.x publishes ``list_tools()``, ``call_tool()``, ``add_tool()`` and
``remove_tool()``, but three things the router needs have no public form:

1. **Synchronous registry introspection.** ``list_tools()`` is a coroutine; the
   router's index builder runs in synchronous code.
2. **Publishing a pre-built ``Tool``.** ``add_tool()`` re-derives the tool from
   its function and drops ``title``/``icons``/``meta``; the router must
   republish the backend's exact ``Tool``.
3. **Raw in-process dispatch.** ``MCPServer.call_tool()`` converts results to
   wire content; the router's response budget works on the raw Python value,
   which only the manager's ``call_tool(..., convert_result=False)`` returns.

Interception happens at the tool manager's dispatcher because the SDK's
``ServerMiddleware`` is wire-tier only: in-process calls (how the router
reaches its backends) never pass through it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool

__all__ = [
    "call_tool_raw",
    "claim_dispatcher",
    "get_tool",
    "install_sorted_tool_listing",
    "register_tool_object",
    "set_dispatcher",
    "tool_names",
    "tool_registry",
]

_SORTED_LISTING_ATTR = "_casper_sorted_list_tools_applied"


def tool_registry(server: MCPServer) -> Mapping[str, Tool]:
    """The server's live ``{name: Tool}`` registry.

    The mapping is the manager's own dict, so it tracks later registrations --
    callers that need a snapshot must copy it. Typed ``Mapping`` because mutating
    it directly is not this module's contract: use :func:`register_tool_object`.
    """
    return server._tool_manager._tools


def tool_names(server: MCPServer) -> list[str]:
    """Registered tool names, sorted. The synchronous counterpart to ``list_tools()``."""
    return sorted(server._tool_manager._tools)


def get_tool(server: MCPServer, name: str) -> Tool | None:
    """The internal ``Tool`` object for ``name``, or ``None``.

    Returns the *internal* tool -- ``annotations``, ``parameters``, ``fn`` and
    ``fn_metadata`` -- not the ``MCPTool`` wire form that ``list_tools()`` builds.
    """
    return server._tool_manager.get_tool(name)


def register_tool_object(server: MCPServer, name: str, tool: Tool) -> None:
    """Publish an already-built ``Tool`` under ``name``, verbatim.

    Unlike ``MCPServer.add_tool``, this does not re-derive the tool from its
    function, so the advertised schema and metadata are preserved exactly.
    """
    server._tool_manager._tools[name] = tool


async def call_tool_raw(
    server: MCPServer,
    name: str,
    arguments: dict[str, Any],
    context: Any = None,
) -> Any:
    """Dispatch ``name`` in-process and return the tool's raw Python value.

    Goes through the manager's dispatcher, so any interception installed by
    :func:`set_dispatcher` applies.
    """
    return await server._tool_manager.call_tool(name, arguments, context, convert_result=False)


def _installed_attr(marker: str) -> str:
    return f"{marker}__wrapper"


def claim_dispatcher(server: MCPServer, marker: str) -> Callable[..., Awaitable[Any]]:
    """Return the dispatcher an interceptor registered under ``marker`` must call.

    The first claim under a given ``marker`` snapshots the dispatcher currently
    in place and remembers it on the manager, so two interceptors using
    different markers compose: each wraps whatever it found.

    A **re-claim is only safe while this marker's wrapper is still the outermost
    dispatcher.** Otherwise the remembered snapshot is stale -- something else
    has wrapped since -- and rebuilding from it would silently discard every
    interceptor installed in between. When one of those is a safety check, that
    is a security boundary disappearing with no error, no log and no failing
    test, so this refuses instead.

    Raises:
        RuntimeError: on a re-claim whose saved original is no longer current.
            Install order is fixed and one-shot per server; hitting this means a
            second install, which is the bug.
    """
    manager = server._tool_manager
    original = getattr(manager, marker, None)
    if original is None:
        original = manager.call_tool
        setattr(manager, marker, original)
        return original
    current = manager.call_tool
    installed = getattr(manager, _installed_attr(marker), None)
    if (installed is not None and current is installed) or current == original:
        # Either this marker's own wrapper is still outermost, or nothing has
        # been installed since the claim. Both are safe to rebuild from: the
        # saved original is still current, so no interceptor can be dropped.
        return original
    raise RuntimeError(
        f"refusing to re-install {marker!r} on server {getattr(server, 'name', '?')!r}: "
        "another interceptor has wrapped the tool dispatcher since this one was "
        "installed, so rebuilding from the saved original would silently remove it. "
        "Install each interceptor exactly once, outermost last."
    )


def set_dispatcher(
    server: MCPServer,
    dispatcher: Callable[..., Awaitable[Any]],
    marker: str | None = None,
) -> None:
    """Install ``dispatcher`` as the server's tool-call entry point.

    It must accept ``(name, arguments, context=None, convert_result=False)`` --
    the manager's own signature -- because the SDK calls it positionally from
    ``MCPServer.call_tool``.

    Pass the same ``marker`` used for :func:`claim_dispatcher` so a later
    re-claim can tell whether this wrapper is still the outermost one.
    """
    server._tool_manager.call_tool = dispatcher  # type: ignore[method-assign]
    if marker is not None:
        setattr(server._tool_manager, _installed_attr(marker), dispatcher)


def release_dispatcher(server: MCPServer, marker: str) -> bool:
    """Uninstall the interceptor registered under ``marker``, restoring what it wrapped.

    The symmetric counterpart to :func:`claim_dispatcher`, and the only correct
    way to take an interceptor back off. Restoring by hand -- assigning
    ``call_tool`` back to some remembered value -- leaves this module's
    bookkeeping pointing at a wrapper that is no longer installed, and the next
    claim then cannot tell a safe refresh from a silent removal.

    Returns ``True`` if an interceptor was removed, ``False`` if none was
    installed. Raises ``RuntimeError`` if something else has wrapped the
    dispatcher since -- unwinding out of order would drop that one, which is the
    same hazard :func:`claim_dispatcher` refuses.
    """
    manager = server._tool_manager
    original = getattr(manager, marker, None)
    if original is None:
        return False
    installed = getattr(manager, _installed_attr(marker), None)
    if installed is not None and manager.call_tool is not installed:
        raise RuntimeError(
            f"refusing to release {marker!r} on server {getattr(server, 'name', '?')!r}: "
            "another interceptor was installed on top of it, so restoring the saved "
            "original would silently remove that one too. Release in reverse install "
            "order."
        )
    manager.call_tool = original  # type: ignore[method-assign]
    delattr(manager, marker)
    if installed is not None:
        delattr(manager, _installed_attr(marker))
    return True


def install_sorted_tool_listing(server: MCPServer) -> bool:
    """Make ``list_tools()`` return tools ordered by name. Idempotent.

    Returns ``True`` if this call installed the ordering, ``False`` if it was
    already in place.
    """
    manager = server._tool_manager
    if getattr(manager, _SORTED_LISTING_ATTR, False):
        return False
    original = manager.list_tools

    def sorted_list_tools() -> list[Tool]:
        return sorted(original(), key=lambda tool: tool.name)

    manager.list_tools = sorted_list_tools  # type: ignore[method-assign]
    setattr(manager, _SORTED_LISTING_ATTR, True)
    return True
