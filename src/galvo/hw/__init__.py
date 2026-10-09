"""Hardware backends behind one interface. The choice is always explicit, never a fallback."""

from __future__ import annotations

from typing import Literal

from galvo.config import RigConfig
from galvo.hw.base import (
    Clock,
    DaqBackend,
    FakeClock,
    HardwareError,
    RealClock,
    VoltageLimitError,
)
from galvo.hw.simulated import SimulatedBackend

BackendKind = Literal["simulated", "nidaqmx"]

__all__ = [
    "BackendKind",
    "Clock",
    "DaqBackend",
    "FakeClock",
    "HardwareError",
    "RealClock",
    "SimulatedBackend",
    "VoltageLimitError",
    "open_backend",
]


def open_backend(kind: BackendKind, config: RigConfig, clock: Clock | None = None) -> DaqBackend:
    clock = clock or RealClock()
    if kind == "simulated":
        return SimulatedBackend(config, clock)
    if kind == "nidaqmx":
        from galvo.hw.nidaqmx_backend import open_nidaqmx

        return open_nidaqmx(config, clock)
    raise ValueError(f"unknown backend {kind!r}")
