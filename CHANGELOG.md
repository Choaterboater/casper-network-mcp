# Changelog

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
