"""Protocol-wide constants for the agent <-> server WebSocket protocol."""

PROTOCOL_VERSION = 2

# WebSocket path exposed by the relay (and by tools/relay-stub).
RELAY_PATH = "/relay/v1"

# Timing rules (seconds). See docs/protocol.md.
HEARTBEAT_INTERVAL_S = 30
TELEMETRY_INTERVAL_S = 60
INVENTORY_INTERVAL_S = 6 * 3600
# A device is marked offline after 2 missed heartbeats plus a grace period.
OFFLINE_AFTER_S = 75

# Enrollment codes are single-use and expire after 24 hours.
ENROLL_CODE_TTL_S = 24 * 3600
