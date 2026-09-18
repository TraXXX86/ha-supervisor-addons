# HA Supervisor Agent

Version 0.4.0 requires the matching server with migration 0007; upgrade the server
first. Backup observations now include completeness, known copies and optional
content details (20 newest archives, inventory capped at 100). Unsupported details
remain unknown. Supervisor visibility does not guarantee all cloud copies are known.
No new creation, deletion or restoration action is introduced.

This add-on connects your Home Assistant instance to the HA Supervisor supervision
platform. It reports the health of the box (versions, CPU, memory, disk, temperature,
add-ons, integrations, automations, available updates) and executes the maintenance
actions your provider triggers, **only while you have given your consent**.

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `server_url` | *(empty)* | Base URL of the supervision server, e.g. `https://supervision.example.com`. |
| `enroll_code` | *(empty)* | Single-use enrollment code given by your provider. Used once, then the add-on stores its own device token. |
| `consent_default` | `false` | Fallback consent when the helper entity does not exist (see below). |
| `consent_window_hours` | `12` | Length of a maintenance window, in hours. The consent switches itself off at the end. |
| `consent_entity_id` | `input_boolean.hasup_maintenance_consent` | Entity used as the consent switch. |
| `log_events_enabled` | `true` | Report Home Assistant errors to the supervision server. |
| `log_min_level` | `error` | Lowest level reported: `warning`, `error` or `critical`. |
| `log_level` | `info` | Verbosity of the add-on's own logs. |
| `tunnel_enabled` | `true` | Allow temporary UI tunnels only while local maintenance consent is active (each session is logged). |
| `tunnel_target` | `http://homeassistant.local.hass.io:8123` | Local address the tunnel connects to. |

## Maintenance consent

**No remote action is possible while the consent is inactive.** Backups, restarts and
updates are all refused with `refused_no_consent` and the refusal is recorded on the
server.

Recommended setup: create a helper (Settings > Devices & services > Helpers > Toggle)
named `hasup_maintenance_consent`, which gives the entity
`input_boolean.hasup_maintenance_consent`. Switch it on to open a maintenance window;
the agent switches it off again automatically after `consent_window_hours`. Put it on a
dashboard to see the current state at a glance.

If the helper does not exist, the agent falls back to the `consent_default` option: the
window opens when the option goes from `false` to `true` and closes automatically after
`consent_window_hours` (restarting the add-on does not reopen it).

## What the add-on sends

Operational data includes versions, resource usage, disk categories and immediate
subdirectory names and sizes, backup names, dates, sizes and primary locations,
add-on and integration names, automation count, and Home Assistant error messages.
Directory and backup names can contain information chosen by the user. File contents,
backup contents and entity state history are not collected. The tunnel is opened at your
provider's request and closed automatically at the end of the session.

## Troubleshooting

- *"not enrolled and no enroll_code configured"*: paste the code from your provider into
  the add-on configuration and restart the add-on.
- *"enroll code ... was already used"*: ask for a new code; a code can only be used once.
- *"relay refused the device token"*: the device was revoked on the server side; ask for
  a new enrollment code.

## Version 0.2.0

Upgrade the central backend to protocol 2 before installing this version. Tunnels
require a local consent window and close on expiry or consent withdrawal.
Unavailable measurements are shown as unknown instead of zero. Inventory sections
retain their last successful values and timestamp during a collection failure.

## Version 0.2.1

Upgrade the backend to the matching inventory-versions revision **before the add-on**.
The new inventory field is rejected by earlier backends, including the initial 0.2.0
backend. No additional database migration is needed if migration 0004 is applied.

Core, OS and Supervisor versions are now refreshed with each inventory (every five
minutes by default), including updates performed directly in Home Assistant.
After a successful remote update or restart, the agent collects immediately and
retries every 15 seconds for two minutes. The device page polls every 30 seconds.
An API outage preserves the last known version until a fresh reading is available.

## Version 0.3.0

Upgrade the central backend through migration 0005 before installing this version.
The agent now collects the data-disk breakdown at connection time and every six hours.
Only the Supervisor's top-level categories are sent; individual file names are never
collected. Older Supervisor versions are reported as unavailable without affecting the
usual CPU, memory and disk-total telemetry.

## Version 0.3.1

Upgrade the matching server through migration 0006 before installing the add-on:
older strict parsers reject the new storage fields. Collection runs on first
connection and once per hour, with a 120-second disk request timeout. Reconnecting
does not trigger another scan before the interval expires.

The Storage tab shows categories, up to twenty largest immediate subdirectories per
category, and the latest 100 backups exposed by Supervisor. Cloud-only backups may
not be listed by that API. Backup metadata is collected independently of disk usage;
the server keeps the last successful list during an outage. No filesystem mount,
SSH access or deletion permission is added. Individual file sizes, including SQLite,
are not available from this directory API.
