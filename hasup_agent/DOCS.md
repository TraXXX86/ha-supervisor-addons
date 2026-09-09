# HA Supervisor Agent

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

Only operational data: versions, resource usage, add-on and integration names,
automation count, and error messages coming from the Home Assistant log. No entity
state, no history, no personal data. The tunnel, when used, is opened at your
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
