"""Command executor (W4.4).

Every ``command`` received from the server goes through :meth:`CommandExecutor.execute`,
which always answers exactly one ``command_result``:

* ``timeout`` if the command is already past its ``expires_at`` (it is never executed),
  or if execution exceeded the per-kind timeout;
* ``refused_no_consent`` if the local consent is inactive at execution time (checked
  again just before running, whatever the server believed);
* ``success`` / ``failed`` otherwise, with a short human-readable output.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from hasup_protocol import CommandKind, CommandPayload, CommandResultPayload, CommandResultStatus

from .consent import ConsentManager
from .supervisor import SupervisorClient, SupervisorError, SupervisorTimeout

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 16 * 1024

# Per-kind execution budget, in seconds. Updates and backups are long operations on a
# small box; restarts are expected to return quickly.
DEFAULT_TIMEOUTS_S: dict[CommandKind, float] = {
    CommandKind.BACKUP_CREATE: 1800.0,
    CommandKind.CORE_RESTART: 300.0,
    CommandKind.CORE_UPDATE: 1800.0,
    CommandKind.OS_UPDATE: 1800.0,
    CommandKind.ADDON_UPDATE: 1800.0,
    CommandKind.ADDON_RESTART: 300.0,
}


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class CommandExecutor:
    """Executes maintenance commands through the Supervisor API."""

    def __init__(
        self,
        client: SupervisorClient,
        consent: ConsentManager,
        *,
        timeouts_s: dict[CommandKind, float] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._consent = consent
        self._timeouts_s = {**DEFAULT_TIMEOUTS_S, **(timeouts_s or {})}
        self._clock = clock
        self._self_slug: str | None = None

    async def execute(self, command: CommandPayload) -> CommandResultPayload:
        if command.expires_at <= self._clock():
            logger.warning("command %s received after expiry, not executed", command.command_id)
            return self._result(command, CommandResultStatus.TIMEOUT, "command expired")

        # Re-read the consent source: the final decision belongs to the agent.
        await self._consent.refresh()
        if not self._consent.is_active():
            logger.warning("command %s refused: local consent inactive", command.command_id)
            return self._result(
                command,
                CommandResultStatus.REFUSED_NO_CONSENT,
                "local maintenance consent is inactive",
            )

        timeout_s = self._timeouts_s.get(command.kind, 600.0)
        logger.info(
            "executing command %s (%s) with a %.0fs timeout",
            command.command_id,
            command.kind,
            timeout_s,
        )
        try:
            async with asyncio.timeout(timeout_s):
                output = await self._dispatch(command)
        except (TimeoutError, SupervisorTimeout):
            logger.error("command %s timed out after %.0fs", command.command_id, timeout_s)
            return self._result(
                command, CommandResultStatus.TIMEOUT, f"timed out after {timeout_s:.0f}s"
            )
        except SupervisorError as exc:
            logger.error("command %s failed: %s", command.command_id, exc)
            return self._result(command, CommandResultStatus.FAILED, str(exc))
        except ValueError as exc:
            logger.error("command %s rejected: %s", command.command_id, exc)
            return self._result(command, CommandResultStatus.FAILED, str(exc))

        return self._result(command, CommandResultStatus.SUCCESS, output)

    # ---------------------------------------------------------- dispatching

    async def _dispatch(self, command: CommandPayload) -> str:
        params = command.params
        match command.kind:
            case CommandKind.BACKUP_CREATE:
                return await self._backup_create(params)
            case CommandKind.CORE_RESTART:
                await self._client.core_restart(
                    timeout_s=self._timeouts_s[CommandKind.CORE_RESTART]
                )
                return "Home Assistant core restart requested"
            case CommandKind.CORE_UPDATE:
                version = _optional_str(params, "version")
                await self._client.core_update(
                    version, timeout_s=self._timeouts_s[CommandKind.CORE_UPDATE]
                )
                return f"Home Assistant core updated to {version or 'latest'}"
            case CommandKind.OS_UPDATE:
                version = _optional_str(params, "version")
                await self._client.os_update(
                    version, timeout_s=self._timeouts_s[CommandKind.OS_UPDATE]
                )
                # The Supervisor requires a host reboot to finish an OS update; the
                # agent never reboots the box on its own.
                return (
                    f"Home Assistant OS updated to {version or 'latest'}; "
                    "a host reboot is required to finish the update"
                )
            case CommandKind.ADDON_UPDATE:
                slug = _required_str(params, "addon_slug")
                await self._guard_self(slug, "update")
                version = _optional_str(params, "version")
                await self._client.addon_update(
                    slug, version, timeout_s=self._timeouts_s[CommandKind.ADDON_UPDATE]
                )
                return f"add-on {slug} updated to {version or 'latest'}"
            case CommandKind.ADDON_RESTART:
                slug = _required_str(params, "addon_slug")
                await self._guard_self(slug, "restart")
                await self._client.addon_restart(
                    slug, timeout_s=self._timeouts_s[CommandKind.ADDON_RESTART]
                )
                return f"add-on {slug} restarted"

    async def _backup_create(self, params: dict[str, Any]) -> str:
        name = _optional_str(params, "name") or f"hasup-{self._clock():%Y%m%d-%H%M%S}"
        partial = bool(params.get("partial", False))
        timeout_s = self._timeouts_s[CommandKind.BACKUP_CREATE]
        if partial:
            addons = _optional_str_list(params, "addons")
            folders = _optional_str_list(params, "folders")
            data = await self._client.backup_partial(
                name, addons=addons, folders=folders, timeout_s=timeout_s
            )
        else:
            data = await self._client.backup_full(name, timeout_s=timeout_s)
        slug = data.get("slug", "unknown")
        return f"Backup created: name={name} slug={slug}"

    async def _guard_self(self, slug: str, action: str) -> None:
        """Refuse to act on this very add-on: the result could never be reported."""
        if self._self_slug is None:
            try:
                self._self_slug = str((await self._client.self_info()).get("slug", "")) or ""
            except SupervisorError:
                self._self_slug = ""
        if self._self_slug and slug == self._self_slug:
            raise ValueError(
                f"refusing to {action} the supervisor agent add-on itself ({slug}); "
                "do it from the Home Assistant add-on page"
            )

    def _result(
        self, command: CommandPayload, status: CommandResultStatus, output: str
    ) -> CommandResultPayload:
        return CommandResultPayload(
            command_id=command.command_id,
            status=status,
            output=output[:MAX_OUTPUT_CHARS],
        )


def _required_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid parameter {key!r}")
    return value.strip()


def _optional_str(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid parameter {key!r}")
    return value.strip()


def _optional_str_list(params: dict[str, Any], key: str) -> list[str] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"invalid parameter {key!r}: expected a list of strings")
    return [str(item) for item in value]
