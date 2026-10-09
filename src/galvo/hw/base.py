"""The DAQ interface, plus the clock that timestamps everything.

A backend knows only channel volts (ao0, ao1) and digital line masks. It knows nothing
about images, calibration or patterns.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Protocol, Self

import numpy as np

from galvo.config import RigConfig
from galvo.geometry import Points, as_points


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def monotonic(self) -> float:
        return time.perf_counter()

    def wall(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests and the headless demo: sleeping just advances time."""

    def __init__(self, start: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> None:
        self._start = start
        self._t = 0.0

    def monotonic(self) -> float:
        return self._t

    def wall(self) -> datetime:
        return self._start + timedelta(seconds=self._t)

    def sleep(self, seconds: float) -> None:
        self._t += max(0.0, seconds)


class HardwareError(RuntimeError):
    pass


class VoltageLimitError(HardwareError):
    pass


class DaqBackend(ABC):
    """One DAQ device driving two galvo channels and a port of gate lines."""

    #: Short name written into every exposure log record.
    name: str
    simulated: bool

    def __init__(self, config: RigConfig, clock: Clock) -> None:
        self.config = config
        self.clock = clock
        self._lines = 0

    @property
    def lines(self) -> int:
        """Current digital output mask (bit n = line n high)."""
        return self._lines

    @abstractmethod
    def set_position(self, ao: tuple[float, float]) -> float:
        """Move both mirrors now (software-timed). Returns the monotonic timestamp."""

    @abstractmethod
    def write_waveform(self, samples: Points) -> tuple[float, float]:
        """Play (N, 2) channel volts at the sample rate, hardware-timed. Blocks until done.

        Returns monotonic (start, end) timestamps.
        """

    @abstractmethod
    def set_lines(self, mask: int) -> float:
        """Set the whole digital port (software-timed). Returns the monotonic timestamp."""

    @abstractmethod
    def wait_for_trigger(self, timeout_s: float) -> float:
        """Block until a rising edge on the trigger-in line. Returns the monotonic timestamp."""

    @abstractmethod
    def close(self) -> None:
        """Close all gates and release the device."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # Shared checks: backends reject bad requests rather than clamp them.

    def _check_volts(self, values: Points) -> Points:
        pts = as_points(values)
        lo, hi = self.config.daq.ao_min_v, self.config.daq.ao_max_v
        bad = (pts < lo) | (pts > hi) | ~np.isfinite(pts)
        if bad.any():
            first = pts[bad.any(axis=1)][0]
            raise VoltageLimitError(f"channel volts {first.tolist()} outside [{lo}, {hi}]")
        return pts

    def _check_mask(self, mask: int) -> int:
        if not 0 <= mask < (1 << self.config.daq.do_line_count):
            raise HardwareError(f"line mask {mask:#x} uses lines that do not exist")
        return mask
