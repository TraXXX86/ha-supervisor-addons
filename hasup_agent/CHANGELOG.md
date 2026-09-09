# Changelog

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
