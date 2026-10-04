"""Change kinds for generated tools: whole-word rules and the hand-checked lists."""

from __future__ import annotations

import re

import pytest

from casper_network_mcp.core import kinds
from casper_network_mcp.core.annotations import DESTRUCTIVE, DIAGNOSTIC, READ_ONLY, WRITE
from casper_network_mcp.core.kinds import (
    CHANGE_KINDS,
    KIND_OVERRIDES,
    READ_POSTS,
    TROUBLESHOOT_OPS,
    kind_for_operation,
    label_for_kind,
)
from casper_network_mcp.specs_bundle import operations

DOWNLOAD_LINK = "/network-reporting/v1/reports/{report-id}/report-runs/{report-run-id}/download-link"


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/v1/sites/{site_id}/wlans", "read"),
        ("GET", "/api/v1/orgs/{org_id}/alarms/search", "read"),  # Mist's search is a GET in its spec
        ("POST", DOWNLOAD_LINK, "read"),  # a POST that only reads, on READ_POSTS
        ("POST", "/api/v1/sites/{site_id}/devices/upgrade", "firmware"),
        ("DELETE", "/api/v1/sites/{site_id}", "delete"),
        ("POST", "/api/v1/orgs/{org_id}/invites", "admin"),
        ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/restart", "disruptive"),
        ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/ping", "troubleshoot"),
        ("PUT", "/api/v1/sites/{site_id}/wlans/{wlan_id}", "config"),
        ("PUT", "/api/role-mapping/{id}", "config"),  # "ping" inside "mapping" is not a word
        ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/pingsweep", "config"),  # not on TROUBLESHOOT_OPS
        ("POST", "/api/slideshow/{id}", "config"),  # "show" inside "slideshow" is not a word
        ("POST", "/network-troubleshooting/v1/cx/{serial-number}/poeBounce", "disruptive"),  # camelCase words
        ("POST", "/network-troubleshooting/v1/aps/{serial-number}/rebootSwarm", "disruptive"),
        ("POST", "/network-troubleshooting/v1/cx/{serial-number}/showCommands", "troubleshoot"),
        ("POST", "/api/v1/sites/{site_id}/maps/{image_id}/x", "config"),  # a path parameter's name is not a word
        ("POST", "/api/v1/orgs/{org_id}/alarms/search", "config"),  # no such read POST in the spec
    ],
)
def test_kind_rules(method, path, expected):
    assert kind_for_operation(method, path, "") == expected


def test_operation_id_words_count():
    assert kind_for_operation("POST", "/api/x/{id}", "restartSiteDevice") == "disruptive"
    assert kind_for_operation("POST", "/api/x/{id}", "mappingUpdate") == "config"


def test_concrete_paths_match_the_lists():
    assert kind_for_operation("POST", "/api/v1/sites/s1/devices/d1/ping", "") == "troubleshoot"
    assert kind_for_operation("POST", "/network-reporting/v1/reports/r1/report-runs/r2/download-link", "") == "read"
    assert kind_for_operation("GET", "/api/v1/installer/sites/Branch-12/optimize", "") == "config"


def test_troubleshoot_only_from_the_hand_checked_list():
    for method, path in TROUBLESHOOT_OPS:
        assert kind_for_operation(method, path, "") == "troubleshoot"
    for product in ("central", "mist", "clearpass"):
        for op in operations(product):
            if (op.method, op.path) not in TROUBLESHOOT_OPS and (op.method, "/api" + op.path) not in TROUBLESHOOT_OPS:
                full = ("/api" + op.path) if product == "clearpass" else op.path
                assert kind_for_operation(op.method, full, op.operation_id) != "troubleshoot", (op.method, op.path)


