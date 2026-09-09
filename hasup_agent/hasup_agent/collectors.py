"""Collectors (W4.2): build the hello, telemetry and inventory payloads.

Sources:

* Supervisor API: ``/core/info``, ``/os/info``, ``/supervisor/info``, ``/host/info``,
  ``/addons``;
* Home Assistant API: ``/core/api/config`` (loaded components) and ``/core/api/states``
  (automation count, HACS sensor);
* the container's ``/proc`` and ``/sys`` for host CPU, memory, uptime and temperature
  (see :mod:`hasup_agent.metrics`).

Every collector degrades gracefully: a failing endpoint yields a partial payload and a
warning rather than an exception, because losing telemetry must never take the agent
down.
"""

from __future__ import annotations

import logging
from typing import Any

from hasup_protocol import (
    AddonInfo,
    HelloPayload,
    InventoryPayload,
    TelemetryPayload,
    UpdateAvailability,
)

from . import AGENT_VERSION
from .metrics import HostMetricsReader
from .supervisor import SupervisorClient, SupervisorError

logger = logging.getLogger(__name__)

# Entities exposed by the Supervisor integration itself: they are not HACS content.
_CORE_UPDATE_ENTITIES = {
    "update.home_assistant_core_update",
    "update.home_assistant_operating_system_update",
    "update.home_assistant_supervisor_update",
}


class Collectors:
    """Gathers everything the agent reports about the box."""

    def __init__(
        self,
        client: SupervisorClient,
        metrics: HostMetricsReader | None = None,
    ) -> None:
        self._client = client
        self._metrics = metrics or HostMetricsReader()

    # ------------------------------------------------------------- hello

    async def hello_payload(self) -> HelloPayload:
        core = await self._safe_dict(self._client.core_info, "core/info")
        os_info = await self._safe_dict(self._client.os_info, "os/info")
        supervisor = await self._safe_dict(self._client.supervisor_info, "supervisor/info")

        machine = _str_or_none(supervisor.get("machine")) or _str_or_none(os_info.get("board"))
        arch = _str_or_none(supervisor.get("arch"))

        return HelloPayload(
            agent_version=AGENT_VERSION,
            ha_core=_str_or_none(core.get("version")),
            ha_os=_str_or_none(os_info.get("version")),
            ha_supervisor=_str_or_none(supervisor.get("version")),
            machine=machine,
            arch=arch,
        )

    # --------------------------------------------------------- telemetry

    async def telemetry_payload(self) -> TelemetryPayload:
        cpu = self._metrics.cpu_percent()
        if cpu is None:
            cpu = self._metrics.cpu_percent_blocking() or 0.0

        memory = self._metrics.memory_mb()
        if memory is None:
            logger.warning("memory metrics unavailable, reporting zeros")
            mem_used_mb, mem_total_mb = 0.0, 1.0
        else:
            mem_used_mb, mem_total_mb = memory

        disk_used_gb, disk_total_gb = await self._disk_gb()

        return TelemetryPayload(
            cpu_pct=round(cpu, 1),
            mem_used_mb=round(mem_used_mb, 1),
            mem_total_mb=round(mem_total_mb, 1),
            disk_used_gb=round(disk_used_gb, 2),
            disk_total_gb=round(disk_total_gb, 2),
            temp_c=self._metrics.temperature_c(),
        )

    async def _disk_gb(self) -> tuple[float, float]:
        host = await self._safe_dict(self._client.host_info, "host/info")
        total = _float_or_none(host.get("disk_total"))
        used = _float_or_none(host.get("disk_used"))
        if used is None and total is not None:
            free = _float_or_none(host.get("disk_free"))
            used = total - free if free is not None else None
        if total is None or total <= 0:
            logger.warning("disk metrics unavailable from host/info, reporting zeros")
            return 0.0, 1.0
        return max(0.0, used or 0.0), total

    def uptime_s(self) -> int:
        return self._metrics.uptime_s()

    # --------------------------------------------------------- inventory

    async def inventory_payload(self) -> InventoryPayload:
        addons = await self._addons()
        components = await self._components()
        states = await self._states()

        return InventoryPayload(
            addons=addons,
            integrations=_integrations(components),
            hacs=_hacs_repositories(components, states),
            automation_count=sum(
                1 for state in states if str(state.get("entity_id", "")).startswith("automation.")
            ),
            update_available=await self._update_availability(),
        )

    async def _addons(self) -> list[AddonInfo]:
        try:
            raw_addons = await self._client.addons()
        except SupervisorError as exc:
            logger.warning("addon inventory unavailable: %s", exc)
            return []
        addons: list[AddonInfo] = []
        for item in raw_addons:
            slug = _str_or_none(item.get("slug"))
            if not slug:
                continue
            version = (
                _str_or_none(item.get("version"))
                or _str_or_none(item.get("version_installed"))
                or ""
            )
            addons.append(
                AddonInfo(
                    slug=slug,
                    name=_str_or_none(item.get("name")) or slug,
                    version=version,
                    update_available=bool(item.get("update_available", False)),
                )
            )
        return sorted(addons, key=lambda addon: addon.slug)

    async def _components(self) -> list[str]:
        try:
            config = await self._client.ha_config()
        except SupervisorError as exc:
            logger.warning("home assistant config unavailable: %s", exc)
            return []
        components = config.get("components", [])
        if not isinstance(components, list):
            return []
        return [str(component) for component in components]

    async def _states(self) -> list[dict[str, Any]]:
        try:
            return await self._client.ha_states()
        except SupervisorError as exc:
            logger.warning("home assistant states unavailable: %s", exc)
            return []

    async def _update_availability(self) -> UpdateAvailability:
        core = await self._safe_dict(self._client.core_info, "core/info")
        os_info = await self._safe_dict(self._client.os_info, "os/info")
        supervisor = await self._safe_dict(self._client.supervisor_info, "supervisor/info")
        return UpdateAvailability(
            core=bool(core.get("update_available", False)),
            os=bool(os_info.get("update_available", False)),
            supervisor=bool(supervisor.get("update_available", False)),
        )

    # ------------------------------------------------------------ helpers

    async def _safe_dict(self, call: Any, label: str) -> dict[str, Any]:
        try:
            result = await call()
        except SupervisorError as exc:
            logger.warning("%s unavailable: %s", label, exc)
            return {}
        return result if isinstance(result, dict) else {}


