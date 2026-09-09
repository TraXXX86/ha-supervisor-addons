"""Persistent agent state stored in the add-on ``/data`` volume.

``/data`` survives add-on restarts and updates, which is where the device
credentials obtained at enrollment must live. The consent activation timestamp is
persisted too, so restarting the add-on cannot be used to extend a maintenance
window past its expiry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AgentState:
    """Small JSON-backed key/value store with atomic writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            parsed: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("state file %s unreadable (%s), starting fresh", self.path, exc)
            return
        if isinstance(parsed, dict):
            self._data = parsed
        else:
            logger.warning("state file %s is not a JSON object, starting fresh", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file in the same directory then rename: a power loss
        # can never leave a half-written state file behind.
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    # --- device credentials ------------------------------------------------

    @property
    def device_id(self) -> str | None:
        value = self._data.get("device_id")
        return value if isinstance(value, str) else None

    @property
    def device_token(self) -> str | None:
        value = self._data.get("device_token")
        return value if isinstance(value, str) else None

    @property
    def enrolled(self) -> bool:
        return bool(self.device_token)

    def set_credentials(self, device_id: str, device_token: str) -> None:
        self._data["device_id"] = device_id
        self._data["device_token"] = device_token
        self._data["enrolled_at"] = datetime.now(tz=UTC).isoformat()
        self._save()

    def clear_credentials(self) -> None:
        for key in ("device_id", "device_token", "enrolled_at"):
            self._data.pop(key, None)
        self._save()

    # --- enrollment code bookkeeping --------------------------------------

    @property
    def used_enroll_code(self) -> str | None:
        """Code that was already exchanged, to avoid retrying a burnt code."""
        value = self._data.get("used_enroll_code")
        return value if isinstance(value, str) else None

    def set_used_enroll_code(self, code: str) -> None:
        self._data["used_enroll_code"] = code
        self._save()

    # --- consent window ----------------------------------------------------

    @property
    def consent_activated_at(self) -> datetime | None:
        raw = self._data.get("consent_activated_at")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def set_consent_activated_at(self, moment: datetime | None) -> None:
        if moment is None:
            self._data.pop("consent_activated_at", None)
        else:
            self._data["consent_activated_at"] = moment.astimezone(UTC).isoformat()
        self._save()
