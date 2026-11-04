"""Every tool's label and change kind: checked by hand, and never softer than Casper's own word rule."""

from __future__ import annotations

import re

import pytest

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.annotations import DIAGNOSTIC, READ_ONLY
from casper_network_mcp.core.kinds import CHANGE_KINDS, check_label_rows, label_for_kind, labels, tool_labels
from casper_network_mcp.openapi_gen.runtime import generated_backend
from casper_network_mcp.products import hand_written_backends

PRODUCTS = ("central", "mist", "clearpass")

# ── Casper's word rule, ported from Casper src/capabilities/kinds.ts as data ──
DELETE_WORDS = {"delete", "remove", "unclaim", "erase", "zeroize", "destroy", "wipe", "purge", "factory"}
FIRMWARE_WORDS = {"firmware", "upgrade", "downgrade", "image", "ota"}
ADMIN_WORDS = {
    "admin",
    "administrator",
    "sso",
    "saml",
    "invite",
    "token",
    "tokens",
    "apitoken",
    "rbac",
    "privilege",
    "privileges",
    "scim",
    "password",
    "credential",
    "credentials",
}
ACCOUNT_WORDS = {"user", "users", "account", "accounts"}
DISRUPTIVE_WORDS = {
    "bounce",
    "reboot",
    "restart",
    "reload",
    "disconnect",
    "deauth",
    "deauthenticate",
    "halt",
    "shutdown",
    "powercycle",
    "power",
    "reset",
    "kick",
}
TROUBLESHOOT_WORDS = {"ping", "traceroute", "show", "test", "iperf", "speedtest", "cable", "nslookup", "blink"}
RISKY = {"firmware", "delete", "admin"}


def tool_words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def casper_word_kind(name: str) -> str:
    words = tool_words(name)
    has = lambda group: any(w in group for w in words)
    if has(DELETE_WORDS):
        return "delete"
    if has(FIRMWARE_WORDS):
        return "firmware"
    if has(ADMIN_WORDS) or ("role" in words and any(w.startswith("assignment") for w in words)):
        return "admin"
    if has(DISRUPTIVE_WORDS):
        return "disruptive"
    if has(ACCOUNT_WORDS):
        return "admin"
    if has(TROUBLESHOOT_WORDS):
        return "troubleshoot"
    return "config"


def softer_than_casper(name: str, kind: str) -> str | None:
    """Why ``kind`` is less risky than Casper's reading of ``name``, or None.

    Casper takes read and troubleshoot from the label, so only change kinds are compared.
    """
    if kind in ("read", "troubleshoot"):
        return None
    words = casper_word_kind(name)
    if words in RISKY and kind not in RISKY:
        return f"Casper reads {name} as {words}, the server says {kind}"
    if words == "disruptive" and kind not in RISKY | {"disruptive"}:
        return f"Casper reads {name} as disruptive, the server says {kind}"
    return None


def test_casper_word_rule_port_matches_known_answers():
    assert casper_word_kind("mist_restart_site_device") == "disruptive"
    assert casper_word_kind("disconnect_user") == "disruptive"
    assert casper_word_kind("invite_glp_user") == "admin"
    assert casper_word_kind("trigger_device_upgrade") == "firmware"
    assert casper_word_kind("update_role") == "config"


# ── labels.yaml rows ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("product", PRODUCTS)
def test_every_row_has_a_kind_a_category_and_a_checked_note(product):
    for name, row in labels(product).items():
        assert row["kind"] in CHANGE_KINDS, name
        assert row.get("vendor_category"), name
        assert len(str(row["checked"])) > 10, name


@pytest.mark.parametrize("product", PRODUCTS)
def test_no_row_is_softer_than_casper_word_rule(product):
    bad = [why for name, row in labels(product).items() if (why := softer_than_casper(name, row["kind"]))]
    assert not bad, bad


def test_central_table_follows_the_plan():
    central = labels("central")
    expect = {
        "get_report_run_download_link": "read",
        "cx_show": "troubleshoot",
        "ap_ping": "troubleshoot",
        "cx_traceroute": "troubleshoot",
        "poe_bounce": "disruptive",
        "port_bounce": "disruptive",
        "reboot_device": "disruptive",
        "disconnect_client": "disruptive",
        "gateway_halt": "disruptive",
        "reboot_ap_swarm": "disruptive",
        "delete_device_notes": "delete",
        "trigger_device_upgrade": "firmware",
        "rotate_webhook_key": "admin",
        "create_notification_rule": "config",
        "clear_alerts": "config",
        "delete_role": "delete",
        "list_sites": "read",
    }
    for name, kind in expect.items():
        assert central[name]["kind"] == kind, name


# ── Show-only checks: read label, troubleshoot kind ─────────────────────────

