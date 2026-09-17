# Changelog

## 0.3.1

Upgrade the matching backend through migration 0006 before the add-on.

- Collect storage every hour, with the twenty largest immediate subdirectories per category.
- Report the latest 100 Supervisor backups, with dates, sizes and primary locations.
- Track disk and backup collection health independently; preserve previous successful
  measurements on the server when either API is unavailable.

## 0.3.0

Requires the backend migration 0005. Upgrade the server before the add-on.

- Collect the Supervisor data-disk breakdown on connection and every six hours.
- Report collection health when detailed disk usage is unsupported or temporarily
  unavailable.
- Advertise the `disk_usage_v1` capability to matching servers.

## 0.2.1

Upgrade the backend to the matching inventory-versions revision before the add-on.
No additional database migration is needed after 0004.

- Report installed Core, OS and Supervisor versions in every inventory so updates
  are visible without reconnecting the agent.
- Refresh inventory immediately after successful updates/restarts, then every
  15 seconds for two minutes to cover temporary API outages during HA startup.
- Preserve the server's last known versions when a collection is unavailable.

## 0.2.0

Requires the protocol 2 backend and migration 0004. Upgrade the server first.

- Require local consent and an expiry for every tunnel stream; close streams when
  consent expires or is withdrawn. Verify TLS for HTTPS targets and bound buffers.
- Report unavailable CPU/RAM/disk readings as null, with collection health and
  the last successful collection time. Preserve valid zero readings.
- Keep last successful inventory sections during API failures and report unknown
  update status. Attribute HACS updates through HA's entity registry.
- Fix host CPU accounting (guest times) and missing memory fallback data.

## 0.1.0

First version of the HA Supervisor agent add-on.

- Enrollment with a single-use code, device token persisted in `/data`.
- Permanent WebSocket connection to the relay with automatic reconnection
  (exponential backoff with jitter, capped at 5 minutes).
- Telemetry (CPU, memory, disk, temperature), heartbeat and inventory (add-ons,
  integrations, HACS content needing an update, automations, available updates).
- Home Assistant log surveillance with deduplication and rate limiting.
- Consent-gated remote maintenance: backup, core restart, core/OS/add-on updates.
- Optional HTTP tunnel client for on-demand access to the Home Assistant UI.
