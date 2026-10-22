# Changelog

## Unreleased

- New `gateway_ping` (with `vlan`, so a probe can take a client VLAN's path) and
  `gateway_traceroute`; both were already on the read-only check list but had no tool.
- Troubleshooting checks poll after 1, 2 and 4 seconds, then every 5, so a quick check answers in
  about a second instead of five. Pings, iperf and ping sweeps keep polling for as long as their count
  or duration needs, and a check still running at the end says so (`still_running`, `poll_path`).
- When Central refuses a show command (`1/5/49`, `vlan 144`), `cx_show`, `aos_s_show`, `gateway_show`
  and `ap_show` add `allowed_commands`: the device's catalog entries that start the same way, so the
  next try uses an accepted form. The catalog is read only after a refusal and kept 15 minutes.

## 0.1.5

Generated tools say what each argument is, dry runs name body keys a spec does not know, and the Mist
token can use mistapi's name.

- Generated tools describe each argument: path, query and header arguments carry the spec's own words
  (first paragraph, at most 200 characters); bare ones get generic words (org, site and device IDs, MAC,
  serial number, limit). The body argument lists its top-level fields, required ones first and marked.
- A generated change tool's dry run lists top-level body keys its spec does not declare
  (`unknown_body_nodes`, with one `errors` line each), as the Central firmware tool already did. It only
  informs: a spec can allow keys it does not list, so a real send is never refused for them.
- `MIST_APITOKEN` (mistapi's name) is read as the Mist token when `MIST_API_TOKEN` is unset.
- The code is formatted with `ruff format`.

## 0.1.4

Secrets stay hidden in large replies, ClearPass logs in by itself, writes send the bodies the specs
require, and a cable test now asks first.

- Security: a reply too large to return whole is now redacted before it is cut, so a secret deep in it
  (a WLAN PSK, a RADIUS secret) can no longer reach the AI in the cut text; text that is not JSON gets the
  string redaction, which now also hides `"key": "value"` pairs with a secret key. Mist claim codes
  (`magic`, `claim_code(s)`, `registration_code`) are hidden everywhere.
- Cable (TDR) tests on Central CX, AOS-S and Mist are now disruptive changes: they go through the change
  route and its confirmation, and the read-only pin blocks them. They briefly take the tested link down.
- Errors carry distinct codes and recovery guidance for permission (403), rate limit (429) and timeouts; a
  timed-out write is never reported as definitely failed, and a write is still sent only once.
- `find_tool`: a request naming one product ranks that product's tools first when no filter is given
  (explicit filters and exact API lookups still win); intent is read past a leading product name ("mist
  create a wlan"); folded plurals and synonyms, list/get ties and ClearPass denial questions rank better.
  The benchmark reports per product (filtered and unfiltered top-1/3/10 and MRR) with a frozen evaluation set.
- Generated tools keep body fields and required fields from `allOf` schemas (46 and 51 Central operations),
  an operation-level parameter overrides the path-level one of the same name (no more `site_id_2`), and the
  dry-run body check counts `allOf` properties.
- Mist search paging follows `search_after`, cursors Mist returns can be passed back to any tool, switch
  ports page for real, and NAC troubleshooting searches by MAC across pages.
- The Mist spec pin is labelled 2610.1.0, the version of the bundled file.

- ClearPass can log in by itself with an API client: set `CLEARPASS_CLIENT_ID` and `CLEARPASS_CLIENT_SECRET`
  and the server gets a token at `POST /api/oauth` (client credentials), keeps it in memory, renews it a minute
  before it expires and, after a 401, gets a new one and sends again once. A set `CLEARPASS_API_TOKEN` still
  wins and works as before.
- Added read-only `clearpass_why_denied`: recent session summaries, endpoint status/profile, NAD and applied
  roles for one MAC or username. Uses only the bundled 6.12.7 session and endpoint APIs; explicitly reports
  that active accounting sessions cannot establish missing Access Tracker reject reasons or enforcement profiles.
- Added validated MAC, username, NAD IP, accounting service type and timezone-aware ISO start/end filters to
  ClearPass session/auth-failure reads. Time filtering is local (the spec declares no range operators), bounded
  to ten pages per call with `next_server_offset` for continuation and a count of unusable timestamps.
- Added offline validation, summary, redaction, pagination and routing tests; regenerated the router index.
- `clearpass_create_guest` now sends the body the spec requires: `password` is required and the role is
  `role_id` (an integer), or `role_name`, which is looked up with `GET /api/role` and refused when no role or
  more than one role has that name. It used to send `role_name`, which the API does not take.
- `clearpass_disconnect_session` now sends `{"id": ..., "confirm_disconnect": true}`, which the spec requires.
- `clearpass_get` refuses a GET that is not a plain read (one listed in `KIND_OVERRIDES`, such as a guest
  receipt, which can hold a password) and points to that operation's own tool, like `mist_get` already did.
- Central `run_speed_test` now takes `iperf_server_address`, which the spec requires, and the spec's optional
  iperf settings (protocol, port, bandwidth, reverse, duration, parallel streams, omit, window size). It used
  to send an empty body.
- Central `gateway_config_static_route` now sends the spec's static-route profile: it takes `profile_name`
  and `routes` (`[{prefix, nexthop, distance}]`), mapped into the profile's `ipv4` and `ipv6` route lists, and
  sends the `device-function` the spec requires with a LOCAL object. The spec's IPv4 route has no prefix
  field, so an IPv4 route's prefix goes in its `prefix-vrf-nexthop-id` key as `<prefix>~<vrf>` (`vrf`
  defaults to `default`). That format is not yet confirmed on a device; the preview and a 4xx error say so.
- Central `update_device_settings` sets the device notes only, the one field
  `PATCH /network-monitoring/v1/devices/{serial-number}` takes. It no longer accepts other settings or tries
  the switch-system profile; `set_hostname` sets a hostname.
- Central `list_visitors` sends `limit`, `offset` and the `next` cursor (`next_cursor`), and `full_list`
  follows `next` (up to 50 pages of 200, with `next` returned if more remain). It used to read one default page.
- Mist `mist_update_wlan` reads the WLAN first and PUTs the whole object with the changes merged in, since the
  spec's PUT requires `ssid`. That read keeps secrets (a PSK, RADIUS secrets) so they are sent back
  unchanged; the preview and the reply still hide them. A WLAN that cannot be read whole is refused, and a
  refused change (read-only pin or scope) is refused before the read.

## 0.1.3

Read replies stay under 16 KiB and can be resumed, and two Mist tools diagnose NAC.

- A routed read reply is now capped at 16 KiB (was 200 KB), the size the consumer
  shows. A reply cut for size alone now gets a `next_cursor`, so a large stats read is
  sliced here and can be resumed instead of being cut blind downstream.
- A generated read that asks for `fields` returns only those top-level keys: in each
  record for a list reply or a reply with a main list of records, otherwise in the
  record itself. Some Mist endpoints declare `fields` but answer with the whole record.
  A dotted name such as `radio_stat.channel` keeps its top-level key.
- An HTML error body, such as a 404 page, is summarised as its title and size instead
  of being inlined; other non-JSON bodies are still cut to 2000 characters.
- Central `get_site` returns `{"found": false, "name": ..., "hint": ...}` when no site
  matches, instead of an empty result.
- Spec lookup schema hits carry the schema's description (which holds enum text), its
  declared fields and their enums.
- New Mist tools, all read-only:
  - `mist_whoami` (`GET /api/v1/self`): the login's identity, orgs and sites, for
    finding an `org_id` or `site_id`.
  - `mist_wlan_security_summary`: one WLAN's security in a few fields, the cloud PSKs
    bound to its SSID, and whether it is doing MPSK, with a verdict for allow-all MAB.
  - `mist_nac_troubleshoot`: joins a site's NAC clients, recent NAC events and the
    org's NAC rules; reports `event_counts`, `rules_with_unknown_auth_type` and a plain
    verdict (multi-PSK lookup intercepting, no rule matched, or a rule permitted).
- `uv run mypy .` passes: `scripts` is a package, the remaining `import yaml` sites
  carry the same type-ignore as the rest of the tree, and `refresh_specs` guards a
  missing entry before indexing it.
- Added Mist labels for the new tools and regenerated `router/index.json`.

## 0.1.2

Trimmed the largest Central replies, and fixed the per-device firmware upgrade path.

- `central_site_overview` returns compact alerts (id, name, severity, device, site,
  summary); `full_alerts=True` returns the raw alert objects, including their
  action/rootCause text.
- `list_clients`, `list_events`, `get_channel_utilization` and `find_scope` return
  compact rows by default; `full=True` returns the raw records. This also drops the
  duplicated `raw` copy that used to ride along with the radio and scope summaries.
- `trigger_device_upgrade` POSTed a `version-chart` body to
  `/network-config/v1alpha1/device-firmware`, whose schema declares only `issu` and
  `site-distribution`: the dry run passed and the real call failed with HTTP 400
  "Node 'version-chart' not found as a child of 'device-firmware' node". It now writes
  the firmware-compliance policy its sibling `set_firmware_compliance` uses, and its dry
  run names any body node the endpoint's bundled schema does not declare.
- Firmware writes (`trigger_device_upgrade`, `set_firmware_compliance`) now say
  `status: queued`, `applied: false` and a note that the change is queued, not applied
  yet, so a caller does not report it done before the device checks in.
- Regenerated `router/index.json` for the new tool parameters.

## 0.1.1

Baseline release.
