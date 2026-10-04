# casper-network-mcp

One small MCP router for HPE Aruba Networking Central, Juniper Mist and ClearPass, built for Casper. Work in progress.

## How well find_tool finds tools

`find_tool` was measured on a fixed set of 60 plain questions (20 each for Central, Mist and ClearPass, in
`tests/bench/find_tool_questions.yaml`), then tuned one change at a time. A change stayed only if it found more
right tools in the top 3, or answered faster without finding fewer. Run `uv run python scripts/bench_find_tool.py`
to measure it yourself.

| | Right tool first | Right tool in top 3 | Top 3, no product given | Time per question (p50 / p95) | First question in a new process |
| --- | --- | --- | --- | --- | --- |
| Before | 67% | 77% | 68% | 0.6 / 1.6 ms | 3.4 s |
| After | 85% | 100% | 90% | 0.6 / 2.0 ms | 0.4 s |

What changed, in order:

1. Synonyms (`router/synonyms.yaml`) and plural folding: "access point" finds `ap`, "kick" finds disconnect, "who is
   on" finds clients. Top 3: 77% to 97%.
2. Ranking by what the question asks: "what" and "show" prefer tools that read, "create", "delete" and "change"
   prefer those changes; the product name in a tool's name no longer counts against it. Top 3: 97% to 98%, first
   pick 75% to 87%. (Weighting name words over description words was tried and dropped: top 3 fell to 90-95%.)
3. A prebuilt word index shipped in the package (`router/index.json`): the first question no longer loads every
   tool. First question: 3.4 s to 0.4 s.
4. Overview tools for broad questions (`central_site_overview`, `mist_site_overview`, `clearpass_overview`: health,
   device counts and top alarms in one read) and `mist_list_site_clients` for "who is on the wifi". A synonym that
   spreads into several words ("wifi" to wlan, ssid, wireless) now counts once. Top 3: 98% to 100%.

Times are on a laptop with the router already loaded, except the last column.
