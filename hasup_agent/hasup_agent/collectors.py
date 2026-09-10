"""Collect host measurements and inventory without inventing successful readings."""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hasup_protocol import (
    AddonInfo,
    HelloPayload,
    InstalledVersions,
    InventoryPayload,
    TelemetryPayload,
    UpdateAvailability,
)
from hasup_protocol.messages import CollectionHealth

from . import AGENT_VERSION
from .metrics import HostMetricsReader
from .supervisor import SupervisorClient, SupervisorError

logger = logging.getLogger(__name__)


class Collectors:
    def __init__(self, client: SupervisorClient, metrics: HostMetricsReader | None = None) -> None:
        self._client = client
        self._metrics = metrics or HostMetricsReader()
        self._last_success: dict[str, datetime] = {}
        self._inventory = InventoryPayload()

    def _health(self, key: str, valid: bool) -> CollectionHealth:
        if valid:
            self._last_success[key] = datetime.now(tz=UTC)
        return CollectionHealth(
            status="ok" if valid else "unavailable", last_success_at=self._last_success.get(key)
        )

    async def hello_payload(self) -> HelloPayload:
        core = await self._safe_dict(self._client.core_info, "core/info")
        os_info = await self._safe_dict(self._client.os_info, "os/info")
        supervisor = await self._safe_dict(self._client.supervisor_info, "supervisor/info")
        return HelloPayload(
            agent_version=AGENT_VERSION,
            capabilities=["tunnel_v2"],
            ha_core=_str_or_none(core.get("version")),
            ha_os=_str_or_none(os_info.get("version")),
            ha_supervisor=_str_or_none(supervisor.get("version")),
            machine=_str_or_none(supervisor.get("machine")) or _str_or_none(os_info.get("board")),
            arch=_str_or_none(supervisor.get("arch")),
        )

    async def telemetry_payload(self) -> TelemetryPayload:
        # The first CPU reading establishes a baseline; no blocking sleep or fake zero.
        cpu = _float_or_none(self._metrics.cpu_percent())
        if cpu is not None and not 0 <= cpu <= 100:
            cpu = None
        memory = self._metrics.memory_mb()
        mem_used, mem_total = _capacity_pair(*(memory or (None, None)))
        disk_used, disk_total = await self._disk_gb()
        temp = _float_or_none(self._metrics.temperature_c())
        return TelemetryPayload(
            cpu_pct=_rounded(cpu, 1),
            mem_used_mb=_rounded(mem_used, 1),
            mem_total_mb=_rounded(mem_total, 1),
            disk_used_gb=_rounded(disk_used, 2),
            disk_total_gb=_rounded(disk_total, 2),
            temp_c=temp,
            collection={
                key: self._health(key, valid)
                for key, valid in (
                    ("cpu", cpu is not None),
                    ("memory", mem_total is not None),
                    ("disk", disk_total is not None),
                    ("temperature", temp is not None),
                )
            },
        )

    async def _disk_gb(self) -> tuple[float | None, float | None]:
        host = await self._safe_dict(self._client.host_info, "host/info")
        total = _float_or_none(host.get("disk_total"))
        used = _float_or_none(host.get("disk_used"))
        if used is None and total is not None:
            free = _float_or_none(host.get("disk_free"))
            used = total - free if free is not None else None
        return _capacity_pair(used, total)

    def uptime_s(self) -> int:
        return self._metrics.uptime_s()

    async def inventory_payload(self) -> InventoryPayload:
        current = self._inventory.model_copy(deep=True)
        current.addons, current.collection["addons"] = await self._collect(
            "addons", self._addons, current.addons
        )
        components, current.collection["integrations"] = await self._collect(
            "integrations", self._components, current.integrations
        )
        current.integrations = _integrations(components)
        states: list[dict[str, Any]]
        empty_states: list[dict[str, Any]] = []
        states, current.collection["automations"] = await self._collect(
            "automations", self._states, empty_states
        )
        if current.collection["automations"].status == "ok":
            current.automation_count = sum(
                str(state.get("entity_id", "")).startswith("automation.") for state in states
            )
        hacs_valid = (
            current.collection["integrations"].status == "ok"
            and current.collection["automations"].status == "ok"
        )
        if hacs_valid:
            try:
                registry = (
                    await self._client.ha_entity_registry()
                    if "hacs" in current.integrations
                    else []
                )
                current.hacs = _hacs_repositories(current.integrations, states, registry)
                # Unavailable HACS update entities must not mean 'no updates'.
                ids = {
                    entry.get("entity_id")
                    for entry in registry
                    if entry.get("platform") == "hacs"
                    and str(entry.get("entity_id", "")).startswith("update.")
                }
                observed = {s.get("entity_id"): s.get("state") for s in states}
                hacs_valid = all(observed.get(entity_id) in {"on", "off"} for entity_id in ids)
            except SupervisorError as exc:
                logger.warning("HACS attribution unavailable: %s", exc)
                hacs_valid = False
        current.collection["hacs"] = self._health("hacs", hacs_valid)
        flags = current.update_available.model_dump()
        versions: dict[str, str | None] = {}
        for key, call in (
            ("core", self._client.core_info),
            ("os", self._client.os_info),
            ("supervisor", self._client.supervisor_info),
        ):
            data = await self._safe_dict(call, key)
            version = data.get("version")
            versions[key] = (
                version.strip() or None
                if isinstance(version, str) and len(version.strip()) <= 50
                else None
            )
            value = data.get("update_available")
            valid = isinstance(value, bool)
            if valid:
                flags[key] = value
            current.collection[key] = self._health(key, valid)
        current.update_available = UpdateAvailability(**flags)
        current.versions = InstalledVersions(**versions)
        self._inventory = current
        return current.model_copy(deep=True)

    async def _collect[T](
        self, key: str, call: Callable[[], Awaitable[T]], previous: T
    ) -> tuple[T, CollectionHealth]:
        try:
            value = await call()
        except (SupervisorError, ValueError, TypeError) as exc:
            logger.warning("%s inventory unavailable: %s", key, exc)
            return previous, self._health(key, False)
        return value, self._health(key, True)

    async def _addons(self) -> list[AddonInfo]:
        raw = await self._client.addons()
        addons = []
        for item in raw:
            slug = _str_or_none(item.get("slug"))
            if not slug:
                raise SupervisorError("addon lacks slug")
            flag = item.get("update_available")
            addons.append(
                AddonInfo(
                    slug=slug,
                    name=_str_or_none(item.get("name")) or slug,
                    version=_str_or_none(item.get("version"))
                    or _str_or_none(item.get("version_installed"))
                    or "",
                    update_available=flag if isinstance(flag, bool) else None,
                )
            )
        return sorted(addons, key=lambda addon: addon.slug)

    async def _components(self) -> list[str]:
        components = (await self._client.ha_config()).get("components")
        if not isinstance(components, list):
            raise SupervisorError("invalid HA components")
        return [str(c) for c in components]

    async def _states(self) -> list[dict[str, Any]]:
        return await self._client.ha_states()

    async def _safe_dict(self, call: Any, label: str) -> dict[str, Any]:
        try:
            result = await call()
            return result if isinstance(result, dict) else {}
        except SupervisorError as exc:
            logger.warning("%s unavailable: %s", label, exc)
            return {}


def _integrations(components: list[str]) -> list[str]:
    return sorted({c for c in components if c and "." not in c})


def _hacs_repositories(
    components: list[str],
    states: list[dict[str, Any]],
    registry: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Only pending update entities attributed to HACS by HA's entity registry."""
    if "hacs" not in components:
        return []
    ids = {entry.get("entity_id") for entry in registry or [] if entry.get("platform") == "hacs"}
    repos = set()
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if (
            entity_id not in ids
            or not entity_id.startswith("update.")
            or state.get("state") != "on"
        ):
            continue
        attributes = state.get("attributes") or {}
        url = urlsplit(str(attributes.get("release_url", "")))
        parts = url.path.strip("/").split("/")
        repos.add(
            "/".join(parts[:2]) if url.hostname == "github.com" and len(parts) >= 2 else entity_id
        )
    return sorted(repos)


def _str_or_none(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _capacity_pair(used: Any, total: Any) -> tuple[float | None, float | None]:
    used, total = _float_or_none(used), _float_or_none(total)
    if used is None or total is None or not 0 <= used <= total or total <= 0:
        return None, None
    return used, total


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None