def _integrations(components: list[str]) -> list[str]:
    """Loaded integration domains.

    ``/core/api/config`` returns both domains (``mqtt``) and platform entries
    (``sensor.mqtt``); only the domains are reported.
    """
    domains = {component for component in components if component and "." not in component}
    return sorted(domains)


def _hacs_repositories(components: list[str], states: list[dict[str, Any]]) -> list[str]:
    """Best-effort list of HACS content.

    Limits (documented in ``agent/README.md``): an add-on cannot read the
    ``custom_components`` folder of Home Assistant, and the HACS inventory is only
    exposed through the HACS WebSocket API. What the REST API offers is:

    * the ``sensor.hacs`` entity, whose ``repositories`` attribute lists the
      repositories **with a pending update** (not the full installed set);
    * HACS ``update.*`` entities (HACS >= 1.33), one per repository with a pending
      update.

    So the reported list is "HACS content needing an update", not "everything HACS
    installed". When HACS is not installed the list is empty.
    """
    if "hacs" not in components:
        return []

    repositories: set[str] = set()
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        attributes = state.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}

        if entity_id == "sensor.hacs":
            raw_repositories = attributes.get("repositories", [])
            if isinstance(raw_repositories, list):
                for repository in raw_repositories:
                    name = _repository_name(repository)
                    if name:
                        repositories.add(name)
        elif entity_id.startswith("update.") and entity_id not in _CORE_UPDATE_ENTITIES:
            # HACS update entities carry a release_url pointing at the GitHub repository.
            release_url = str(attributes.get("release_url", ""))
            if "github.com/" in release_url:
                repositories.add(_repository_from_url(release_url))

    return sorted(repositories)


def _repository_name(repository: Any) -> str | None:
    if isinstance(repository, str):
        return repository
    if isinstance(repository, dict):
        for key in ("full_name", "name", "display_name"):
            value = repository.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _repository_from_url(url: str) -> str:
    path = url.split("github.com/", 1)[1]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return path


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
