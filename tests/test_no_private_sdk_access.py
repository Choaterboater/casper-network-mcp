"""No module outside ``sdk_compat.py`` may reach into MCP's private tool manager.

Ported from hpe-networking-mcp ``tests/unit/test_no_private_sdk_access.py``. The scan is AST-based: attribute
access and string literals count; docstrings and comments are prose and do not.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
PRIVATE_ATTR = "_tool_manager"
QUARANTINE = {"sdk_compat.py"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _offenders_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    rel = path.relative_to(REPO_ROOT)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == PRIVATE_ATTR:
            hits.append(f"{rel}:{node.lineno} (attribute access)")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and PRIVATE_ATTR in node.value
            and id(node) not in docstrings
        ):
            hits.append(f"{rel}:{node.lineno} (code in a string literal)")
    return hits


def test_no_private_tool_manager_access() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in QUARANTINE:
            continue
        offenders.extend(_offenders_in(path))
    assert not offenders, "private MCP SDK tool-manager access outside sdk_compat.py:\n  " + "\n  ".join(offenders)


def test_quarantine_module_is_the_one_that_reaches_in() -> None:
    compat = SRC / "casper_network_mcp" / "sdk_compat.py"
    assert _offenders_in(compat), "sdk_compat.py no longer touches the private tool manager; drop the quarantine"


def test_sdk_compat_matches_the_installed_sdk() -> None:
    import anyio
    from mcp.server.mcpserver import MCPServer

    from casper_network_mcp import sdk_compat as compat

    server = MCPServer("sdk-compat-probe")

    @server.tool()
    def probe_tool(value: int = 1) -> dict:
        return {"value": value}

    assert compat.tool_names(server) == ["probe_tool"]
    assert set(compat.tool_registry(server)) == {"probe_tool"}

    tool = compat.get_tool(server, "probe_tool")
    assert tool is not None
    assert hasattr(tool, "annotations")
    assert isinstance(tool.parameters, dict)
    assert compat.get_tool(server, "absent") is None

    other = MCPServer("sdk-compat-probe-2")
    compat.register_tool_object(other, "probe_tool", tool)
    assert compat.get_tool(other, "probe_tool") is tool

    assert anyio.run(compat.call_tool_raw, server, "probe_tool", {"value": 7}) == {"value": 7}

    pristine = compat.claim_dispatcher(server, "_probe_marker")
    assert compat.claim_dispatcher(server, "_probe_marker") is pristine

    seen: list[str] = []

    async def intercepted(name, arguments, context=None, convert_result=False):
        seen.append(name)
        return await pristine(name, arguments, context, convert_result=convert_result)

    compat.set_dispatcher(server, intercepted)
    anyio.run(server.call_tool, "probe_tool", {})
    assert seen == ["probe_tool"], "public call_tool no longer routes through the seam"
    compat.set_dispatcher(server, pristine)

    assert compat.install_sorted_tool_listing(server) is True
    assert compat.install_sorted_tool_listing(server) is False
