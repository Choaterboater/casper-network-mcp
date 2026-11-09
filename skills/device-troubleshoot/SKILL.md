---
name: device-troubleshoot
description: Troubleshoot one device or user through casper-network-mcp, checking Mist, Central or AOS 8 first and ClearPass second, ending in a timeline and root cause. Use when the user asks why a device, client, MAC, user or hostname can't connect, authenticate or get its role/VLAN, and casper-network-mcp is the network MCP.
---

# Device troubleshoot (casper-network-mcp)

The portable version: one MCP, `casper-network-mcp` (Casper names it `network`), on any customer's network. Build the **whole picture** of one device: where it attaches, what Mist, Central or the AOS 8 controller says, and what ClearPass decided. Every claim in the answer cites the tool output it came from; a source that returned nothing is reported as empty, never skipped.

Every call goes through `find_tool` (to get the exact name and arguments, `include_schema: true`), then `invoke_read_tool`. Run `access_check` first: it says which products have a login, so you know which steps can run and which to report as unavailable.

## Steps

1. **Scope.** `access_check`. Done when you know which of Mist, Central, AOS 8 and ClearPass have a login. The network view (steps 2 to 4) is whichever of Mist, Central or AOS 8 is logged in; run each one that is.
2. **Mist** (when it has a login). Get `org_id` from `mist_whoami` and `site_id` from `mist_list_sites` (`find_tool` does not surface `mist_whoami` for "org id"). Find the client with `mist_list_site_clients` (`mac`, `hostname`, `username` or `ip`), then `mist_get_client` for health (AP, SSID, RSSI, SNR). For any auth question run `mist_nac_troubleshoot`: its `verdict` says permitted, denied (no rule matched) or intercepted by multi-PSK, and `rules_with_unknown_auth_type` names rules that can never match. Done when the client has a record with AP or port and a NAC verdict, or a stated empty result.
3. **Central** (when it has a login, or Mist doesn't know the device). `find_client` with the MAC or IP, then `get_client_details` for auth, VLAN, role and usage. Done when the client has a record or a stated empty result.
4. **AOS 8** (when it has a login). `aos8_find_client` with exactly one of `mac`, `ip` or `username`, then `aos8_get_client_detail` (`mac`) for role and `role_derivation` (a default role means the RADIUS server sent none), VLAN, auth method and `auth_server`. On any auth problem run `aos8_auth_trace` (`mac`): its `summary` counts RADIUS accepts, rejects and timeouts and names the `last_outcome` (a reject is the server's policy; a timeout is reachability or the shared secret). `aos8_list_aaa_servers` says which RADIUS servers the controller uses. Done when the client has a record with role, VLAN and auth server plus a trace outcome, or a stated empty result (`aos8_get_client_history` shows when it left).
5. **ClearPass.** Go here when step 2, 3 or 4 shows an auth failure, the wrong VLAN or role, or a NAD or AOS 8 auth server that points at ClearPass. Run `clearpass_why_denied` (`mac` or `username`), `clearpass_get_endpoint_by_mac` and `clearpass_get_insight_endpoint` (last user, roles, device profile). When `access_check` shows ClearPass's `access_tracker` as `set`, also run `clearpass_access_tracker` (`mac` or `username`; add `status: "REJECT"` for failures, widen `hours` if the event is older than a day): it returns each authentication's login status, service, roles, enforcement profiles, error code with its text and alerts, which is the reason. Done when the latest decision's roles and profile, plus the failure reason (or the reason it is unavailable), are in hand.
6. **Answer.** A timestamped timeline across sources, the root cause with its evidence, and the one next action. Name every source that was unavailable, empty or stale.

ClearPass's REST API holds active accounting sessions, not the full Access Tracker. The reason comes from `clearpass_access_tracker` (or the `access_tracker` part of `clearpass_why_denied`) when its database login is set: quote the error code text and alerts as they are. When that login is missing (the tool answers `login_missing` and names `CLEARPASS_DB_PASSWORD`) or it can't reach the database, say the reason is unknown, say why, and point the user to Access Tracker in the ClearPass UI for that MAC; never infer it.

Steps 1 to 5 only read. Changing anything (a NAC rule, a port, a disconnect) is a separate request the user approves.
