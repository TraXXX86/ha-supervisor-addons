"""Enrollment: exchange the single-use enroll code for a device token.

``POST {server_url}/api/v1/enroll {"enroll_code": ...}`` -> ``201 {device_id, device_token}``
(see ``docs/protocol.md`` section 2). A ``400`` means the code is invalid, expired or
already used: retrying it is pointless, so that case is reported separately from a
transport failure, which is worth retrying with the reconnection backoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

ENROLL_TIMEOUT_S = 30.0


class EnrollmentRejected(Exception):
    """The server refused the code (invalid, expired or already used)."""


class EnrollmentUnavailable(Exception):
    """The server could not be reached or failed; retrying later makes sense."""


@dataclass(frozen=True)
class EnrollmentResult:
    device_id: str
    device_token: str


async def enroll(
    session: aiohttp.ClientSession,
    enroll_url: str,
    enroll_code: str,
    *,
    verify_tls: bool = True,
    timeout_s: float = ENROLL_TIMEOUT_S,
) -> EnrollmentResult:
    payload = {"enroll_code": enroll_code}
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with session.post(
            enroll_url, json=payload, timeout=timeout, ssl=verify_tls
        ) as response:
            body = await response.json(content_type=None)
            if response.status == 400:
                detail = _detail(body)
                raise EnrollmentRejected(f"enroll code refused by the server: {detail}")
            if response.status >= 400:
                raise EnrollmentUnavailable(
                    f"enrollment failed: HTTP {response.status} {_detail(body)}"
                )
    except aiohttp.ClientError as exc:
        raise EnrollmentUnavailable(f"enrollment failed: {exc}") from exc
    except TimeoutError as exc:
        raise EnrollmentUnavailable("enrollment failed: timeout") from exc

    if not isinstance(body, dict):
        raise EnrollmentUnavailable("enrollment failed: unexpected response body")
    device_id = body.get("device_id")
    device_token = body.get("device_token")
    if not isinstance(device_id, str) or not isinstance(device_token, str):
        raise EnrollmentUnavailable("enrollment failed: response misses device_id or device_token")
    return EnrollmentResult(device_id=device_id, device_token=device_token)


def _detail(body: object) -> str:
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if value:
                return str(value)
    return ""
