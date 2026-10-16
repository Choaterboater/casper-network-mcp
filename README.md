# casper-network-mcp

One small MCP server for HPE Aruba Networking Central, Juniper Mist and HPE Aruba Networking ClearPass, built for
[Casper](https://github.com/Choaterboater/casper). Your AI sees four tools; behind them are about 3,900 tools for the
three products, each one labelled with the kind of change it makes.

You normally never install this yourself: Casper sets it up, pins the exact version and starts it for you.

## What it does

| Tool | What it is for |
| --- | --- |
| `find_tool` | Finds the right tool for what you asked ("bounce port 7 on the closet switch") and says what kind of change it makes: read, troubleshoot, config, disruptive, firmware, delete or admin. |
| `invoke_read_tool` | Runs a tool that only reads, or a troubleshooting check (ping, show, cable test; a cable test briefly takes the tested port's link down). Anything else is refused. |
| `invoke_tool` | Runs any tool, including ones that change your network. Casper asks you first. |
| `access_check` | Says, for each product, whether a login is set and where that login can change things. |

Behind them:

- hand-written tools for everyday work (sites, clients, alarms, switch ports, guests, sessions, SSIDs, VLANs and
  more), each one checked against the vendor's own API description;
- one generated tool for every operation in the bundled API descriptions;
- overview tools for broad questions: `central_site_overview`, `mist_site_overview` and `clearpass_overview` give
  health, device counts and the top alarms in one read;
- `clearpass_why_denied` summarizes recent sessions, endpoint status/profile, NAD and applied roles for exactly
  one MAC or username. The bundled 6.12.7 API exposes active accounting sessions, not full Access Tracker
  rejects; missing auth results, reject reasons and enforcement profiles remain unknown;
- ClearPass session and auth-failure reads accept MAC, username, NAD IP, accounting service type and ISO 8601
  start/end times (with timezone). Time windows scan at most ten pages locally; `next_server_offset` continues
  a scan, including an empty page. The service filter is not an Access Tracker policy service name;
- `lookup_api`, which answers exact questions about the vendors' APIs (endpoints, fields, allowed values) from the
  bundled documents, with no network.

## Running it

```
uvx --from casper-network-mcp==0.1.3 casper-network-mcp --read-only
```

| Option | Meaning |
| --- | --- |
| `--read-only` | Only read and run checks; never send a change. Read once at start: nothing turns writes on while it runs. Casper passes this until you allow changes. |
| `--transport stdio` | The default: talk over standard input and output. |
| `--transport http --port 8010` | Listen on this machine only (`127.0.0.1`). |

From a checkout, `.mcp.json.example` starts it read-only with `uv run`; logins come from your shell.

## Logins

The server reads only these variables from the environment Casper starts it with. It reads no settings files.
Leave out a product you do not use: the server still starts, `access_check` says that login is missing, and a call
to one of its tools answers `{"error": "login_missing", "product": "..."}`.

| Product | Variables |
| --- | --- |
| Juniper Mist | `MIST_API_TOKEN`, and `MIST_HOST` if your cloud is not `api.mist.com` |
| HPE Aruba Networking Central | `CENTRAL_BASE_URL`, `CENTRAL_CLIENT_ID`, `CENTRAL_CLIENT_SECRET` |
| HPE Aruba Networking ClearPass | `CLEARPASS_BASE_URL`, `CLEARPASS_API_TOKEN` |

ClearPass API tokens expire (the API client's token lifetime in ClearPass sets when). When a product turns a login
away (HTTP 401), `access_check` reports that product with `"login": "expired"` and a call to one of its tools returns
`{"error": "login_expired", "product": ...}`, so the client can ask you for a new one.

## Changes to your network

There are no write switches. What a login may do is set by its role in the product, and Casper asks you before
every change, showing the tool and what it changes. On top of that:

- `--read-only` refuses every change before anything is sent;
- a path piece that could reach somewhere else (`/`, `?`, `#`, `..`) is refused before anything is sent;
- when the server knows a Mist login can only read a site, a change to that site is refused before anything is
  sent;
- tools that disrupt the network (reboots, port and PoE bounces, disconnects) are labelled disruptive, so Casper
  asks every time;
- tokens, passwords and pre-shared keys are hidden in replies and errors.

The server never asks you to type anything and never treats an argument the AI sets as your approval. Approval is
Casper's box only.

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

### First-pick follow-up

A later offline comparison uses merged PR #12 (`72e41ce`) as its baseline. The original
75-question held-out dataset was unavailable; these are **36 new synthetic evaluation questions**
(12 per product), frozen before router edits. Only aggregate evaluation baseline scores were inspected
before tuning; evaluation misses were not used. This small sample is not evidence of equivalent gains
on real requests or on the original 75 questions.

The additional 30-question development set exposed a general parsing error: a leading product name
or alias hid the following action verb. `find_tool` now skips those prefixes when reading intent.
Scoring weights, candidate retrieval, tool summaries, the shipped index and dependencies are unchanged.

| Product | First, filtered | Top 3, filtered | First, unfiltered | Top 3, unfiltered | MRR, filtered |
| --- | --- | --- | --- | --- | --- |
| Central | 10/12 → 12/12 | 11/12 → 12/12 | 10/12 → 12/12 | 11/12 → 12/12 | 0.8869 → 1.0000 |
| Mist | 10/12 → 11/12 | 12/12 → 12/12 | 9/12 → 10/12 | 12/12 → 12/12 | 0.9167 → 0.9583 |
| ClearPass | 10/12 → 11/12 | 12/12 → 12/12 | 10/12 → 11/12 | 12/12 → 12/12 | 0.9028 → 0.9583 |

Top-ten accuracy remains 12/12 for every product in both modes. Warm filtered top-three p50/p95
was 0.72/5.37 ms before and 0.80/5.95 ms after in this local measurement, below the unchanged 50 ms
p95 limit. The original 60-question benchmark's per-product accuracy is unchanged in both modes;
its p50/p95 was 0.51/2.27 ms before and 0.53/2.29 ms after. Timings are descriptive, not portable
speed guarantees.

Two filtered evaluation first-pick failures remain: `"show WLAN security for one WLAN"` and
`"show every authentication service"`; each acceptable answer is second. Unfiltered evaluation also
prefers Central's overview for `"show the site health overview"`. No further tuning used these misses.
The new development set also exposes a limitation: without a product filter, `"mist restart an access point"`
drops out of the top three (Mist unfiltered development top-three: 11/11 → 10/11). Use the known product
filter; product-name hints are not hard filters.

The benchmark now reports every wrong-first result with its expected rank, separately from top-three
misses, plus per-product filtered/unfiltered top-one, top-three, top-ten and MRR:

```
uv run python scripts/bench_find_tool.py --questions tests/bench/find_tool_evaluation.yaml
uv run python scripts/bench_find_tool.py --questions tests/bench/find_tool_evaluation.yaml --json
```

Dataset provenance, development failure categories and frozen baseline hashes are in
[`tests/bench/README.md`](tests/bench/README.md). This evaluation set is now regression data; another
tuning cycle needs a fresh evaluation set. Tests guard each product's first/top-three scores independently,
so improvements elsewhere cannot conceal regressions.

## What is in the package

The package includes HPE's proprietary API documents (not MIT; see specs/NOTICE.md) and Juniper Mist's MIT
OpenAPI file. They are stored exactly as the vendors serve them; `scripts/refresh_specs.py --check` fetches them
again and confirms every hash. See `NOTICE.md` and `THIRD_PARTY_NOTICES.md`.

The package has no install step, downloads nothing when it starts and never updates itself.

## Working on it

```
uv sync
uv run pytest -q
uv run ruff check .
uv run python scripts/bench_find_tool.py     # find_tool accuracy and speed
uv run python scripts/build_manifests.py     # after the bundled specs change
uv run python scripts/build_index.py         # after any tool changes
```

A release is built from a `v*` tag: the wheel, then `casper-network-mcp.lock.txt` (every dependency pinned by hash,
plus this package pinned to the wheel's own hash, made by `scripts/make_lock.py`). The release checks that lock
installs with Casper's exact command before publishing:

```
uv pip install --require-hashes --no-deps --only-binary :all: -r casper-network-mcp.lock.txt
```

## Security

Report problems privately; see `SECURITY.md`.

## Licence

MIT for this project's code (`LICENSE`). The bundled vendor documents keep their own terms (`NOTICE.md`).
