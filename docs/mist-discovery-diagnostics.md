# Compact Mist discovery diagnostics

## Purpose

Reduce model round trips and response size when investigating AirPrint, Bonjour/mDNS,
AirPlay or Sonos discovery. Previously, the assistant had to discover several API tools,
resolve scope, read WLAN lists, and fetch full configurations; unrelated client inventories
and site settings could add substantial context without explaining the immediate blocker.

The user's natural-language symptom report is sufficient. This change improves tool evidence
and discoverability rather than requiring longer prompts.

## Changes in this commit

- Add read-only `mist_wlan_discovery_summary`.
- Resolve a small login scope automatically, or accept a known `site_id` or `org_id` to skip lookups.
- Read derived WLANs with site variables resolved so inherited template configurations are included.
- Return exact SSID matches with VLAN, isolation, multicast filtering, mDNS, SSDP and Bonjour evidence.
- Include discovery fields in `mist_list_wlans`, avoiding an extra full WLAN read in many cases.
- Add AirPrint, Bonjour, mDNS and printer search vocabulary and regenerate the tool index.
- Add offline functional, routing, response-size and MCP protocol regression coverage.

No write behavior, write approval, retry policy, credential handling or dependency changes are introduced.
The diagnostic sends GETs only. Live network configuration changes are not stored in this commit.

## Usage

Call `find_tool`:

```json
{"query":"AirPrint discovery","product":"mist","top_k":1,"include_schema":true}
```

Call `invoke_read_tool` using the discovered name:

```json
{"name":"mist_wlan_discovery_summary","arguments":{"ssid":"Example"}}
```

For a known site, provide `site_id` in `arguments`; for a known organization, provide `org_id` instead.
Both scope arguments cannot be supplied together. SSID matching is exact and case-sensitive.

Automatic resolution uses the login's visible scopes. It does not assume that an organization-level
request is available to a site-only observer. Automatic scanning is capped at 10 sites, with at most
four simultaneous derived-WLAN requests. Multiple organizations and larger scopes require an explicit
scope rather than an unbounded crawl. Duplicate SSIDs are returned without selecting a change target.
Malformed, failed or truncated reads are reported as incomplete, not as an absent SSID or healthy network.

## Interpreting the evidence

| Evidence | Meaning and limits |
| --- | --- |
| `limit_bcast: true`, `allow_mdns: false` | mDNS forwarding is filtered; this can block Bonjour/AirPrint/AirPlay discovery. Check configured Bonjour gateway services before choosing a remedy. |
| `limit_bcast: true`, `allow_ssdp: false` | SSDP forwarding is filtered; this can interfere with Sonos discovery/control. The current tool exposes the flags but does not emit a separate SSDP finding code. |
| `isolation: true` or `l2_isolation: true` | Isolation can restrict the traffic required between controller and speaker/printer. |
| Different reported phone/speaker IP ranges | Investigate leases, VLAN placement, routing and discovery forwarding. IP telemetry alone does not prove the active VLAN or a stale lease. |
| No findings | Not an end-to-end success verdict. Device advertisements, reachability, deployment state and application behavior remain unverified. |

The flags are not substitutes for traffic-path verification. Allowing mDNS does not enable SSDP, and
allowing SSDP does not itself repair cross-VLAN AirPlay or a stale DHCP lease. A Bonjour gateway is not
a general SSDP relay. Preserve multicast filtering when a narrow exception is sufficient; do not
change VLANs, isolation or templates without establishing the required scope and approving the change.

## Evidence and validation

Offline regression scenario: a synthetic observer login, two visible sites and one matching SSID.
No real network identifiers or saved credentials are used in the fixtures.

- Two MCP calls: `find_tool` with one schema, then `invoke_read_tool`.
- Combined structured discovery and diagnostic responses below 4,000 bytes.
- Four underlying Mist GETs: identity, organization site list and two derived-WLAN reads.
- A known `site_id` reduces the diagnostic to one derived-WLAN GET.
- Coverage includes inherited WLANs, duplicate SSIDs, site-only observers, larger scopes, incomplete
  replies, permission failures, invalid arguments, read-only pinning and omitted unrelated configuration.

These are bounded fixture assertions, not a production latency benchmark or exact token-cost measurement.
The MCP transport envelope and the assistant's subsequent troubleshooting calls are not included in the
structured-response byte assertion.

Full checks run successfully for this implementation:

```text
uv run pytest             3,351 passed, 139 skipped
uv run ruff check .       passed
uv run mypy .             passed
uv build                  passed
```

## Operational follow-up

During the supporting troubleshooting session, enabling mDNS on the WLAN was confirmed by the user to
restore AirPrint. On a subsequent Sonos/AirPlay investigation, the compact diagnostic exposed blocked SSDP
in the initial discovery-plus-diagnostic pair of MCP calls. Additional targeted reads found differing
reported phone/speaker IP ranges. SSDP was then enabled separately and verified in Mist configuration;
Sonos/AirPlay playback was not confirmed. Those operational changes are separate from this code commit.

The updated diagnostic was available in the connected MCP server during that follow-up. Other deployments
must restart/reconnect to load it, and an installation from a published package requires an updated package
or a launch configuration pointing to this checkout. There is no version bump or release in this commit.

## Later PR scope

This local commit is intended to be included in a larger PR. It introduces no promise to diagnose all
multicast failures and no automatic device remediation. Future improvements can separately add SSDP-specific
findings, AirPlay/Sonos vocabulary and targeted current-client/VLAN checks without returning entire inventories.

References:

- [Juniper: Add a Bonjour Gateway to a WLAN](https://www.juniper.net/documentation/us/en/software/mist/mist-wireless/topics/concept/mist-bonjour-gateway.html)
- [Sonos: Configure your firewall to work with Sonos](https://support.sonos.com/en-us/article/configure-your-firewall-to-work-with-sonos)
