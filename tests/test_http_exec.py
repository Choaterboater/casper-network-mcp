"""Generated-tool shaping: the reply is trimmed to any ``fields`` the caller asked for.

Some Mist endpoints declare ``fields`` but still answer with the whole record,
so the projection is applied here as well.
"""

from casper_network_mcp.openapi_gen.http_exec import _project_fields, send


def test_project_top_level_record():
    assert _project_fields({"a": 1, "b": 2, "c": 3}, "a,c") == {"a": 1, "c": 3}


def test_project_records_in_a_list():
    assert _project_fields([{"a": 1, "b": 2}, {"a": 3}], "a") == [{"a": 1}, {"a": 3}]


def test_project_nested_primary_list_keeps_metadata():
    data = {"meta": "ok", "items": [{"a": 1, "b": 2}]}
    assert _project_fields(data, "a") == {"meta": "ok", "items": [{"a": 1}]}


def test_dotted_name_selects_its_top_level_key():
    data = {"radio_stat": {"channel": 1}, "ip": "10.0.0.1"}
    assert _project_fields(data, "radio_stat.channel") == {"radio_stat": {"channel": 1}}


def test_unknown_field_yields_an_empty_record_not_an_error():
    assert _project_fields({"a": 1}, "nope") == {}


def test_blank_fields_is_a_noop():
    data = {"a": 1}
    assert _project_fields(data, "  ") is data


class _FakeClient:
    async def request(self, *args, **kwargs):
        return {"items": [{"a": 1, "b": 2}], "meta": "m"}


async def test_send_projects_fields_in_a_query():
    out = await send(
        lambda: _FakeClient(),
        product="mist",
        method="GET",
        path="/api/v1/sites/s1/stats/devices",
        query={"fields": "a"},
        headers={},
        body=None,
        content_type="application/json",
        kind="read",
        path_args={},
    )
    assert out["items"] == [{"a": 1}]
    assert out["meta"] == "m"
