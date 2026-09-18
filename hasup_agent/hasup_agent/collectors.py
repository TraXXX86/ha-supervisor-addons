"""Collect host measurements and inventory without inventing successful readings."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hasup_protocol import (
    AddonInfo,
    DiskUsageItem,
    DiskUsagePayload,
    HelloPayload,
    InstalledVersions,
    InventoryPayload,
    TelemetryPayload,
    UpdateAvailability,
)
from hasup_protocol.messages import (
    BackupLocation,
    CollectionHealth,
    DiskUsageDirectory,
    StorageBackup,
    StorageBackups,
)

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
            capabilities=["tunnel_v2", "disk_usage_v1"],
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

    async def disk_usage_payload(self) -> DiskUsagePayload:
        """Keep the disk and backup collections independent when either API fails."""
        backups = await self._storage_backups()
        try:
            raw = await self._client.disk_usage(max_depth=2)
            total = _non_negative_int(raw.get("total_bytes"))
            used = _non_negative_int(raw.get("used_bytes"))
            raw_children = raw.get("children", [])
            if total is None or total <= 0 or used is None or used > total:
                raise ValueError("invalid disk totals")
            if not isinstance(raw_children, list):
                raise ValueError("invalid disk breakdown")
            children: list[DiskUsageItem] = []
            for raw_item in raw_children[:100]:
                if not isinstance(raw_item, dict):
                    raise ValueError("invalid disk category")
                item_id = _bounded_text(raw_item.get("id"), 100)
                label = _bounded_text(raw_item.get("label"), 200)
                item_used = _non_negative_int(raw_item.get("used_bytes"))
                if item_id is None or label is None or item_used is None:
                    raise ValueError("invalid disk category")
                directories: list[DiskUsageDirectory] = []
                raw_directories = raw_item.get("children", [])
                if isinstance(raw_directories, list):
                    seen: set[str] = set()
                    for directory in raw_directories:
                        if not isinstance(directory, dict):
                            continue
                        directory_id = _bounded_text(directory.get("id"), 100)
                        directory_label = _bounded_text(directory.get("label"), 200)
                        directory_used = _non_negative_int(directory.get("used_bytes"))
                        if (
                            directory_id is None
                            or directory_label is None
                            or directory_used is None
                            or directory_id in seen
                        ):
                            continue
                        seen.add(directory_id)
                        directories.append(
                            DiskUsageDirectory(
                                id=directory_id, label=directory_label, used_bytes=directory_used
                            )
                        )
                # A changing filesystem can yield inconsistent child sizes. Keep totals.
                if sum(item.used_bytes for item in directories) > item_used:
                    directories = []
                directories.sort(key=lambda item: item.used_bytes, reverse=True)
                children.append(
                    DiskUsageItem(
                        id=item_id, label=label, used_bytes=item_used, children=directories[:20]
                    )
                )
            payload = DiskUsagePayload(
                total_bytes=total,
                used_bytes=used,
                children=children,
                backups=backups,
            )
            payload.collection = self._health("disk_usage", True)
            return payload
        except (SupervisorError, ValueError, TypeError) as exc:
            logger.warning("detailed disk usage unavailable: %s", exc)
            return DiskUsagePayload(collection=self._health("disk_usage", False), backups=backups)

    async def _storage_backups(self) -> StorageBackups:
        try:
            raw = await self._client.storage_backups()
            details = await self._storage_backup_details(raw)
            by_slug: dict[str, StorageBackup] = {}
            for item in raw:
                slug = _bounded_text(item.get("slug"), 100)
                name = _bounded_text(item.get("name"), 200)
                if slug is None or name is None:
                    raise ValueError("invalid backup")
                # Supervisor reports MiB, rounded to two decimals; never sum these
                # estimates into the measured disk total (backups may be remote).
                size = _float_or_none(item.get("size"))
                size_bytes = _non_negative_int(item.get("size_bytes"))
                if size_bytes is None and size is not None and size >= 0:
                    size_bytes = round(size * 1024**2)
                raw_locations = item.get("locations")
                if raw_locations is None:
                    if "location" not in item:
                        raise ValueError("backup location missing")
                    raw_locations = [item.get("location")]
                if not isinstance(raw_locations, list):
                    raise ValueError("invalid backup locations")
                locations: list[str | None] = []
                for raw_location in raw_locations[:20]:
                    location_id = _parse_location(raw_location)
                    if location_id not in locations:
                        locations.append(location_id)
                location = (
                    _parse_location(item["location"])
                    if "location" in item
                    else (locations[0] if locations else None)
                )
                if "location" not in item and not locations:
                    raise ValueError("backup location missing")
                if location not in locations:
                    locations.insert(0, location)
                raw_attributes = item.get("location_attributes")
                attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
                copies: list[BackupLocation] = []
                for location_id in locations:
                    attribute_key = ".local" if location_id is None else location_id
                    attr = attributes.get(attribute_key, {})
                    if not isinstance(attr, dict):
                        attr = {}
                    copy_size = _non_negative_int(attr.get("size_bytes"))
                    copy_protected = attr.get("protected")
                    if location_id == location:
                        if copy_size is None:
                            copy_size = size_bytes
                        if not isinstance(copy_protected, bool):
                            copy_protected = item.get("protected")
                    copies.append(
                        BackupLocation(
                            id=location_id,
                            kind="local" if location_id is None else "remote",
                            size_bytes=copy_size,
                            protected=copy_protected if isinstance(copy_protected, bool) else None,
                        )
                    )
                date = datetime.fromisoformat(str(item.get("date")))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                backup_type = item.get("type")
                if backup_type not in ("full", "partial"):
                    raise ValueError("invalid backup type")
                existing = by_slug.get(slug)
                if existing is not None:
                    if (
                        existing.name != name
                        or existing.date != date
                        or existing.type != backup_type
                    ):
                        raise ValueError("conflicting duplicate backup")
                    for copy in copies:
                        if copy.id not in existing.locations:
                            existing.locations.append(copy.id)
                            existing.copies.append(copy)
                    continue

                detail = details.get(slug, {})
                content_source = detail.get("content", item.get("content"))
                content = content_source if isinstance(content_source, dict) else {}
                addons = _text_list(
                    detail.get("addons", content.get("addons", item.get("addons"))), 200
                )
                folders = _text_list(
                    detail.get("folders", content.get("folders", item.get("folders"))), 100
                )
                ha_included = content.get("homeassistant")
                exclude_database = detail.get("homeassistant_exclude_database")
                database_included = (
                    bool(ha_included and not exclude_database)
                    if isinstance(ha_included, bool) and isinstance(exclude_database, bool)
                    else None
                )
                protected = detail.get("protected", item.get("protected"))
                if not isinstance(protected, bool):
                    protected = None
                version = _bounded_text(
                    detail.get("homeassistant")
                    or detail.get("homeassistant_version")
                    or item.get("homeassistant_version"),
                    50,
                )
                by_slug[slug] = StorageBackup(
                    slug=slug,
                    name=name,
                    date=date,
                    size_bytes=size_bytes,
                    location=location,
                    locations=locations,
                    copies=copies,
                    type="full" if backup_type == "full" else "partial",
                    protected=protected,
                    home_assistant_version=version,
                    database_included=database_included,
                    addons=addons,
                    folders=folders,
                )
            items = list(by_slug.values())
            items.sort(key=lambda item: item.date, reverse=True)
            return StorageBackups(
                collection=self._health("storage_backups", True),
                items=items[:100],
                total_count=len(items),
                complete=len(items) <= 100,
                coverage="supervisor_visible",
            )
        except (SupervisorError, ValueError, TypeError) as exc:
            logger.warning("storage backup list unavailable: %s", exc)
            return StorageBackups(collection=self._health("storage_backups", False))

    async def _storage_backup_details(self, raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Fetch bounded optional details without making list health depend on them."""
        slugs: list[str] = []

        def sort_date(item: dict[str, Any]) -> str:
            return str(item.get("date", ""))

        for item in sorted(raw, key=sort_date, reverse=True)[:20]:
            slug = _bounded_text(item.get("slug"), 100)
            if slug is not None and slug not in slugs:
                slugs.append(slug)

        semaphore = asyncio.Semaphore(5)

        async def load(slug: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                try:
                    return slug, await self._client.storage_backup_info(slug)
                except SupervisorError as exc:
                    logger.debug("backup detail unavailable for %s: %s", slug, exc)
                    return slug, {}

        return dict(await asyncio.gather(*(load(slug) for slug in slugs)))

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


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _text_list(value: Any, limit: int) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in value[:limit]:
        # Backup content can be returned as slugs or ``{"slug": ...}``.
        raw = item.get("slug") if isinstance(item, dict) else item
        text = _bounded_text(raw, 100)
        if text is not None and text not in result:
            result.append(text)
    return result


def _parse_location(value: Any) -> str | None:
    if value in (None, ".local"):
        return None
    result = _bounded_text(value, 200)
    if result is None:
        raise ValueError("invalid backup location")
    return result


def _capacity_pair(used: Any, total: Any) -> tuple[float | None, float | None]:
    used, total = _float_or_none(used), _float_or_none(total)
    if used is None or total is None or not 0 <= used <= total or total <= 0:
        return None, None
    return used, total


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None
