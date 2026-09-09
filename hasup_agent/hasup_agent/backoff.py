"""Reconnection backoff: exponential with jitter, capped at 5 minutes.

``docs/protocol.md`` section 1: "backoff exponentiel avec jitter, borne a 5 minutes
(1 s, 2 s, 4 s, ..., 300 s max)". Full jitter is used (a uniform draw in
``[0, computed_delay]``) so a fleet of agents does not reconnect in lockstep after a
server restart, with a small floor to avoid a hot reconnect loop.
"""

from __future__ import annotations

import random

MAX_BACKOFF_S = 300.0
INITIAL_BACKOFF_S = 1.0
# Never return a delay below this, even when the jitter draw is close to zero.
MIN_DELAY_S = 0.5


class ExponentialBackoff:
    """Successive reconnection delays; reset after a successful session."""

    def __init__(
        self,
        initial_s: float = INITIAL_BACKOFF_S,
        max_s: float = MAX_BACKOFF_S,
        factor: float = 2.0,
        min_delay_s: float = MIN_DELAY_S,
        rng: random.Random | None = None,
    ) -> None:
        self.initial_s = initial_s
        self.max_s = max_s
        self.factor = factor
        self.min_delay_s = min_delay_s
        self._rng = rng or random.Random()
        self._attempt = 0

    @property
    def attempt(self) -> int:
        """Number of delays handed out since the last reset."""
        return self._attempt

    def ceiling(self) -> float:
        """Upper bound of the next delay, before jitter."""
        return min(self.initial_s * (self.factor**self._attempt), self.max_s)

    def next_delay(self) -> float:
        delay = self._rng.uniform(0.0, self.ceiling())
        self._attempt += 1
        return max(self.min_delay_s, min(delay, self.max_s))

    def reset(self) -> None:
        self._attempt = 0
