"""Clients for the two local APIs available to an add-on.

* the **Supervisor API** on ``http://supervisor/`` (``hassio_api: true``);
* the **Home Assistant API** on ``http://supervisor/core/api/`` (``homeassistant_api: true``).

Both are authenticated with the ``SUPERVISOR_TOKEN`` bearer token injected in the
add-on container. Supervisor answers are wrapped in ``{"result": ..., "data": ...}``
while Home Assistant answers are plain JSON, hence the two thin wrappers below.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, cast

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


class SupervisorError(RuntimeError):
    """An API call failed (transport error, HTTP error or ``result: error``)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SupervisorTimeout(SupervisorError):
    """An API call exceeded its timeout; reported as ``timeout``, not ``failed``."""


class SupervisorClient:
    """Async client for the Supervisor API and the Home Assistant core API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        session: aiohttp.ClientSession | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> SupervisorClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------ raw

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        as_text: bool = False,
    ) -> Any:
        """Call the Supervisor API and return the ``data`` field (or raw text)."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        timeout = aiohttp.ClientTimeout(total=timeout_s or self._timeout_s)
        headers = dict(self._headers)
        if as_text:
            headers["Accept"] = "text/plain"
        try:
            async with self._get_session().request(
                method, url, json=json_body, headers=headers, timeout=timeout
            ) as response:
                if as_text:
                    text = await response.text()
                    if response.status >= 400:
                        raise SupervisorError(
                            f"{method} {path} -> HTTP {response.status}", status=response.status
                        )
                    return text
                body = await response.json(content_type=None)
                if response.status >= 400:
                    message = ""
                    if isinstance(body, dict):
                        message = str(body.get("message", ""))
                    raise SupervisorError(
                        f"{method} {path} -> HTTP {response.status} {message}".strip(),
                        status=response.status,
                    )
        except TimeoutError as exc:
            raise SupervisorTimeout(f"{method} {path} -> timeout") from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise SupervisorError(f"{method} {path} -> {exc}") from exc

        if isinstance(body, dict) and "result" in body:
            if body.get("result") != "ok":
                raise SupervisorError(f"{method} {path} -> {body.get('message', 'error')}")
            return body.get("data", {})
        return body

    async def get(self, path: str, *, timeout_s: float | None = None) -> Any:
        return await self.request("GET", path, timeout_s=timeout_s)

    async def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        return await self.request("POST", path, json_body=json_body, timeout_s=timeout_s)

    # ------------------------------------------------------- read-only info

    async def core_info(self) -> dict[str, Any]:
        return _as_dict(await self.get("/core/info"))

    async def os_info(self) -> dict[str, Any]:
        return _as_dict(await self.get("/os/info"))

    async def supervisor_info(self) -> dict[str, Any]:
        return _as_dict(await self.get("/supervisor/info"))

    async def host_info(self) -> dict[str, Any]:
        return _as_dict(await self.get("/host/info"))

    async def addons(self) -> list[dict[str, Any]]:
        data = _as_dict(await self.get("/addons"))
        addons = data.get("addons")
        if not isinstance(addons, list) or any(not isinstance(item, dict) for item in addons):
            raise SupervisorError("invalid addons response")
        return addons

    async def self_info(self) -> dict[str, Any]:
        """Information about this very add-on (``/addons/self/info``)."""
        return _as_dict(await self.get("/addons/self/info"))

    async def core_logs(self, lines: int = 200) -> str:
        """Tail of the Home Assistant core journal, plain text, one record per line."""
        text = await self.request("GET", f"/core/logs?lines={lines}&no_colors", as_text=True)
        return text if isinstance(text, str) else ""

    # ------------------------------------------------- Home Assistant proxy

    async def ha_get(self, path: str, *, timeout_s: float | None = None) -> Any:
        return await self.get(f"/core/api/{path.lstrip('/')}", timeout_s=timeout_s)

    async def ha_config(self) -> dict[str, Any]:
        return _as_dict(await self.ha_get("config"))

    async def ha_states(self) -> list[dict[str, Any]]:
        data = await self.ha_get("states")
        if not isinstance(data, list):
            raise SupervisorError("invalid HA states response")
        return [item for item in data if isinstance(item, dict)]

    async def ha_entity_registry(self) -> list[dict[str, Any]]:
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._get_session().ws_connect(
                    f"{self.base_url}/core/websocket", headers=self._headers
                ) as ws:
                    hello = await ws.receive_json()
                    if hello.get("type") != "auth_required":
                        raise SupervisorError("HA WebSocket did not request authentication")
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise SupervisorError("HA WebSocket authentication failed")
                    await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
                    result = await ws.receive_json()
                    if (
                        result.get("id") != 1
                        or result.get("success") is not True
                        or not isinstance(result.get("result"), list)
                    ):
                        raise SupervisorError("HA entity registry unavailable")
                    return cast(list[dict[str, Any]], result["result"])
        except (TimeoutError, aiohttp.ClientError, ValueError, TypeError) as exc:
            raise SupervisorError("HA entity registry unavailable") from exc

    async def ha_state(self, entity_id: str) -> dict[str, Any] | None:
        """State object of one entity, or ``None`` when it does not exist."""
        try:
            return _as_dict(await self.ha_get(f"states/{entity_id}"))
        except SupervisorError as exc:
            if exc.status == 404:
                return None
            raise

    async def ha_call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> None:
        await self.post(f"/core/api/services/{domain}/{service}", json_body=data or {})

    # ------------------------------------------------------------ commands

    async def backup_full(
        self, name: str | None = None, *, timeout_s: float = 1800.0
    ) -> dict[str, Any]:
        body = {"name": name} if name else {}
        return _as_dict(await self.post("/backups/new/full", json_body=body, timeout_s=timeout_s))

    async def backup_partial(
        self,
        name: str | None = None,
        addons: list[str] | None = None,
        folders: list[str] | None = None,
        *,
        timeout_s: float = 1800.0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if addons:
            body["addons"] = addons
        if folders:
            body["folders"] = folders
        return _as_dict(
            await self.post("/backups/new/partial", json_body=body, timeout_s=timeout_s)
        )

    async def core_restart(self, *, timeout_s: float = 300.0) -> None:
        await self.post("/core/restart", timeout_s=timeout_s)

    async def core_update(self, version: str | None = None, *, timeout_s: float = 1800.0) -> None:
        body = {"version": version} if version else {}
        await self.post("/core/update", json_body=body, timeout_s=timeout_s)

    async def os_update(self, version: str | None = None, *, timeout_s: float = 1800.0) -> None:
        body = {"version": version} if version else {}
        await self.post("/os/update", json_body=body, timeout_s=timeout_s)

    async def addon_update(
        self, slug: str, version: str | None = None, *, timeout_s: float = 1800.0
    ) -> None:
        body = {"version": version} if version else {}
        await self.post(f"/addons/{slug}/update", json_body=body, timeout_s=timeout_s)

    async def addon_restart(self, slug: str, *, timeout_s: float = 300.0) -> None:
        await self.post(f"/addons/{slug}/restart", timeout_s=timeout_s)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
