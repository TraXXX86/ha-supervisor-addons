"""Agent configuration.

Two layers, in increasing order of priority:

1. the add-on options file (``/data/options.json``), written by the Home Assistant
   Supervisor from the user-facing add-on configuration page;
2. environment variables prefixed with ``HASUP_``, used for local development and
   for the integration tests (no Supervisor around).

``SUPERVISOR_TOKEN`` is injected by the Supervisor into every add-on that declares
``hassio_api``/``homeassistant_api``; it is the bearer token for both the Supervisor
API (``http://supervisor/``) and the Home Assistant API (``http://supervisor/core/api/``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_OPTIONS_PATH = Path("/data/options.json")
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_SUPERVISOR_URL = "http://supervisor"
DEFAULT_CONSENT_ENTITY_ID = "input_boolean.hasup_maintenance_consent"
DEFAULT_TUNNEL_TARGET = "http://homeassistant.local.hass.io:8123"


class AgentSettings(BaseModel):
    """Effective configuration of a running agent."""

    model_config = ConfigDict(extra="forbid")

    # --- central server ---------------------------------------------------
    server_url: str = ""
    enroll_code: str = ""
    # TLS verification of the central server; disabled only for local test setups.
    verify_tls: bool = True

    # --- Home Assistant side ---------------------------------------------
    supervisor_url: str = DEFAULT_SUPERVISOR_URL
    supervisor_token: str = ""
    data_dir: Path = DEFAULT_DATA_DIR

    # --- consent (W4.5) ---------------------------------------------------
    consent_entity_id: str = DEFAULT_CONSENT_ENTITY_ID
    consent_default: bool = False
    consent_window_hours: int = Field(default=12, ge=1, le=168)
    consent_poll_interval_s: float = Field(default=15.0, gt=0)

    # --- log surveillance (W4.3) -----------------------------------------
    log_events_enabled: bool = True
    log_poll_interval_s: float = Field(default=60.0, gt=0)
    log_lines_per_poll: int = Field(default=200, ge=10, le=5000)
    log_event_max_per_hour: int = Field(default=20, ge=1)
    log_dedup_ttl_s: int = Field(default=1800, ge=60)
    # Lowest Home Assistant log level reported as an event: warning, error or critical.
    log_min_level: str = "error"

    # --- tunnel (W4.6) ----------------------------------------------------
    tunnel_enabled: bool = True
    tunnel_target: str = DEFAULT_TUNNEL_TARGET

    # --- misc -------------------------------------------------------------
    log_level: str = "info"
    # Intervals are normally imposed by the server in hello_ack; these are the
    # values used until the first hello_ack of a connection. Setting one of them
    # explicitly (option or environment variable) locks it: the server value is then
    # ignored, which is what the tests and local runs use to speed things up.
    heartbeat_interval_s: int = Field(default=30, ge=1)
    telemetry_interval_s: int = Field(default=60, ge=1)
    intervals_locked: bool = False
    # WebSocket ping interval. A relay that dies without closing the TCP connection is
    # detected after roughly two ping intervals, which must stay below the 75 s the
    # server waits before marking the device offline.
    ws_ping_interval_s: float = Field(default=25.0, gt=0)
    inventory_interval_s: int = Field(default=6 * 3600, ge=60)
    # Detailed disk usage walks the data disk and must stay much less frequent than telemetry.
    disk_usage_interval_s: int = Field(default=3600, ge=300)
    # How often the inventory is recomputed to detect a change between two full sends.
    inventory_check_interval_s: float = Field(default=300.0, gt=0)
    # Simulated host metrics source, overridable so tests can point at a fixture tree.
    procfs_root: Path = Path("/proc")
    sysfs_root: Path = Path("/sys")

    @property
    def enroll_url(self) -> str:
        return f"{self.server_url.rstrip('/')}/api/v1/enroll"

    @property
    def relay_url(self) -> str:
        """WebSocket URL of the relay, derived from the HTTP(S) server URL."""
        base = self.server_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/relay/v1"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "hasup-agent-state.json"


_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}

# Option name -> (type, environment variable). The option name is what the user sees
# in the add-on configuration page.
_FIELD_ENV: dict[str, str] = {
    "server_url": "HASUP_SERVER_URL",
    "enroll_code": "HASUP_ENROLL_CODE",
    "verify_tls": "HASUP_VERIFY_TLS",
    "supervisor_url": "HASUP_SUPERVISOR_URL",
    "supervisor_token": "SUPERVISOR_TOKEN",
    "data_dir": "HASUP_DATA_DIR",
    "consent_entity_id": "HASUP_CONSENT_ENTITY_ID",
    "consent_default": "HASUP_CONSENT_DEFAULT",
    "consent_window_hours": "HASUP_CONSENT_WINDOW_HOURS",
    "consent_poll_interval_s": "HASUP_CONSENT_POLL_INTERVAL_S",
    "log_events_enabled": "HASUP_LOG_EVENTS_ENABLED",
    "log_poll_interval_s": "HASUP_LOG_POLL_INTERVAL_S",
    "log_lines_per_poll": "HASUP_LOG_LINES_PER_POLL",
    "log_event_max_per_hour": "HASUP_LOG_EVENT_MAX_PER_HOUR",
    "log_dedup_ttl_s": "HASUP_LOG_DEDUP_TTL_S",
    "tunnel_enabled": "HASUP_TUNNEL_ENABLED",
    "tunnel_target": "HASUP_TUNNEL_TARGET",
    "log_level": "HASUP_LOG_LEVEL",
    "heartbeat_interval_s": "HASUP_HEARTBEAT_INTERVAL_S",
    "telemetry_interval_s": "HASUP_TELEMETRY_INTERVAL_S",
    "inventory_interval_s": "HASUP_INVENTORY_INTERVAL_S",
    "disk_usage_interval_s": "HASUP_DISK_USAGE_INTERVAL_S",
    "inventory_check_interval_s": "HASUP_INVENTORY_CHECK_INTERVAL_S",
    "ws_ping_interval_s": "HASUP_WS_PING_INTERVAL_S",
    "log_min_level": "HASUP_LOG_MIN_LEVEL",
    "procfs_root": "HASUP_PROCFS_ROOT",
    "sysfs_root": "HASUP_SYSFS_ROOT",
}


def _coerce(field_name: str, raw: str) -> Any:
    annotation = AgentSettings.model_fields[field_name].annotation
    if annotation is bool:
        lowered = raw.strip().lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        raise ValueError(f"{field_name}: expected a boolean, got {raw!r}")
    return raw


def load_options_file(path: Path) -> dict[str, Any]:
    """Read the add-on options file; missing or empty file yields no options."""
    if not path.is_file():
        return {}
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return {}
    parsed: Any = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: expected a JSON object")
    # Unknown keys would be rejected by the strict model; the Supervisor only writes
    # keys declared in the add-on schema, so this simply keeps forward compatibility.
    known = set(AgentSettings.model_fields)
    return {key: value for key, value in parsed.items() if key in known}


def load_settings(
    options_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> AgentSettings:
    """Build the effective settings from the options file and the environment."""
    env = dict(os.environ if environ is None else environ)
    path = options_path or Path(env.get("HASUP_OPTIONS_PATH", str(DEFAULT_OPTIONS_PATH)))

    values: dict[str, Any] = load_options_file(path)
    for field_name, env_name in _FIELD_ENV.items():
        raw = env.get(env_name)
        if raw is not None and raw != "":
            values[field_name] = _coerce(field_name, raw)

    if "heartbeat_interval_s" in values or "telemetry_interval_s" in values:
        values["intervals_locked"] = True

    return AgentSettings.model_validate(values)
