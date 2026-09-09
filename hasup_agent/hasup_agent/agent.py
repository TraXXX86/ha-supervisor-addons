"""Agent core (W4.1): asyncio loop, enrollment, relay session, periodic reporting.

Lifecycle::

    load settings -> enroll (once, code -> device token persisted in /data)
      -> connect wss://<server>/relay/v1 (Bearer device token)
        -> hello / hello_ack -> inventory + consent_state
        -> heartbeat (30 s), telemetry (60 s), inventory (6 h or on change),
           log events, consent polling, incoming commands
      -> on disconnection: exponential backoff with jitter (5 min cap) and retry
    SIGTERM/SIGINT -> stop the loops, close the WebSocket, exit 0
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import aiohttp
from hasup_protocol import (
    BaseMessage,
    CommandMessage,
    CommandResultMessage,
    CommandResultPayload,
    CommandResultStatus,
    ConsentStateMessage,
    ConsentStatePayload,
    EventLevel,
    EventMessage,
    EventPayload,
    HeartbeatMessage,
    HeartbeatPayload,
    HelloAckMessage,
    HelloMessage,
    InventoryMessage,
    InventoryPayload,
    TelemetryMessage,
    TokenRevokedMessage,
    TunnelCloseMessage,
    TunnelDataMessage,
    TunnelOpenMessage,
    parse_server_message,
)
from pydantic import ValidationError

from . import AGENT_VERSION
from .backoff import ExponentialBackoff
from .collectors import Collectors
from .commands import CommandExecutor
from .config import AgentSettings
from .consent import ConsentManager, ConsentStatus
from .enroll import EnrollmentRejected, EnrollmentUnavailable, enroll
from .logwatch import LogWatcher
from .metrics import HostMetricsReader
from .state import AgentState
from .supervisor import SupervisorClient
from .tunnel import TunnelClient

logger = logging.getLogger(__name__)

HELLO_ACK_TIMEOUT_S = 15.0
# Delay between two attempts when the agent has no usable enrollment code.
NO_CODE_RETRY_S = 60.0


class Agent:
    """The whole agent: one instance per process."""

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.state = AgentState(settings.state_path)
        self._stop_event = asyncio.Event()
        self._session_closed = asyncio.Event()
        self._backoff = ExponentialBackoff()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._last_inventory: InventoryPayload | None = None
        self._last_inventory_sent_at: float = 0.0
        self._connected_sessions = 0

        self.client: SupervisorClient | None = None
        self.collectors: Collectors | None = None
        self.consent: ConsentManager | None = None
        self.executor: CommandExecutor | None = None
        self.logwatcher: LogWatcher | None = None
        self.tunnel: TunnelClient | None = None

    # ------------------------------------------------------------- startup

    async def _startup(self) -> None:
        self._session = aiohttp.ClientSession()
        self.client = SupervisorClient(
            self.settings.supervisor_url,
            self.settings.supervisor_token,
            session=self._session,
        )
        self.collectors = Collectors(
            self.client,
            HostMetricsReader(self.settings.procfs_root, self.settings.sysfs_root),
        )
        self.consent = ConsentManager(
            self.client,
            self.state,
            entity_id=self.settings.consent_entity_id,
            window_hours=self.settings.consent_window_hours,
            option_default=self.settings.consent_default,
        )
        self.consent.add_listener(self._on_consent_change)
        self.executor = CommandExecutor(self.client, self.consent)
        self.logwatcher = LogWatcher(
            self.client,
            lines=self.settings.log_lines_per_poll,
            max_per_hour=self.settings.log_event_max_per_hour,
            dedup_ttl_s=float(self.settings.log_dedup_ttl_s),
            min_level=_parse_level(self.settings.log_min_level),
        )
        self.tunnel = TunnelClient(
            target=self.settings.tunnel_target,
            send=self._send,
            enabled=self.settings.tunnel_enabled,
            consent=self.consent,
        )
        await self.consent.refresh()

    async def _shutdown(self) -> None:
        for task in list(self._command_tasks):
            task.cancel()
        if self._command_tasks:
            await asyncio.gather(*self._command_tasks, return_exceptions=True)
        if self.tunnel is not None:
            await self.tunnel.close_all()
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._session is not None:
            await self._session.close()
        logger.info("agent stopped")

    # ---------------------------------------------------------------- run

    def request_stop(self) -> None:
        """Ask the agent to shut down (signal handler friendly, never blocks)."""
        logger.info("shutdown requested")
        self._stop_event.set()
        self._session_closed.set()

    async def run(self) -> int:
        logger.info("hasup-agent %s starting (server=%s)", AGENT_VERSION, self.settings.server_url)
        if not self.settings.server_url:
            logger.error("no server_url configured; set it in the add-on configuration")
            return 2

        await self._startup()
        try:
            while not self._stop_event.is_set():
                if not self.state.enrolled and not await self._ensure_enrolled():
                    continue
                connected = await self._connect_and_serve()
                if self._stop_event.is_set():
                    break
                if connected:
                    self._backoff.reset()
                delay = self._backoff.next_delay()
                logger.info("reconnecting in %.1fs", delay)
                await self._sleep(delay, honour_session=False)
        finally:
            await self._shutdown()
        return 0

    # --------------------------------------------------------- enrollment

    async def _ensure_enrolled(self) -> bool:
        code = self.settings.enroll_code.strip()
        if not code:
            logger.error(
                "not enrolled and no enroll_code configured; add the code from the "
                "dashboard to the add-on configuration"
            )
            await self._sleep(NO_CODE_RETRY_S, honour_session=False)
            return False
        if code == self.state.used_enroll_code:
            logger.error(
                "enroll code %s... was already used and the device token is gone; "
                "generate a new code in the dashboard",
                code[:6],
            )
            await self._sleep(NO_CODE_RETRY_S, honour_session=False)
            return False

        assert self._session is not None
        try:
            result = await enroll(
                self._session,
                self.settings.enroll_url,
                code,
                verify_tls=self.settings.verify_tls,
            )
        except EnrollmentRejected as exc:
            logger.error("%s", exc)
            self.state.set_used_enroll_code(code)
            await self._sleep(NO_CODE_RETRY_S, honour_session=False)
            return False
        except EnrollmentUnavailable as exc:
            logger.warning("%s", exc)
            await self._sleep(self._backoff.next_delay(), honour_session=False)
            return False

        self.state.set_credentials(result.device_id, result.device_token)
        self.state.set_used_enroll_code(code)
        self._backoff.reset()
        logger.info("enrolled successfully as device %s", result.device_id)
        return True

    # ------------------------------------------------------------ session

    async def _connect_and_serve(self) -> bool:
        """Run one relay session; returns True if the session was established."""
        assert self._session is not None
        token = self.state.device_token or ""
        url = self.settings.relay_url
        self._session_closed.clear()
        logger.info("connecting to %s", url)
        try:
            async with self._session.ws_connect(
                url,
                headers={"Authorization": f"Bearer {token}"},
                heartbeat=self.settings.ws_ping_interval_s,
                ssl=self.settings.verify_tls,
            ) as ws:
                self._ws = ws
                self._connected_sessions += 1
                logger.info("connected to relay")
                await self._serve(ws)
                return True
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (401, 403) or exc.status == 4401:
                logger.error("relay refused the device token (HTTP %s)", exc.status)
                self.state.clear_credentials()
            else:
                logger.warning("relay handshake failed: %s", exc)
            return False
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.warning("connection to %s failed: %s", url, exc)
            return False
        finally:
            self._ws = None
            self._session_closed.set()

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        assert self.collectors is not None and self.consent is not None

        await self._send(HelloMessage(payload=await self.collectors.hello_payload()))
        if not await self._await_hello_ack(ws):
            return

        await self._send_inventory(force=True)
        await self._send_consent_state(self.consent.status)

        async with asyncio.TaskGroup() as group:
            group.create_task(self._receive_loop(ws))
            group.create_task(self._close_on_stop(ws))
            group.create_task(self._heartbeat_loop())
            group.create_task(self._telemetry_loop())
            group.create_task(self._inventory_loop())
            group.create_task(self._consent_loop())
            if self.settings.log_events_enabled:
                group.create_task(self._log_loop())

    async def _await_hello_ack(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        try:
            async with asyncio.timeout(HELLO_ACK_TIMEOUT_S):
                async for raw in ws:
                    if raw.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    message = self._parse(raw.data)
                    if isinstance(message, HelloAckMessage):
                        config = message.payload.config
                        if not self.settings.intervals_locked:
                            self.settings.heartbeat_interval_s = config.heartbeat_interval_s
                            self.settings.telemetry_interval_s = config.telemetry_interval_s
                        logger.info(
                            "hello_ack received (heartbeat=%ss telemetry=%ss)",
                            self.settings.heartbeat_interval_s,
                            self.settings.telemetry_interval_s,
                        )
                        return True
                    if message is not None:
                        await self._handle_message(message)
        except TimeoutError:
            logger.warning("no hello_ack within %.0fs, reconnecting", HELLO_ACK_TIMEOUT_S)
        return False

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for raw in ws:
                if raw.type is not aiohttp.WSMsgType.TEXT:
                    continue
                message = self._parse(raw.data)
                if message is not None:
                    await self._handle_message(message)
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("relay connection lost: %s", exc)
        finally:
            logger.info("relay session closed")
            self._session_closed.set()

    async def _close_on_stop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Unblock the receive loop when a shutdown is requested."""
        while await self._sleep(3600.0):
            pass
        if self._stop_event.is_set() and not ws.closed:
            await ws.close()

    def _parse(self, raw: str) -> BaseMessage | None:
        try:
            return parse_server_message(raw)
        except ValidationError as exc:
            logger.error("invalid message from server ignored: %s", exc)
            return None

    async def _handle_message(self, message: BaseMessage) -> None:
        if isinstance(message, CommandMessage):
            task = asyncio.create_task(self._run_command(message))
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)
        elif isinstance(message, TokenRevokedMessage):
            logger.warning("device token revoked by the server, clearing credentials")
            self.state.clear_credentials()
            self._session_closed.set()
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
        elif isinstance(message, TunnelOpenMessage | TunnelDataMessage | TunnelCloseMessage):
            if self.tunnel is not None:
                await self.tunnel.handle(message)
        elif isinstance(message, HelloAckMessage):
            logger.debug("late hello_ack ignored")
        else:
            logger.warning("unexpected message %s ignored", type(message).__name__)

    async def _run_command(self, message: CommandMessage) -> None:
        assert self.executor is not None
        try:
            result = await self.executor.execute(message.payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive: a command must always answer
            logger.exception("unhandled error while executing command")
            result = CommandResultPayload(
                command_id=message.payload.command_id,
                status=CommandResultStatus.FAILED,
                output=f"internal agent error: {exc}",
            )
        await self._send(CommandResultMessage(payload=result))

    # -------------------------------------------------------- periodic tasks

    async def _heartbeat_loop(self) -> None:
        assert self.collectors is not None
        # A first heartbeat and a first telemetry are sent right after hello_ack so the
        # server has fresh data without waiting a full interval.
        while True:
            await self._send(
                HeartbeatMessage(payload=HeartbeatPayload(uptime_s=self.collectors.uptime_s()))
            )
            if not await self._sleep(self.settings.heartbeat_interval_s):
                return

    async def _telemetry_loop(self) -> None:
        assert self.collectors is not None
        while True:
            payload = await self.collectors.telemetry_payload()
            await self._send(TelemetryMessage(payload=payload))
            if not await self._sleep(self.settings.telemetry_interval_s):
                return

    async def _inventory_loop(self) -> None:
        while await self._sleep(self.settings.inventory_check_interval_s):
            await self._send_inventory(force=False)

    async def _consent_loop(self) -> None:
        assert self.consent is not None
        while await self._sleep(self.settings.consent_poll_interval_s):
            await self.consent.refresh()

    async def _log_loop(self) -> None:
        assert self.logwatcher is not None
        while True:
            for event in await self.logwatcher.poll():
                await self._send_event(event)
            if not await self._sleep(self.settings.log_poll_interval_s):
                return

    async def _send_inventory(self, *, force: bool) -> None:
        assert self.collectors is not None
        payload = await self.collectors.inventory_payload()
        now = asyncio.get_running_loop().time()
        changed = payload != self._last_inventory
        due = now - self._last_inventory_sent_at >= self.settings.inventory_interval_s
        if not (force or changed or due):
            return
        await self._send(InventoryMessage(payload=payload))
        self._last_inventory = payload
        self._last_inventory_sent_at = now
        if changed and not force:
            logger.info("inventory change detected and reported")

    async def _send_event(self, payload: EventPayload) -> None:
        await self._send(EventMessage(payload=payload))

    async def _on_consent_change(self, status: ConsentStatus) -> None:
        if not status.is_active(datetime.now(tz=UTC)) and self.tunnel is not None:
            await self.tunnel.close_all()
        await self._send_consent_state(status)

    async def _send_consent_state(self, status: ConsentStatus) -> None:
        await self._send(
            ConsentStateMessage(
                payload=ConsentStatePayload(
                    enabled=status.enabled,
                    expires_at=status.expires_at,
                )
            )
        )

    # ------------------------------------------------------------ plumbing

    async def _send(self, message: Any) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            logger.debug("dropping %s: not connected", getattr(message, "type", "?"))
            return
        frame = message.model_dump_json()
        async with self._send_lock:
            try:
                await ws.send_str(frame)
            except (aiohttp.ClientError, ConnectionResetError, RuntimeError) as exc:
                logger.warning("send failed (%s), session will reconnect", exc)
                self._session_closed.set()

    async def _sleep(self, delay: float, *, honour_session: bool = True) -> bool:
        """Interruptible sleep.

        Returns ``True`` when the delay elapsed normally, ``False`` when the agent is
        stopping or (unless ``honour_session`` is false) the relay session ended.
        """
        waiters = [asyncio.create_task(self._stop_event.wait())]
        if honour_session:
            waiters.append(asyncio.create_task(self._session_closed.wait()))
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
        if self._stop_event.is_set():
            return False
        if honour_session and self._session_closed.is_set():
            return False
        return not done


def _parse_level(name: str) -> EventLevel:
    try:
        return EventLevel(name.strip().lower())
    except ValueError:
        logger.warning("unknown log_min_level %r, falling back to 'error'", name)
        return EventLevel.ERROR
