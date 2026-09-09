"""Pydantic v2 models for every message of the agent <-> server protocol (v1).

Every WebSocket frame is a JSON object with the common envelope::

    { "type": "...", "id": "<uuid>", "ts": "<ISO-8601>", "payload": { ... } }

Each concrete message class binds a ``type`` literal to its typed payload.
Use :func:`parse_message` to deserialize an incoming frame into the right
class, and ``model_dump_json()`` on any message to serialize it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .constants import (
    HEARTBEAT_INTERVAL_S,
    PROTOCOL_VERSION,
    TELEMETRY_INTERVAL_S,
)
from .enums import (
    CommandKind,
    CommandResultStatus,
    EventLevel,
    MessageType,
    TunnelCloseReason,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class _ProtocolModel(BaseModel):
    """Base for all protocol models: strict about unknown fields."""

    model_config = ConfigDict(extra="forbid")


class BaseMessage(_ProtocolModel):
    """Common envelope fields shared by every message."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    ts: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Payloads: agent -> server
# --------------------------------------------------------------------------


class HelloPayload(_ProtocolModel):
    protocol_version: int = PROTOCOL_VERSION
    agent_version: str
    ha_core: str | None = None
    ha_os: str | None = None
    ha_supervisor: str | None = None
    machine: str | None = None
    arch: str | None = None


class HeartbeatPayload(_ProtocolModel):
    uptime_s: int = Field(ge=0)


class TelemetryPayload(_ProtocolModel):
    cpu_pct: float = Field(ge=0, le=100)
    mem_used_mb: float = Field(ge=0)
    mem_total_mb: float = Field(gt=0)
    disk_used_gb: float = Field(ge=0)
    disk_total_gb: float = Field(gt=0)
    temp_c: float | None = None


class AddonInfo(_ProtocolModel):
    slug: str
    name: str
    version: str
    update_available: bool = False


class UpdateAvailability(_ProtocolModel):
    core: bool = False
    os: bool = False
    supervisor: bool = False


class InventoryPayload(_ProtocolModel):
    addons: list[AddonInfo] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    hacs: list[str] = Field(default_factory=list)
    automation_count: int = Field(default=0, ge=0)
    update_available: UpdateAvailability = Field(default_factory=UpdateAvailability)


class EventPayload(_ProtocolModel):
    level: EventLevel
    source: str
    message: str


class ConsentStatePayload(_ProtocolModel):
    enabled: bool
    expires_at: datetime | None = None


class CommandResultPayload(_ProtocolModel):
    command_id: uuid.UUID
    status: CommandResultStatus
    output: str | None = None


class TunnelDataPayload(_ProtocolModel):
    stream_id: uuid.UUID
    seq: int = Field(ge=0)
    # Chunk of the proxied byte stream, base64-encoded.
    data: str


class TunnelClosedPayload(_ProtocolModel):
    stream_id: uuid.UUID
    reason: TunnelCloseReason = TunnelCloseReason.TARGET_CLOSED


# --------------------------------------------------------------------------
# Payloads: server -> agent
# --------------------------------------------------------------------------


class AgentConfig(_ProtocolModel):
    telemetry_interval_s: int = TELEMETRY_INTERVAL_S
    heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S


class HelloAckPayload(_ProtocolModel):
    server_time: datetime
    config: AgentConfig = Field(default_factory=AgentConfig)
    # Oldest agent version still accepted; lets the server flag outdated agents.
    min_agent_version: str | None = None


class CommandPayload(_ProtocolModel):
    command_id: uuid.UUID
    kind: CommandKind
    params: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime


class TunnelOpenPayload(_ProtocolModel):
    stream_id: uuid.UUID
    # Base URL the agent must proxy to, e.g. "http://homeassistant:8123".
    target: str


class TunnelClosePayload(_ProtocolModel):
    stream_id: uuid.UUID
    reason: TunnelCloseReason = TunnelCloseReason.SERVER_REQUEST


class TokenRevokedPayload(_ProtocolModel):
    """Empty payload: the agent must erase its token and re-enroll."""


# --------------------------------------------------------------------------
# Messages: agent -> server
# --------------------------------------------------------------------------


class HelloMessage(BaseMessage):
    type: Literal[MessageType.HELLO] = MessageType.HELLO
    payload: HelloPayload


class HeartbeatMessage(BaseMessage):
    type: Literal[MessageType.HEARTBEAT] = MessageType.HEARTBEAT
    payload: HeartbeatPayload


class TelemetryMessage(BaseMessage):
    type: Literal[MessageType.TELEMETRY] = MessageType.TELEMETRY
    payload: TelemetryPayload


class InventoryMessage(BaseMessage):
    type: Literal[MessageType.INVENTORY] = MessageType.INVENTORY
    payload: InventoryPayload


