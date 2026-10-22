"""busy_hours: one trend call per day, averaged by local hour, with the quietest window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products._tools import use_client
from casper_network_mcp.products.central import monitoring, trends

CLIENTS = "/network-monitoring/v1/clients-trend"
END = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)
CHICAGO = ZoneInfo("America/Chicago")


def _load(local_hour: int) -> int:
    """Quiet 03:00-06:00 local, busy at 10:00 local."""
    return 20 if 3 <= local_hour < 6 else (300 if local_hour == 10 else 100)


def _day_of_samples(request):
    start = datetime.fromisoformat(request.url.params["start-at"])
    stop = datetime.fromisoformat(request.url.params["end-at"])
    assert stop - start == timedelta(days=1)
    samples, t = [], start
    while t < stop:
        load = _load(t.astimezone(CHICAGO).hour)
        samples.append({"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "data": [load - 5, 5]})
        t += timedelta(minutes=5)
    return {"interval": "5 mins", "keys": ["Wired", "Wireless"], "samples": samples}


@pytest.fixture
def central(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    use_client("central", lambda: client)
    return client


async def test_a_week_of_client_counts_finds_the_quiet_local_hours(central, recording_transport):
    recording_transport.reply(_day_of_samples, "GET", CLIENTS)
    out = await monitoring.busy_hours(site_id="S1", days=7, timezone="America/Chicago", end_time=END.isoformat())
    assert len(recording_transport.calls) == 7 and out["days_fetched"] == 7 and out["failed_days"] == []
    assert out["quietest_window"] == {
        "start": "03:00",
        "end": "06:00",
        "avg": 20.0,
        "busiest_hour": "10:00",
        "busiest_avg": 300.0,
    }
    assert out["series_summed"] == ["Wired", "Wireless"] and out["samples"] == 7 * 288
    assert {c.url.params["site-id"] for c in recording_transport.calls} == {"S1"}
    assert out["to"] == "2026-10-21T17:00:00Z" and out["from"] == "2026-10-14T17:00:00Z"


async def test_a_failed_day_is_reported_and_the_rest_still_count(central, recording_transport):
    def flaky(request):
        if request.url.params["start-at"].startswith("2026-10-17"):
            return (500, {"message": "upstream"})
        return _day_of_samples(request)

    recording_transport.reply(flaky, "GET", CLIENTS)
    out = await monitoring.busy_hours(site_id="S1", days=7, timezone="America/Chicago", end_time=END.isoformat())
    assert out["days_fetched"] == 6 and out["failed_days"][0]["day_starting"].startswith("2026-10-17")
    assert out["quietest_window"]["start"] == "03:00"


async def test_switch_interface_uses_the_odata_window(central, recording_transport):
    path = "/network-monitoring/v1/switches/CORE1/interface-trends"
    recording_transport.reply(
        {"keys": ["rx", "tx"], "samples": [{"timestamp": 1792000000, "data": [1.5, 2.5]}]}, "GET", path
    )
    out = await monitoring.busy_hours(
        source="switch_interface", serial_number="CORE1", interface_id="1/5/17", days=1, end_time=END.isoformat()
    )
    params = recording_transport.calls[0].url.params
    assert params["filter"] == "timestamp gt 2026-10-20T17:00:00Z and timestamp lt 2026-10-21T17:00:00Z"
    assert params["interface-id"] == "1/5/17"
    assert out["samples"] == 1 and out["hours"][0]["avg"] == 4.0
    assert out["quietest_window"] is None  # not every hour has data


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"site_id": "S1", "timezone": "Mars/Base"}, "time zone"),
        ({"site_id": "S1", "days": 40}, "days"),
        ({}, "site_id"),
        ({"source": "switch_interface", "serial_number": "SN1"}, "interface_id"),
        ({"site_id": "S1", "end_time": "yesterday"}, "end_time"),
    ],
)
async def test_bad_arguments_are_refused_before_any_call(central, recording_transport, kwargs, message):
    out = await monitoring.busy_hours(**kwargs)
    assert message in out["error"] and recording_transport.calls == []


def test_epoch_milliseconds_and_graph_nesting_are_read():
    keys, samples = trends.parse_samples(
        {"graph": {"keys": ["in"], "samples": [{"timestamp": 1792000000000, "data": [7]}]}}
    )
    assert keys == ["in"] and samples[0][1] == 7.0 and samples[0][0].year == 2026
