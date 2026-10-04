"""Every hand-written tool calls an endpoint that is in its product's bundled spec.

Each tool is run on a recording fake (``tests/spec_check.py``) with
placeholder arguments, one call per value of each fixed-choice argument, and
the branches in ``tests/spec_branches.yaml``. Every request it sends must
land on a bundled operation, with query names that operation has and values
inside its enums.
"""

from __future__ import annotations

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
