"""Every hand-written tool calls an endpoint that is in its product's bundled spec.

Each tool is run on a recording fake (``tests/spec_check.py``) with
placeholder arguments, one call per value of each fixed-choice argument, and
the branches in ``tests/spec_branches.yaml``. Every request it sends must
land on a bundled operation, with query names that operation has and values
inside its enums.
"""

from __future__ import annotations

import inspect
import types
import typing

import pytest
from spec_check import NOT_CHECKED, VALIDATE_FIRST, all_hand_written_tools, calls_for, match, problems, record_requests


@pytest.mark.parametrize("tool", all_hand_written_tools(), ids=lambda t: t.name)
async def test_every_tool_calls_an_endpoint_in_the_spec(tool):
    if tool.name in NOT_CHECKED:
        pytest.skip(NOT_CHECKED[tool.name])
    found = []
    for args in calls_for(tool):
        for failing in (False, True):
            for req in await record_requests(tool, args, failing=failing):
                found.extend(problems(tool, req))
    assert not found, f"{tool.name}: " + "; ".join(dict.fromkeys(found))


@pytest.mark.parametrize("tool", all_hand_written_tools(), ids=lambda t: t.name)
async def test_every_tool_sends_a_request(tool):
    if tool.name in NOT_CHECKED or tool.name in VALIDATE_FIRST:
        pytest.skip(NOT_CHECKED.get(tool.name) or VALIDATE_FIRST[tool.name])
    assert await record_requests(tool, calls_for(tool)[0]), f"{tool.name} sent nothing"


def test_match_prefers_a_fixed_segment():
    op = match("GET", "/api/v1/sites/s1/stats/devices", "mist")
    assert op is not None and op.path == "/api/v1/sites/{site_id}/stats/devices"
    assert match("GET", "/api/v1/sites/s1/no_such_thing", "mist") is None
    assert match("GET", "/api/oauth/me", "clearpass").path == "/oauth/me"
    assert match("GET", "/oauth/me", "clearpass") is None


def test_skips_have_reasons():
    assert set(NOT_CHECKED) == {"central_get", "mist_get", "clearpass_get"}
    names = {t.name for t in all_hand_written_tools()}
    assert set(VALIDATE_FIRST) <= names
    assert all(VALIDATE_FIRST.values())


SLASHED = "zz9/qq8"


def _string_params(tool):
    try:
        hints = typing.get_type_hints(inspect.unwrap(tool.fn))
    except Exception:  # noqa: BLE001
        hints = {}
    out = []
    for name in inspect.signature(tool.fn).parameters:
        ann = hints.get(name)
        if ann is str or (typing.get_origin(ann) in (typing.Union, types.UnionType) and str in typing.get_args(ann)):
            out.append(name)
    return out


@pytest.mark.parametrize("tool", all_hand_written_tools(), ids=lambda t: t.name)
async def test_a_slash_in_any_string_argument_never_adds_a_path_segment(tool):
    """Review Focus 1: a path piece holding '/' is refused (or kept as one piece) before anything is sent."""
    if tool.name in NOT_CHECKED:
        pytest.skip(NOT_CHECKED[tool.name])
    base = calls_for(tool)[0]
    leaked = []
    for name in _string_params(tool):
        for req in await record_requests(tool, {**base, name: SLASHED}):
            if SLASHED in req.path:
                leaked.append(f"{name}: {req.method} {req.path}")
    assert not leaked, leaked


ASYNC_CENTRAL_TOOLS = [
    t for t in all_hand_written_tools() if t.product == "central" and inspect.iscoroutinefunction(inspect.unwrap(t.fn))
]


@pytest.mark.parametrize("tool", ASYNC_CENTRAL_TOOLS, ids=lambda t: t.name)
async def test_an_async_central_tool_never_blocks_the_event_loop(tool, monkeypatch):
    """The sync Central door (httpx.Client, time.sleep on retries) must not run on the event loop thread."""
    import asyncio

    from casper_network_mcp.products.central.client import CentralClient

    on_loop: list[str] = []
    real = CentralClient._roundtrip_sync

    def watched(self, method, path, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            on_loop.append(f"{method} {path}")
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(CentralClient, "_roundtrip_sync", watched)
    for args in calls_for(tool):
        for failing in (False, True):
            await record_requests(tool, args, failing=failing)
    assert not on_loop, f"{tool.name} made sync calls on the event loop: {sorted(set(on_loop))}"


def _read_runnable(tool):
    from casper_network_mcp.router.dispatch import is_read_tool
    from casper_network_mcp.router.index import catalog

    entry = catalog().get(tool.name)
    return entry is not None and is_read_tool(entry)


@pytest.mark.parametrize("tool", all_hand_written_tools(), ids=lambda t: t.name)
async def test_a_tool_invoke_read_tool_runs_sends_only_reads(tool):
    """invoke_read_tool runs a hand-written tool on its label alone; check the label against what it sends."""
    from casper_network_mcp.core.kinds import READ_POSTS, TROUBLESHOOT_OPS, listed_template
    from casper_network_mcp.router.index import catalog

    if tool.name in NOT_CHECKED:
        pytest.skip(NOT_CHECKED[tool.name])
    if not _read_runnable(tool):
        pytest.skip("not run by invoke_read_tool")
    kind = catalog().get(tool.name).kind
    allowed_posts = READ_POSTS if kind == "read" else READ_POSTS | TROUBLESHOOT_OPS
    bad = []
    for args in calls_for(tool):
        for failing in (False, True):
            for req in await record_requests(tool, args, failing=failing):
                if req.method == "GET":
                    continue
                template = listed_template(req.method, req.path)
                if req.method != "POST" or template is None or (req.method, template) not in allowed_posts:
                    bad.append(f"{req.method} {req.path}")
    assert not bad, f"{tool.name} (kind {kind}) sends changes: {sorted(set(bad))}"