#: Fixed or validated show commands and table reads: they change nothing and send no traffic.
SHOW_ONLY_READS = {
    "get_lldp_neighbors",
    "get_cx_arp_table",
    "get_cx_mac_table",
    "find_mac_on_switch",
    "get_switch_port_errors",
    "get_switch_spanning_tree",
    "get_switch_interface_counters",
    "aos_s_arp",
    "cx_show",
    "aos_s_show",
    "gateway_show",
    "ap_show",
}
#: Checks that send traffic from the device: they keep the diagnostic label, so Casper still asks.
LINK_CHECKS = {
    "cx_ping",
    "cx_traceroute",
    "aos_s_ping",
    "aos_s_traceroute",
    "gateway_ping",
    "gateway_traceroute",
    "ap_ping",
    "ap_traceroute",
    "ap_tcp",
    "ap_nslookup",
    "ap_http",
    "ap_https",
    "run_troubleshooting_bundle",
}
#: Casper treats a troubleshoot hit labelled read as a read unless its name has one of these words.
LINK_CHECK_WORDS = {"ping", "traceroute", "iperf", "speedtest", "cable", "nslookup", "blink"}


def test_show_only_checks_are_reads_that_keep_the_troubleshoot_kind():
    central = labels("central")
    for name in SHOW_ONLY_READS:
        assert central[name]["kind"] == "troubleshoot", name
        assert central[name].get("label") == "read", name
        annotations, meta = tool_labels("central", name)
        assert annotations == READ_ONLY, name
        assert meta == {"casper/change-kind": "troubleshoot"}, name


def test_checks_that_send_traffic_still_ask():
    central = labels("central")
    for name in LINK_CHECKS:
        assert central[name]["kind"] == "troubleshoot", name
        assert "label" not in central[name], name
        assert tool_labels("central", name)[0] == DIAGNOSTIC, name


def test_only_the_listed_checks_carry_a_read_label():
    for product in PRODUCTS:
        for name, row in labels(product).items():
            if "label" in row:
                assert (product, name) in {("central", n) for n in SHOW_ONLY_READS}, (product, name)
                assert not set(tool_words(name)) & LINK_CHECK_WORDS, name


@pytest.mark.parametrize(
    "row",
    [
        {"kind": "read", "label": "read"},
        {"kind": "config", "label": "read"},
        {"kind": "disruptive", "label": "read"},
        {"kind": "troubleshoot", "label": "diagnostic"},
        {"kind": "troubleshoot", "label": "write"},
        {"kind": "troubleshoot", "label": ""},
    ],
)
def test_a_label_is_only_read_on_a_troubleshoot_row(row):
    with pytest.raises(ValueError):
        check_label_rows("central", {"some_tool": {**row, "vendor_category": "x", "checked": "a checked note"}})


def test_a_troubleshoot_row_with_a_read_label_is_accepted():
    row = {"kind": "troubleshoot", "label": "read", "vendor_category": "x", "checked": "a checked note"}
    assert check_label_rows("central", {"some_tool": row}) == {"some_tool": row}


# ── Registered tools ────────────────────────────────────────────────────────


def _registered():
    for product, backend in hand_written_backends():
        for tool in sdk_compat.tool_registry(backend).values():
            yield product, tool


def test_every_hand_written_tool_has_a_row_and_its_label():
    for product, tool in _registered():
        row = labels(product).get(tool.name)
        assert row is not None, f"{product}: {tool.name} has no labels.yaml row"
        expected = READ_ONLY if row.get("label") == "read" else label_for_kind(row["kind"])
        assert tool.annotations == expected, tool.name
        assert (tool.meta or {}).get("casper/change-kind") == row["kind"], tool.name
        if tool.annotations.destructive_hint:
            assert row["kind"] in ("disruptive", "delete"), tool.name


def test_labels_have_no_rows_for_missing_tools():
    registered: dict[str, set[str]] = {}
    for product, tool in _registered():
        registered.setdefault(product, set()).add(tool.name)
    for product, names in registered.items():
        stale = set(labels(product)) - names
        assert not stale, (product, sorted(stale)[:10])


def test_tables_wait_for_their_tools():
    """A product's table may run ahead of its port only while its backend is not registered yet."""
    registered = {product for product, _ in hand_written_backends()}
    for product in PRODUCTS:
        if product not in registered and product != "central":
            assert labels(product) == {}, f"{product} rows land with its tools (Tasks 7 and 9)"


# ── Generated tools ─────────────────────────────────────────────────────────


class _NoClient:
    async def request(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError


@pytest.mark.parametrize("product", PRODUCTS)
def test_generated_tools_carry_their_kind_and_label(product):
    backend = generated_backend(product, client=_NoClient)
    softer = []
    for tool in sdk_compat.tool_registry(backend).values():
        kind = (tool.meta or {}).get("casper/change-kind")
        assert kind in CHANGE_KINDS, tool.name
        assert tool.annotations == label_for_kind(kind), tool.name
        if why := softer_than_casper(tool.name, kind):
            softer.append(why)
    assert not softer, softer[:10]


def test_spec_lookup_tools_say_they_read():
    from casper_network_mcp.products.specs_tools import backend

    for tool in sdk_compat.tool_registry(backend()).values():
        assert (tool.meta or {}).get("casper/change-kind") == "read", tool.name
        assert tool.annotations.read_only_hint is True
