# Changelog

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
