"""Home Assistant log surveillance (W4.3).

The agent polls the Supervisor log endpoint (``GET /core/logs?lines=N``, plain text),
parses Home Assistant log records and turns warnings/errors into ``event`` messages.

Anti-spam strategy, in three layers (all sizes configurable):

1. **Line-level replay guard**: every poll returns the same tail of the journal, so a
   fingerprint of each already-processed record is kept in a bounded ring
   (``_PROCESSED_RING``); a record is only considered once.
2. **Content deduplication**: records are keyed by ``(level, source, normalized
   message)`` where volatile parts (numbers, hex blobs, UUIDs, paths) are masked. A key
   already reported is suppressed for ``dedup_ttl_s`` (30 min by default); the number of
   suppressed repeats is appended to the next report of that key
   (``... [repeated 12 times in the last 30 min]``).
3. **Rate limiting**: a token bucket of ``max_per_hour`` events (refilled continuously)
   bounds the outgoing rate whatever happens in the logs. Events dropped by the bucket
   are counted and summarized in a single ``event`` once capacity is available again.

Only records at or above ``min_level`` (``error`` by default) are reported.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from hasup_protocol import EventLevel, EventPayload

from .supervisor import SupervisorClient, SupervisorError

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 500
_PROCESSED_RING = 2000

_LEVEL_BY_NAME: dict[str, EventLevel] = {
    "WARNING": EventLevel.WARNING,
    "ERROR": EventLevel.ERROR,
    "CRITICAL": EventLevel.CRITICAL,
    "FATAL": EventLevel.CRITICAL,
}
_LEVEL_ORDER: dict[EventLevel, int] = {
    EventLevel.WARNING: 10,
    EventLevel.ERROR: 20,
    EventLevel.CRITICAL: 30,
}

# "2026-09-01 12:02:13.123 ERROR (MainThread) [homeassistant.components.recorder] Message"
_RECORD_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*)\s+"
    r"(?P<level>WARNING|ERROR|CRITICAL|FATAL)\s+"
    r"(?:\([^)]*\)\s+)?"
    r"\[(?P<source>[^\]]+)\]\s+"
    r"(?P<message>.*)"
)
_RECORD_START_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_NORMALIZE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<addr>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{6,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


@dataclass(frozen=True)
class LogRecord:
    """One parsed Home Assistant log record."""

    timestamp: str
    level: EventLevel
    source: str
    message: str

    @property
    def fingerprint(self) -> str:
        return f"{self.timestamp}|{self.level}|{self.source}|{self.message[:200]}"


def parse_log_records(text: str) -> list[LogRecord]:
    """Parse a chunk of Home Assistant logs into warning-or-worse records.

    Continuation lines (tracebacks) are appended to the record they belong to.
    """
    records: list[LogRecord] = []
    pending_lines: list[str] = []
    current: LogRecord | None = None

    def flush() -> None:
        nonlocal current, pending_lines
        if current is not None:
            message = current.message
            if pending_lines:
                message = f"{message} | {' '.join(pending_lines)}"
            records.append(
                LogRecord(
                    timestamp=current.timestamp,
                    level=current.level,
                    source=current.source,
                    message=_truncate(message),
                )
            )
        current = None
        pending_lines = []

    for raw_line in text.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        if not line:
            continue
        match = _RECORD_RE.search(line)
        if match:
            flush()
            level = _LEVEL_BY_NAME.get(match.group("level").upper())
            if level is None:
                continue
            current = LogRecord(
                timestamp=match.group("ts"),
                level=level,
                source=match.group("source").strip(),
                message=match.group("message").strip(),
            )
        elif current is not None and not _RECORD_START_RE.match(line):
            if len(pending_lines) < 5:
                pending_lines.append(line.strip())
        else:
            flush()
    flush()
    return records


def normalize_message(message: str) -> str:
    """Mask volatile parts so repeats of the same problem share one key."""
    normalized = message.lower()
    for pattern, replacement in _NORMALIZE_RULES:
        normalized = pattern.sub(replacement, normalized)
    return normalized.strip()[:200]


class TokenBucket:
    """Classic token bucket, used to bound the outgoing event rate."""

    def __init__(
        self, capacity: float, refill_per_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._clock = clock
        self._tokens = capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)

    def consume(self, amount: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens


class EventThrottle:
    """Deduplication + rate limiting on top of parsed log records."""

    def __init__(
        self,
        *,
        max_per_hour: int = 20,
        dedup_ttl_s: float = 1800.0,
        min_level: EventLevel = EventLevel.ERROR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bucket = TokenBucket(float(max_per_hour), max_per_hour / 3600.0, clock)
        self._dedup_ttl_s = dedup_ttl_s
        self._min_level = min_level
        self._clock = clock
        self._processed: deque[str] = deque(maxlen=_PROCESSED_RING)
        self._processed_set: set[str] = set()
        # key -> (last_sent_monotonic, suppressed_count)
        self._last_sent: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._rate_limited_count = 0

    def seen(self, record: LogRecord) -> bool:
        return record.fingerprint in self._processed_set

    def mark_seen(self, record: LogRecord) -> None:
        fingerprint = record.fingerprint
        if fingerprint in self._processed_set:
            return
        if len(self._processed) == self._processed.maxlen:
            self._processed_set.discard(self._processed[0])
        self._processed.append(fingerprint)
        self._processed_set.add(fingerprint)

    def offer(self, record: LogRecord) -> EventPayload | None:
        """Return the event to send for this record, or ``None`` if suppressed."""
        if _LEVEL_ORDER[record.level] < _LEVEL_ORDER[self._min_level]:
            return None
        if self.seen(record):
            return None
        self.mark_seen(record)

        key = f"{record.level}|{record.source}|{normalize_message(record.message)}"
        now = self._clock()
        entry = self._last_sent.get(key)
        if entry is not None:
            last_sent, suppressed = entry
            if now - last_sent < self._dedup_ttl_s:
                self._last_sent[key] = (last_sent, suppressed + 1)
                return None
            repeats = suppressed
        else:
            repeats = 0

        if not self._bucket.consume():
            self._rate_limited_count += 1
            return None

        self._last_sent[key] = (now, 0)
        self._prune(now)

        message = record.message
        if repeats:
            minutes = int(self._dedup_ttl_s // 60)
            message = f"{message} [repeated {repeats} times in the last {minutes} min]"
        return EventPayload(level=record.level, source=record.source, message=_truncate(message))

    def drain_rate_limit_summary(self) -> EventPayload | None:
        """One event summarizing what the rate limiter dropped, when possible."""
        if not self._rate_limited_count:
            return None
        if not self._bucket.consume():
            return None
        count = self._rate_limited_count
        self._rate_limited_count = 0
        return EventPayload(
            level=EventLevel.WARNING,
            source="hasup_agent.logwatch",
            message=f"{count} log events suppressed by the agent rate limiter",
        )

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, (last_sent, suppressed) in self._last_sent.items()
            if now - last_sent > self._dedup_ttl_s * 4 and suppressed == 0
        ]
        for key in expired:
            self._last_sent.pop(key, None)


class LogWatcher:
    """Polls the Supervisor for Home Assistant core logs and yields events."""

    def __init__(
        self,
        client: SupervisorClient,
        *,
        lines: int = 200,
        max_per_hour: int = 20,
        dedup_ttl_s: float = 1800.0,
        min_level: EventLevel = EventLevel.ERROR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._lines = lines
        self._throttle = EventThrottle(
            max_per_hour=max_per_hour,
            dedup_ttl_s=dedup_ttl_s,
            min_level=min_level,
            clock=clock,
        )
        self._primed = False

    async def poll(self) -> list[EventPayload]:
        """Fetch the log tail and return the events worth sending."""
        try:
            text = await self._client.core_logs(lines=self._lines)
        except SupervisorError as exc:
            logger.warning("core logs unavailable: %s", exc)
            return []

        records = parse_log_records(text)
        if not self._primed:
            # First poll: absorb the existing backlog so a restart of the agent does not
            # replay hours-old errors as fresh events.
            self._primed = True
            for record in records:
                self._throttle.mark_seen(record)
            logger.info("log watcher primed with %d existing records", len(records))
            return []

        events = [event for record in records if (event := self._throttle.offer(record))]
        summary = self._throttle.drain_rate_limit_summary()
        if summary is not None:
            events.append(summary)
        return events


def _truncate(message: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    message = message.strip()
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."
