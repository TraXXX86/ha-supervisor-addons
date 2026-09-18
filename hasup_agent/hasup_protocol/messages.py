"""Pydantic v2 models for every message of the agent <-> server protocol (v2).

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

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

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

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BaseMessage(_ProtocolModel):
    """Common envelope fields shared by every message."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    ts: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Payloads: agent -> server
# --------------------------------------------------------------------------


class HelloPayload(_ProtocolModel):
    protocol_version: int = PROTOCOL_VERSION
    capabilities: list[str] = Field(default_factory=list)
    agent_version: str
    ha_core: str | None = None
    ha_os: str | None = None
    ha_supervisor: str | None = None
    machine: str | None = None
    arch: str | None = None


class HeartbeatPayload(_ProtocolModel):
    uptime_s: int = Field(ge=0)


class CollectionHealth(_ProtocolModel):
    status: Literal["ok", "unavailable", "unverified"] = "unverified"
    last_success_at: datetime | None = None


class TelemetryPayload(_ProtocolModel):
    collection: dict[str, CollectionHealth] = Field(default_factory=dict)
    cpu_pct: float | None = Field(default=None, ge=0, le=100)
    mem_used_mb: float | None = Field(default=None, ge=0)
    mem_total_mb: float | None = Field(default=None, gt=0)
    disk_used_gb: float | None = Field(default=None, ge=0)
    disk_total_gb: float | None = Field(default=None, gt=0)
    temp_c: float | None = None


class DiskUsageDirectory(_ProtocolModel):
    """A measured directory; paths are labels, never commands or file contents."""

    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    used_bytes: int = Field(ge=0)


class DiskUsageItem(DiskUsageDirectory):
    children: list[DiskUsageDirectory] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_children(self) -> DiskUsageItem:
        if len({item.id for item in self.children}) != len(self.children):
            raise ValueError("duplicate disk directory")
        if sum(item.used_bytes for item in self.children) > self.used_bytes:
            raise ValueError("directory breakdown exceeds its parent")
        return self


class BackupLocation(_ProtocolModel):
    id: str | None = Field(default=None, max_length=200)
    kind: Literal["local", "remote"]
    size_bytes: int | None = Field(default=None, ge=0)
    protected: bool | None = None


class StorageBackup(_ProtocolModel):
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    date: datetime
    size_bytes: int | None = Field(default=None, ge=0)
    location: str | None = Field(default=None, max_length=200)
    # One logical archive can be exposed from several Supervisor locations.
    # ``location`` remains the primary location for protocol v2 consumers.
    locations: list[str | None] = Field(default_factory=list, max_length=20)
    copies: list[BackupLocation] = Field(default_factory=list, max_length=20)
    type: Literal["full", "partial"]
    protected: bool | None = None
    home_assistant_version: str | None = Field(default=None, max_length=50)
    database_included: bool | None = None
    addons: list[str] | None = Field(default=None, max_length=200)
    folders: list[str] | None = Field(default=None, max_length=100)


class StorageBackups(_ProtocolModel):
    collection: CollectionHealth = Field(default_factory=CollectionHealth)
    items: list[StorageBackup] = Field(default_factory=list, max_length=100)
    total_count: int = Field(default=0, ge=0)
    # Completeness is independent from transport health.  An ``ok`` collection
    # may still be truncated and must then never drive disappearance events.
    complete: bool = False
    # The Supervisor API covers local and configured Supervisor locations; it
    # cannot attest to copies made by unrelated cloud integrations.
    coverage: Literal["unknown", "supervisor_visible"] = "unknown"
    # The Supervisor intentionally omits some cloud-only archives.  This flag
    # remains false unless a future source can attest to all remote copies.
    remote_coverage_complete: bool = False

    @model_validator(mode="after")
    def validate_inventory(self) -> StorageBackups:
        if len({item.slug for item in self.items}) != len(self.items):
            raise ValueError("duplicate backup archive")
        if self.complete and self.total_count != len(self.items):
            raise ValueError("complete backup inventory must contain every archive")
        return self


class DiskUsagePayload(_ProtocolModel):
    """Detailed data-disk usage; unavailable samples keep collection health."""

    collection: CollectionHealth = Field(default_factory=CollectionHealth)
    total_bytes: int | None = Field(default=None, gt=0)
    used_bytes: int | None = Field(default=None, ge=0)
    children: list[DiskUsageItem] = Field(default_factory=list, max_length=100)
    backups: StorageBackups = Field(default_factory=StorageBackups)

    @model_validator(mode="after")
    def validate_totals(self) -> DiskUsagePayload:
        if len({item.id for item in self.children}) != len(self.children):
            raise ValueError("duplicate disk category")
        available = self.total_bytes is not None and self.used_bytes is not None
        if (self.total_bytes is None) != (self.used_bytes is None):
            raise ValueError("disk total and used bytes must be reported together")
        if available and self.used_bytes is not None and self.total_bytes is not None:
            if self.used_bytes > self.total_bytes:
                raise ValueError("disk used bytes cannot exceed total bytes")
            if sum(item.used_bytes for item in self.children) > self.used_bytes:
                raise ValueError("disk breakdown cannot exceed used bytes")
        if self.children and not available:
            raise ValueError("disk breakdown requires available totals")
        if self.collection.status == "ok" and not available:
            raise ValueError("successful disk collection requires totals")
        return self


class AddonInfo(_ProtocolModel):
    slug: str
    name: str
    version: str
    update_available: bool | None = None


class UpdateAvailability(_ProtocolModel):
    core: bool | None = None
    os: bool | None = None
    supervisor: bool | None = None


class InstalledVersions(_ProtocolModel):
    """Versions observed in this sample; null means unavailable, never uninstalled."""

    core: str | None = Field(default=None, max_length=50)
    os: str | None = Field(default=None, max_length=50)
    supervisor: str | None = Field(default=None, max_length=50)


class InventoryPayload(_ProtocolModel):
    versions: InstalledVersions = Field(default_factory=InstalledVersions)
    collection: dict[str, CollectionHealth] = Field(default_factory=dict)
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
    expires_at: datetime | None = None


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


class DiskUsageMessage(BaseMessage):
    type: Literal[MessageType.DISK_USAGE] = MessageType.DISK_USAGE
    payload: DiskUsagePayload


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
        DiskUsageMessage,
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
        DiskUsageMessage,
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
