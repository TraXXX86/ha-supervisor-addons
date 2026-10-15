"""HACS inventory built from HA's registries, never from entity name guesses."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hasup_protocol import CommandKind, CommandPayload, CommandProgressPayload, HacsRepository

from .consent import ConsentManager
from .supervisor import SupervisorClient, SupervisorError

HACS_CAPABILITY = "hacs_update_v1"
ENTITY_ID = re.compile(r"^update\.[a-z0-9_]{1,248}$")


def repositories(
    states: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    devices: list[dict[str, Any]],
) -> list[HacsRepository]:
    by_entity = {s.get("entity_id"): s for s in states}
    by_device = {d.get("id"): d for d in devices}
    items = []
    for entry in registry:
        entity_id = str(entry.get("entity_id", ""))
        if entry.get("platform") != "hacs" or not ENTITY_ID.fullmatch(entity_id):
            continue
        state = by_entity.get(entity_id, {})
        attrs = state.get("attributes") or {}
        device = by_device.get(entry.get("device_id"), {})
        release_url = str(attrs.get("release_url") or "")
        try:
            parsed = urlsplit(release_url)
            safe_url = parsed.scheme == "https" and parsed.hostname == "github.com"
            parts = parsed.path.strip("/").split("/")
        except ValueError:
            safe_url, parts = False, []
        repository = "/".join(parts[:2]) if safe_url and len(parts) >= 2 else None
        category = "integration" if repository == "hacs/integration" else device.get("model")
        summary = str(attrs.get("release_summary") or "").casefold()
        # HACS exposes this marker through the standard update entity. For unknown
        # categories absence of a marker cannot prove that no restart is needed.
        restart = "restart of home assistant required" in summary
        features = attrs.get("supported_features")
        progress = attrs.get("in_progress", False)
        available = state.get("state") in {"on", "off"} and not entry.get("disabled_by")
        items.append(
            HacsRepository(
                entity_id=entity_id,
                name=str(attrs.get("friendly_name") or device.get("name") or entity_id)[:200],
                repository=repository[:300] if repository else None,
                category=str(category)[:50] if category else None,
                installed_version=_version(attrs.get("installed_version")),
                latest_version=_version(attrs.get("latest_version")),
                release_url=release_url[:1000] if safe_url else None,
                available=available,
                update_available=available and state.get("state") == "on",
                can_install=available and isinstance(features, int) and bool(features & 1),
                in_progress=progress is True
                or (isinstance(progress, (int, float)) and not isinstance(progress, bool)),
                restart_required=restart if category or restart else None,
            )
        )
    return sorted(items, key=lambda item: (item.name.casefold(), item.entity_id))


async def read_repository(client: SupervisorClient, entity_id: str) -> HacsRepository:
    if not ENTITY_ID.fullmatch(entity_id):
        raise ValueError("Identifiant de module HACS invalide")
    registry = await client.ha_entity_registry()
    if not any(e.get("entity_id") == entity_id and e.get("platform") == "hacs" for e in registry):
        raise ValueError("Cette entité n'appartient pas à HACS")
    state = await client.ha_state(entity_id)
    try:
        devices = await client.ha_device_registry()
    except SupervisorError:
        devices = []
    items = repositories([state] if state else [], registry, devices)
    return next(item for item in items if item.entity_id == entity_id)


def _version(value: Any) -> str | None:
    return value[:200] if isinstance(value, str) and value else None


class HacsConsentExpired(ValueError):
    """Consent was revoked while creating the pre-update backup."""


class HacsUpdater:
    def __init__(self, client: SupervisorClient, consent: ConsentManager) -> None:
        self.client = client
        self.consent = consent
        self.poll_interval_s = 5.0
        self.verify_timeout_s = 180.0

    async def run(
        self,
        command: CommandPayload,
        report: Callable[[CommandProgressPayload], Awaitable[None]],
    ) -> str:
        entity_id = command.params.get("entity_id")
        expected = command.params.get("expected_version")
        if not isinstance(entity_id, str) or not isinstance(expected, str) or not expected:
            raise ValueError("Module et version attendue obligatoires")
        if len(expected) > 200 or set(command.params) != {"entity_id", "expected_version"}:
            raise ValueError("Paramètres HACS invalides")
        item = await read_repository(self.client, entity_id)
        if not item.available or item.in_progress:
            raise ValueError("Module indisponible ou installation déjà en cours")
        progress = CommandProgressPayload(
            command_id=command.command_id,
            entity_id=entity_id,
            phase="backing_up",
            target_version=expected,
            previous_version=item.installed_version,
        )

        async def phase(value: Any) -> None:
            progress.phase = value
            await report(progress.model_copy())

        if command.kind == CommandKind.HACS_UPDATE:
            self._check_update(item, expected)
            await phase("backing_up")
            backup = await self.client.backup_full(
                f"Avant HACS {item.name} {datetime.now(tz=UTC):%Y%m%d-%H%M%S}"[:200]
            )
            slug = backup.get("slug")
            if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", slug):
                raise ValueError("La sauvegarde préalable n'a pas été confirmée")
            progress.backup_slug = slug
            await phase("backing_up")
            # Fail closed: require a readable full archive before modifying HACS.
            archive = await self.client.storage_backup_info(slug)
            if archive.get("type") != "full":
                raise ValueError("La sauvegarde complète préalable n'a pas été vérifiée")
            await self._check_consent(command)
            current = await read_repository(self.client, entity_id)
            self._check_update(current, expected)
            if current.installed_version != item.installed_version:
                raise ValueError(
                    "Le module a changé depuis la confirmation ; actualisez l'inventaire"
                )
            await phase("installing")
            # Only the version just observed as offered by HACS is allowed, never
            # an arbitrary branch/tag supplied by an operator.
            await self.client.ha_call_service(
                "update",
                "install",
                {"entity_id": entity_id, "version": expected},
                timeout_s=900,
            )
            await phase("verifying")
            current = await self._wait_installed(entity_id, expected, restarted=False)
            needs_restart = current.restart_required is True or current.category == "integration"
            await phase("restart_required" if needs_restart else "downloaded")
            suffix = (
                "Redémarrage de Home Assistant requis."
                if needs_restart
                else ("Téléchargement vérifié ; activation à contrôler dans Home Assistant.")
            )
            return (
                f"{item.name} : {item.installed_version} → {expected}. "
                f"Sauvegarde : {slug}. {suffix}"
            )

        if item.installed_version != expected or item.restart_required is not True:
            raise ValueError(
                "Aucun redémarrage HACS confirmé pour cette version ; actualisez l'inventaire"
            )
        baseline = self._broken(await self.client.ha_config_entries())
        await self._check_consent(command)
        await phase("restarting")
        # A restart may close HTTP. Never repeat it; verify recovery below.
        with suppress(SupervisorError):
            await self.client.core_restart(timeout_s=300)
        await phase("verifying")
        await self._wait_installed(entity_id, expected, restarted=True)
        newly_broken = self._broken(await self.client.ha_config_entries()) - baseline
        if newly_broken:
            raise ValueError(
                "Nouvelles erreurs d'intégration après redémarrage : "
                + ", ".join(sorted(newly_broken))[:500]
            )
        await phase("verified")
        return (
            f"{item.name} : version {expected} vérifiée après redémarrage, "
            "Home Assistant opérationnel."
        )

    async def _check_consent(self, command: CommandPayload) -> None:
        if command.expires_at <= datetime.now(tz=UTC):
            raise TimeoutError("Commande expirée avant modification")
        await self.consent.refresh()
        if not self.consent.is_active():
            raise HacsConsentExpired("Consentement retiré avant modification de Home Assistant")

    @staticmethod
    def _check_update(item: HacsRepository, expected: str) -> None:
        if (
            not item.available
            or not item.can_install
            or not item.update_available
            or item.in_progress
        ):
            raise ValueError("Ce module HACS ne peut pas être mis à jour actuellement")
        if item.restart_required:
            raise ValueError("Un redémarrage est déjà requis pour ce module")
        if item.latest_version != expected or item.installed_version == expected:
            raise ValueError("La version proposée a changé ; actualisez l'inventaire")

    async def _wait_installed(
        self, entity_id: str, expected: str, *, restarted: bool
    ) -> HacsRepository:
        async with asyncio.timeout(self.verify_timeout_s):
            while True:
                try:
                    item = await read_repository(self.client, entity_id)
                    ready = (
                        item.available
                        and not item.in_progress
                        and item.installed_version == expected
                    )
                    if ready and not restarted:
                        return item
                    if ready and restarted and item.restart_required is False:
                        config = await self.client.ha_config()
                        if config.get("state") == "RUNNING":
                            return item
                except SupervisorError:
                    pass
                await asyncio.sleep(self.poll_interval_s)

    @staticmethod
    def _broken(entries: list[dict[str, Any]]) -> set[str]:
        return {
            f"{entry.get('domain')}:{entry.get('entry_id', entry.get('title', ''))}"
            for entry in entries
            if not entry.get("disabled_by") and entry.get("state") not in {"loaded", "not_loaded"}
        }