class EventMessage(BaseMessage):
    type: Literal[MessageType.EVENT] = MessageType.EVENT
    payload: EventPayload


class ConsentStateMessage(BaseMessage):
    type: Literal[MessageType.CONSENT_STATE] = MessageType.CONSENT_STATE
    payload: ConsentStatePayload


class CommandResultMessage(BaseMessage):
    type: Literal[MessageType.COMMAND_RESULT] = MessageType.COMMAND_RESULT
    payload: CommandResultPayload


class TunnelDataMessage(BaseMessage):
    """Tunnel byte chunk; flows in both directions."""

    type: Literal[MessageType.TUNNEL_DATA] = MessageType.TUNNEL_DATA
    payload: TunnelDataPayload


class TunnelClosedMessage(BaseMessage):
    type: Literal[MessageType.TUNNEL_CLOSED] = MessageType.TUNNEL_CLOSED
    payload: TunnelClosedPayload


# --------------------------------------------------------------------------
# Messages: server -> agent
# --------------------------------------------------------------------------


class HelloAckMessage(BaseMessage):
    type: Literal[MessageType.HELLO_ACK] = MessageType.HELLO_ACK
    payload: HelloAckPayload


class CommandMessage(BaseMessage):
    type: Literal[MessageType.COMMAND] = MessageType.COMMAND
    payload: CommandPayload


class TunnelOpenMessage(BaseMessage):
    type: Literal[MessageType.TUNNEL_OPEN] = MessageType.TUNNEL_OPEN
    payload: TunnelOpenPayload


class TunnelCloseMessage(BaseMessage):
    type: Literal[MessageType.TUNNEL_CLOSE] = MessageType.TUNNEL_CLOSE
    payload: TunnelClosePayload


class TokenRevokedMessage(BaseMessage):
    type: Literal[MessageType.TOKEN_REVOKED] = MessageType.TOKEN_REVOKED
    payload: TokenRevokedPayload = Field(default_factory=TokenRevokedPayload)


# --------------------------------------------------------------------------
# Discriminated unions and parsing helpers
# --------------------------------------------------------------------------

AgentToServerMessage = Annotated[
    Union[  # noqa: UP007 - Annotated discriminated union requires Union syntax
        HelloMessage,
        HeartbeatMessage,
        TelemetryMessage,
        InventoryMessage,
        EventMessage,
        ConsentStateMessage,
        CommandResultMessage,
        TunnelDataMessage,
        TunnelClosedMessage,
    ],
    Field(discriminator="type"),
]

ServerToAgentMessage = Annotated[
    Union[  # noqa: UP007
        HelloAckMessage,
        CommandMessage,
        TunnelOpenMessage,
        TunnelDataMessage,
        TunnelCloseMessage,
        TokenRevokedMessage,
    ],
    Field(discriminator="type"),
]

AnyMessage = Annotated[
    Union[  # noqa: UP007
        HelloMessage,
        HeartbeatMessage,
        TelemetryMessage,
        InventoryMessage,
        EventMessage,
        ConsentStateMessage,
        CommandResultMessage,
        TunnelDataMessage,
        TunnelClosedMessage,
        HelloAckMessage,
        CommandMessage,
        TunnelOpenMessage,
        TunnelCloseMessage,
        TokenRevokedMessage,
    ],
    Field(discriminator="type"),
]

_any_message_adapter: TypeAdapter[Any] = TypeAdapter(AnyMessage)
_agent_to_server_adapter: TypeAdapter[Any] = TypeAdapter(AgentToServerMessage)
_server_to_agent_adapter: TypeAdapter[Any] = TypeAdapter(ServerToAgentMessage)


def parse_message(raw: str | bytes | dict[str, Any]) -> BaseMessage:
    """Parse a JSON frame (or an already-decoded dict) into a typed message.

    Raises ``pydantic.ValidationError`` on unknown type or invalid payload.
    """
    if isinstance(raw, dict):
        result = _any_message_adapter.validate_python(raw)
    else:
        result = _any_message_adapter.validate_json(raw)
    assert isinstance(result, BaseMessage)
    return result


def parse_agent_message(raw: str | bytes | dict[str, Any]) -> BaseMessage:
    """Parse a frame, accepting only agent -> server message types."""
    if isinstance(raw, dict):
        result = _agent_to_server_adapter.validate_python(raw)
    else:
        result = _agent_to_server_adapter.validate_json(raw)
    assert isinstance(result, BaseMessage)
    return result


def parse_server_message(raw: str | bytes | dict[str, Any]) -> BaseMessage:
    """Parse a frame, accepting only server -> agent message types."""
    if isinstance(raw, dict):
        result = _server_to_agent_adapter.validate_python(raw)
    else:
        result = _server_to_agent_adapter.validate_json(raw)
    assert isinstance(result, BaseMessage)
    return result
