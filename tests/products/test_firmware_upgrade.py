"""Firmware writes: the version lives on the compliance endpoint, and a write only queues.

``trigger_device_upgrade`` used to POST a version-chart body to
``/network-config/v1alpha1/device-firmware``, whose schema declares only ``issu`` and
``site-distribution`` — so the dry run passed and the real call failed with HTTP 400
"Node 'version-chart' not found as a child of 'device-firmware' node". It now writes the
compliance policy its sibling ``set_firmware_compliance`` uses, and its dry run names any
body node the endpoint does not declare.
"""

from __future__ import annotations

import pytest
from conftest import Backend

from casper_network_mcp import specs_bundle
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products._tools import unknown_body_nodes
from casper_network_mcp.products.central import config as central_config

_DEVICE_FIRMWARE = "/network-config/v1alpha1/device-firmware"
_COMPLIANCE = "/network-config/v1alpha1/firmware-compliance"
_SUCCESS = {"errorCode": "SUCC_001", "httpStatusCode": 200, "message": "success"}


@pytest.fixture
def central_config_backend(recording_transport, central_client_factory) -> Backend:
    from casper_network_mcp.products.central import tools as central

    client = central_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    servers = dict(central.backends(client=lambda: client))
    return Backend(servers[central_config.BACKEND_NAME])


class _FakeReads:
    """Stands in for the inventory lookups the tool makes before it builds the body."""

    def get_device_by_serial(self, serial_number: str) -> dict[str, str]:
        return {"deviceType": "ACCESS_POINT"}

    def get_device_scope_id(self, serial_number: str) -> str:
        return "164429338987122688"


@pytest.fixture
def reads(monkeypatch) -> _FakeReads:
    fake = _FakeReads()
    monkeypatch.setattr(central_config, "get_mcp_client", lambda: fake)
    return fake


def test_device_firmware_schema_has_no_version_chart():
    """The exact historical failure: the endpoint the old body targeted rejects version-chart."""
    bad = {"version-chart": {"version": "10.8.1.0_95966"}}
    assert unknown_body_nodes("central", "POST", _DEVICE_FIRMWARE, bad) == ["version-chart"]


def test_compliance_schema_accepts_the_body_the_tool_builds():
    good = {
        "name": "compliance-campus_ap",
        "enable": True,
        "version-chart": {"version": "10.8.1.0_95966"},
        "upgrade-mode": "REGULAR",
        "enforcement-schedule": {
            "upgrade-schedule": {"upgrade-schedule-mode": "IMMEDIATE"},
            "reboot-schedule": {"reboot-schedule-mode": "IMMEDIATE"},
        },
    }
    assert unknown_body_nodes("central", "POST", _COMPLIANCE, good) == []


async def test_trigger_device_upgrade_dry_run_targets_compliance(reads, central_config_backend):
    out = await central_config_backend.call(
        "trigger_device_upgrade", {"serial_number": "SN1", "firmware_version": "10.8.1.0_95966", "dry_run": True}
    )
    assert out["dry_run"] is True
    assert out["endpoint"] == _COMPLIANCE
    assert out["device_function"] == "CAMPUS_AP"
    assert out["payload"]["version-chart"] == {"version": "10.8.1.0_95966"}
    assert out["unknown_body_nodes"] == []
    assert out["errors"] == []


async def test_trigger_device_upgrade_dry_run_catches_an_undeclared_node(monkeypatch, reads, central_config_backend):
    """If the endpoint's schema declared only issu/site-distribution, the dry run flags the body."""
    monkeypatch.setattr(
        specs_bundle,
        "request_body_properties",
        lambda product, method, path: frozenset({"issu", "site-distribution"}),
    )
    out = await central_config_backend.call(
        "trigger_device_upgrade", {"serial_number": "SN1", "firmware_version": "10.8.1.0_95966", "dry_run": True}
    )
    assert "version-chart" in out["unknown_body_nodes"]
    assert any("version-chart" in e for e in out["errors"])


async def test_trigger_device_upgrade_says_queued_not_applied(recording_transport, reads, central_config_backend):
    recording_transport.reply(_SUCCESS, "POST", _COMPLIANCE)
    out = await central_config_backend.call(
        "trigger_device_upgrade", {"serial_number": "SN1", "firmware_version": "10.8.1.0_95966"}
    )
    assert out["endpoint_used"] == _COMPLIANCE
    assert out["status"] == "queued"
    assert out["applied"] is False
    assert "not applied yet" in out["note"]
    assert {c.method for c in recording_transport.calls} == {"POST"}
    assert recording_transport.calls[-1].url.path == _COMPLIANCE


async def test_set_firmware_compliance_says_queued_not_applied(recording_transport, central_config_backend):
    recording_transport.reply(_SUCCESS, "POST", _COMPLIANCE)
    out = await central_config_backend.call(
        "set_firmware_compliance",
        {"scope_id": "1", "device_function": "CAMPUS_AP", "firmware_version": "10.8.1.0_95966"},
    )
    assert out["action"] == "created"
    assert out["status"] == "queued"
    assert out["applied"] is False
    assert "not applied yet" in out["note"]
