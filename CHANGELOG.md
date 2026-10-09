# Changelog

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