def test_every_listed_operation_is_in_a_bundled_spec_and_has_a_reason():
    known = set()
    for product in ("central", "mist", "clearpass"):
        prefix = "/api" if product == "clearpass" else ""
        known |= {(op.method, prefix + op.path) for op in operations(product)}
    for table in (READ_POSTS, TROUBLESHOOT_OPS, KIND_OVERRIDES):
        for key in table:
            assert key in known, key
    for key, reason in kinds.READ_POST_REASONS.items():
        assert key in READ_POSTS and len(reason) > 10
    assert set(kinds.READ_POST_REASONS) == set(READ_POSTS)
    assert set(kinds.TROUBLESHOOT_REASONS) == set(TROUBLESHOOT_OPS)
    assert all(len(r) > 10 for r in kinds.TROUBLESHOOT_REASONS.values())
    for kind, reason in KIND_OVERRIDES.values():
        assert kind in CHANGE_KINDS and len(reason) > 10


def test_read_posts_and_troubleshooting_are_posts():
    assert {m for m, _ in READ_POSTS} == {"POST"}
    assert {m for m, _ in TROUBLESHOOT_OPS} == {"POST"}


def test_listed_templates_never_cover_a_different_fixed_operation():
    """A concrete path that matches a listed template must not be another operation with a fixed segment there."""
    for product in ("central", "mist", "clearpass"):
        prefix = "/api" if product == "clearpass" else ""
        for op in operations(product):
            full = prefix + op.path
            found = kinds.listed_template(op.method, full)
            if found is not None:
                assert _shape(found) == _shape(full), (op.method, full, found)


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def test_no_disruptive_spec_operation_is_on_the_troubleshoot_list():
    for method, path in TROUBLESHOOT_OPS:
        words = set(kinds.path_words(path))
        assert not words & (kinds.DISRUPTIVE_WORDS | kinds.DELETE_WORDS | kinds.FIRMWARE_WORDS), path
        assert "cable" not in words, "a cable test drops the link on the port it tests"


@pytest.mark.parametrize(
    "kind,label",
    [
        ("read", READ_ONLY),
        ("troubleshoot", DIAGNOSTIC),
        ("config", WRITE),
        ("admin", WRITE),
        ("firmware", WRITE),
        ("disruptive", DESTRUCTIVE),
        ("delete", DESTRUCTIVE),
    ],
)
def test_label_for_kind(kind, label):
    assert label_for_kind(kind) == label


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        label_for_kind("write")


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        # op in the body can delete devices from the org or downgrade them
        ("PUT", "/api/v1/orgs/{org_id}/inventory", "delete"),
        ("PUT", "/api/v1/orgs/o1/inventory", "delete"),
        # op remove/renumber takes a member out of the virtual chassis
        ("PUT", "/api/v1/sites/{site_id}/devices/{device_id}/vc", "disruptive"),
        ("PUT", "/api/v1/installer/orgs/{org_id}/devices/{fpc0_mac}/vc", "disruptive"),
        # op delete removes a VC port, which can split the virtual chassis
        ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/vc/vc_port", "disruptive"),
        # action Bounce makes OnGuard agents bounce the endpoint's connection now
        ("POST", "/api/onguard-activity/notification", "disruptive"),
    ],
)
def test_a_body_op_that_deletes_or_disrupts_is_labelled_so(method, path, expected):
    assert kind_for_operation(method, path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/network-config/v1alpha1/cnac-named-mpsk-reg/export",
        "/network-config/v1alpha1/cnac-visitor/export",
        "/api/guest/{guest_id}/receipt/{id}",
        "/api/guest/g1/receipt/r1",
    ],
)
def test_a_text_export_that_can_hold_passwords_is_not_a_read(path):
    # CSV or receipt text is never redacted, so these must not run as plain reads.
    assert kind_for_operation("GET", path) == "config"


async def test_invoke_read_tool_refuses_a_password_export():
    from casper_network_mcp.router import dispatch

    for name in ("central_export_named_mpsk_csv_file", "central_export_visitor_csv_file"):
        out = await dispatch.invoke_read_tool(name, {})
        assert out["error"] == "not_a_read_tool", name
