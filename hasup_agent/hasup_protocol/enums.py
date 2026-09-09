"""Enumerations of the agent <-> server protocol."""

from enum import StrEnum


class MessageType(StrEnum):
    """Discriminator carried in the ``type`` field of every envelope."""

    # agent -> server
    HELLO = "hello"
    HEARTBEAT = "heartbeat"
    TELEMETRY = "telemetry"
    INVENTORY = "inventory"
    EVENT = "event"
    CONSENT_STATE = "consent_state"
    COMMAND_RESULT = "command_result"
    TUNNEL_DATA = "tunnel_data"  # both directions
    TUNNEL_CLOSED = "tunnel_closed"

    # server -> agent
    HELLO_ACK = "hello_ack"
    COMMAND = "command"
    TUNNEL_OPEN = "tunnel_open"
    TUNNEL_CLOSE = "tunnel_close"
    TOKEN_REVOKED = "token_revoked"


class CommandKind(StrEnum):
    """Maintenance actions the server can push to an agent."""

    BACKUP_CREATE = "backup_create"
    CORE_RESTART = "core_restart"
    CORE_UPDATE = "core_update"
    OS_UPDATE = "os_update"
    ADDON_UPDATE = "addon_update"
    ADDON_RESTART = "addon_restart"


class CommandResultStatus(StrEnum):
    """Terminal status reported by the agent in ``command_result``."""

    SUCCESS = "success"
    FAILED = "failed"
    REFUSED_NO_CONSENT = "refused_no_consent"
    TIMEOUT = "timeout"


class EventLevel(StrEnum):
    """Severity of a log event detected by the agent."""

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class TunnelCloseReason(StrEnum):
    """Why a tunnel stream was closed."""

    CLIENT_CLOSED = "client_closed"
    TARGET_CLOSED = "target_closed"
    TIMEOUT = "timeout"
    ERROR = "error"
    SERVER_REQUEST = "server_request"
