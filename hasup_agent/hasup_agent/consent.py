"""Local maintenance consent (W4.5).

No command is ever executed unless the local consent is active. The consent is a
**timed window**: switching it on opens a window of ``consent_window_hours`` (12 by
default) after which the agent closes it again by itself.

Two sources, in order of priority:

1. **Home Assistant helper entity** ``input_boolean.hasup_maintenance_consent``
   (entity id configurable). The customer owns it: it can be put on a dashboard, driven
   by an automation, etc. The agent polls its state through the Home Assistant API and,
   when the window expires, calls ``input_boolean.turn_off`` so what the customer sees
   matches what the agent enforces.
2. **Add-on option** ``consent_default`` when the helper does not exist. The window then
   starts when the option is switched from ``false`` to ``true`` (the Supervisor
   restarts the add-on when options change, and the agent stores the previous value in
   ``/data``, so a restart alone does not re-open a window).

In both cases the activation instant is persisted in ``/data``: restarting the add-on
can never extend an open window, and the window survives a reboot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .state import AgentState
from .supervisor import SupervisorClient, SupervisorError

logger = logging.getLogger(__name__)

ConsentListener = Callable[["ConsentStatus"], Awaitable[None]]


@dataclass(frozen=True)
class ConsentStatus:
    enabled: bool
    expires_at: datetime | None
    source: str  # "entity" | "option" | "unavailable"

    def is_active(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.expires_at is None:
            return True
        return now < self.expires_at


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class ConsentManager:
    """Tracks the consent state and enforces the expiry of the maintenance window."""

    def __init__(
        self,
        client: SupervisorClient,
        state: AgentState,
        *,
        entity_id: str,
        window_hours: int = 12,
        option_default: bool = False,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._state = state
        self._entity_id = entity_id
        self._window = timedelta(hours=window_hours)
        self._option_default = option_default
        self._clock = clock
        self._status = ConsentStatus(enabled=False, expires_at=None, source="unavailable")
        self._previous_entity_on: bool | None = None
        self._listeners: list[ConsentListener] = []

    # ------------------------------------------------------------- public

    @property
    def status(self) -> ConsentStatus:
        return self._status

    def add_listener(self, listener: ConsentListener) -> None:
        self._listeners.append(listener)

    def is_active(self) -> bool:
        """Whether maintenance is allowed *right now* (re-checks the expiry)."""
        return self._status.is_active(self._clock())

    async def refresh(self) -> ConsentStatus:
        """Re-read the consent source, enforce expiry, notify listeners on change."""
        status = await self._evaluate()
        if status != self._status:
            previous = self._status
            self._status = status
            logger.info(
                "consent changed: %s -> %s (source=%s, expires_at=%s)",
                previous.enabled,
                status.enabled,
                status.source,
                status.expires_at.isoformat() if status.expires_at else None,
            )
            for listener in self._listeners:
                await listener(status)
        else:
            self._status = status
        return status

    # ----------------------------------------------------------- internals

    async def _evaluate(self) -> ConsentStatus:
        try:
            entity = await self._read_entity()
        except SupervisorError:
            return ConsentStatus(enabled=False, expires_at=None, source="unavailable")
        if entity is not None:
            return await self._evaluate_entity(entity)
        return self._evaluate_option()

    async def _read_entity(self) -> dict[str, object] | None:
        try:
            return await self._client.ha_state(self._entity_id)
        except SupervisorError as exc:
            logger.warning("consent entity %s unreadable: %s", self._entity_id, exc)
            raise

    async def _evaluate_entity(self, entity: dict[str, object]) -> ConsentStatus:
        is_on = str(entity.get("state", "")).lower() == "on"
        last_changed = _parse_datetime(entity.get("last_changed"))
        now = self._clock()

        if not is_on:
            if self._state.consent_activated_at is not None:
                self._state.set_consent_activated_at(None)
            self._previous_entity_on = False
            return ConsentStatus(enabled=False, expires_at=None, source="entity")

        activated_at = self._activation_instant(last_changed, now)
        expires_at = activated_at + self._window
        self._previous_entity_on = True

        if now >= expires_at:
            logger.info("consent window expired, switching %s off", self._entity_id)
            await self._turn_entity_off()
            self._state.set_consent_activated_at(None)
            self._previous_entity_on = False
            return ConsentStatus(enabled=False, expires_at=None, source="entity")

        return ConsentStatus(enabled=True, expires_at=expires_at, source="entity")

    def _activation_instant(self, last_changed: datetime | None, now: datetime) -> datetime:
        """Instant the current window started; never later than what is persisted.

        Home Assistant restores ``input_boolean`` states on restart, which resets
        ``last_changed``; taking the earliest known instant prevents a Home Assistant or
        add-on restart from silently extending an open maintenance window.
        """
        stored = self._state.consent_activated_at
        if self._previous_entity_on is False:
            # Off -> on transition observed by the agent: this is a fresh window.
            activated_at = last_changed or now
        elif stored is not None:
            activated_at = min(stored, last_changed) if last_changed else stored
        else:
            activated_at = last_changed or now
        if stored != activated_at:
            self._state.set_consent_activated_at(activated_at)
        return activated_at

    async def _turn_entity_off(self) -> None:
        domain = self._entity_id.split(".", 1)[0]
        try:
            await self._client.ha_call_service(domain, "turn_off", {"entity_id": self._entity_id})
        except SupervisorError as exc:
            logger.warning("could not switch %s off: %s", self._entity_id, exc)

    def _evaluate_option(self) -> ConsentStatus:
        now = self._clock()
        if not self._option_default:
            if self._state.consent_activated_at is not None:
                self._state.set_consent_activated_at(None)
            return ConsentStatus(enabled=False, expires_at=None, source="option")

        activated_at = self._state.consent_activated_at
        if activated_at is None:
            activated_at = now
            self._state.set_consent_activated_at(activated_at)

        expires_at = activated_at + self._window
        if now >= expires_at:
            return ConsentStatus(enabled=False, expires_at=None, source="option")
        return ConsentStatus(enabled=True, expires_at=expires_at, source="option")


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
