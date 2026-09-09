"""Host metrics read from the container's ``/proc`` and ``/sys``.

An add-on container shares the host kernel, so ``/proc/stat``, ``/proc/meminfo`` and
``/proc/uptime`` describe the **host** (the Home Assistant box), not the container.
That is the only way to obtain host-wide CPU, memory and uptime: the Supervisor API
exposes per-container statistics (``/supervisor/stats``, ``/core/stats``) but no
host-wide CPU/RAM endpoint. Disk usage does come from the Supervisor (``/host/info``).

Both roots are configurable so the tests can point them at a fixture tree.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CpuSample:
    total: float
    idle: float


class HostMetricsReader:
    """Reads host CPU / memory / uptime / temperature from procfs and sysfs."""

    def __init__(self, procfs_root: Path = Path("/proc"), sysfs_root: Path = Path("/sys")) -> None:
        self.procfs_root = procfs_root
        self.sysfs_root = sysfs_root
        self._previous_cpu: CpuSample | None = None
        self._started_monotonic = time.monotonic()

    # ------------------------------------------------------------------ cpu

    def _read_cpu_sample(self) -> CpuSample | None:
        try:
            first_line = (self.procfs_root / "stat").read_text(encoding="utf-8").split("\n", 1)[0]
        except OSError:
            return None
        fields = first_line.split()
        if not fields or fields[0] != "cpu":
            return None
        try:
            values = [float(value) for value in fields[1:]]
        except ValueError:
            return None
        if len(values) < 5:
            return None
        # user nice system idle iowait irq softirq steal ...
        idle = values[3] + values[4]
        return CpuSample(total=sum(values[:8]), idle=idle)

    def cpu_percent(self) -> float | None:
        """CPU usage since the previous call (``None`` on the very first call)."""
        sample = self._read_cpu_sample()
        if sample is None:
            return None
        previous = self._previous_cpu
        self._previous_cpu = sample
        if previous is None:
            return None
        total_delta = sample.total - previous.total
        idle_delta = sample.idle - previous.idle
        if total_delta <= 0:
            return None
        usage = 100.0 * (total_delta - idle_delta) / total_delta
        return max(0.0, min(100.0, usage))

    def cpu_percent_blocking(self, interval_s: float = 0.3) -> float | None:
        """Take two samples around a short sleep; used for the first measurement."""
        self._read_cpu_sample()
        self._previous_cpu = self._read_cpu_sample()
        if self._previous_cpu is None:
            return None
        time.sleep(interval_s)
        return self.cpu_percent()

    # --------------------------------------------------------------- memory

    def memory_mb(self) -> tuple[float, float] | None:
        """``(used_mb, total_mb)`` from ``/proc/meminfo``."""
        try:
            content = (self.procfs_root / "meminfo").read_text(encoding="utf-8")
        except OSError:
            return None
        values: dict[str, float] = {}
        for line in content.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if not parts:
                continue
            try:
                values[key] = float(parts[0])  # kB
            except ValueError:
                continue
        total_kb = values.get("MemTotal")
        if not total_kb:
            return None
        available_kb = values.get("MemAvailable")
        if available_kb is None:
            if not all(key in values for key in ("MemFree", "Buffers", "Cached")):
                return None
            free = values["MemFree"]
            buffers = values.get("Buffers", 0.0)
            cached = values.get("Cached", 0.0)
            available_kb = free + buffers + cached
        used_kb = max(0.0, total_kb - available_kb)
        return used_kb / 1024.0, total_kb / 1024.0

    # --------------------------------------------------------------- uptime

    def uptime_s(self) -> int:
        """Host uptime; falls back to the agent's own uptime if procfs is absent."""
        try:
            content = (self.procfs_root / "uptime").read_text(encoding="utf-8")
            return max(0, int(float(content.split()[0])))
        except (OSError, ValueError, IndexError):
            return max(0, int(time.monotonic() - self._started_monotonic))

    # ---------------------------------------------------------- temperature

    def temperature_c(self) -> float | None:
        """Highest plausible thermal zone reading, in degrees Celsius."""
        thermal_root = self.sysfs_root / "class" / "thermal"
        try:
            zones = sorted(thermal_root.glob("thermal_zone*"))
        except OSError:
            return None
        readings: list[float] = []
        for zone in zones:
            try:
                raw = (zone / "temp").read_text(encoding="utf-8").strip()
                milli = float(raw)
            except (OSError, ValueError):
                continue
            celsius = milli / 1000.0
            # Discard obviously bogus zones (disabled sensors report 0 or huge values).
            if -50.0 < celsius < 150.0 and celsius != 0.0:
                readings.append(celsius)
        if not readings:
            return None
        return round(max(readings), 1)
