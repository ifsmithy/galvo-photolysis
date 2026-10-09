"""A pure-Python stand-in for the NI USB-6001 and the galvo mirrors.

It enforces the same voltage limits and sample rate as the real rig, records every
command with a timestamp, and models the mirrors as a first-order lag, so
:meth:`SimulatedBackend.mirror_position` says where the beam actually was. All numbers
come from rig.toml; when the real settling time is measured, the simulator follows it.
"""

from __future__ import annotations

import math
import threading

import numpy as np
import numpy.typing as npt

from galvo.config import RigConfig
from galvo.geometry import Points
from galvo.hw.base import Clock, DaqBackend, HardwareError

# settling_time_s in rig.toml is the time to get within 1% of a jump.
_SETTLE_FRACTION = 0.01


class SimulatedBackend(DaqBackend):
    name = "simulated"
    simulated = True

    def __init__(self, config: RigConfig, clock: Clock, trigger_delay_s: float = 0.0) -> None:
        super().__init__(config, clock)
        self.trigger_delay_s = trigger_delay_s
        # Mirror commands as (time, ao0, ao1) chunks; the mirrors power on at 0 V.
        self._ao_t: list[npt.NDArray[np.float64]] = [np.array([-math.inf])]
        self._ao_v: list[Points] = [np.zeros((1, 2))]
        self.line_events: list[tuple[float, int]] = [(-math.inf, 0)]
        self.closed = False
        # The GUI reads the simulation while an exposure thread writes to it.
        self._lock = threading.Lock()

    # DaqBackend

    def set_position(self, ao: tuple[float, float]) -> float:
        self._check_open()
        pts = self._check_volts(np.array([ao]))
        with self._lock:
            t = self.clock.monotonic()
            self._ao_t.append(np.array([t]))
            self._ao_v.append(pts)
        return t

    def write_waveform(self, samples: Points) -> tuple[float, float]:
        self._check_open()
        pts = self._check_volts(samples)
        rate = self.config.daq.sample_rate_hz
        with self._lock:
            t0 = self.clock.monotonic()
            self._ao_t.append(t0 + np.arange(len(pts)) / rate)
            self._ao_v.append(pts)
        self.clock.sleep(len(pts) / rate)
        return t0, self.clock.monotonic()

    def set_lines(self, mask: int) -> float:
        self._check_open()
        with self._lock:
            self._lines = self._check_mask(mask)
            t = self.clock.monotonic()
            self.line_events.append((t, mask))
        return t

    def wait_for_trigger(self, timeout_s: float) -> float:
        self._check_open()
        if self.config.sync.trigger_in_line is None:
            raise HardwareError("no trigger-in line configured in rig.toml [sync]")
        if self.trigger_delay_s > timeout_s:
            self.clock.sleep(timeout_s)
            raise TimeoutError(f"no trigger within {timeout_s} s")
        self.clock.sleep(self.trigger_delay_s)
        return self.clock.monotonic()

    def close(self) -> None:
        if not self.closed:
            self.set_lines(0)
            self.closed = True

    # Simulation readout

    def commands(self) -> tuple[npt.NDArray[np.float64], Points]:
        """All mirror commands so far, as (times, channel volts), including power-on."""
        with self._lock:
            return np.concatenate(self._ao_t), np.vstack(self._ao_v)

    def mirror_position(self, times: npt.ArrayLike) -> Points:
        """Where the mirrors actually were (channel volts) at each time, after lag."""
        t_cmd, v_cmd = self.commands()
        times = np.asarray(times, dtype=np.float64)
        tau = self.config.galvo.settling_time_s / math.log(1 / _SETTLE_FRACTION)

        # Commands older than 30 time constants have no visible effect (e^-30), so start
        # from a settled mirror there instead of replaying the whole session.
        first = 0
        if len(times):
            first = max(0, int(np.searchsorted(t_cmd, times.min() - 30 * tau, side="right")) - 1)
        t_cmd, v_cmd = t_cmd[first:], v_cmd[first:]

        # Position at each command instant, from the previous command's exponential approach.
        start = np.empty_like(v_cmd)
        start[0] = v_cmd[0]
        for i in range(1, len(t_cmd)):
            start[i] = _approach(start[i - 1], v_cmd[i - 1], t_cmd[i] - t_cmd[i - 1], tau)

        idx = np.clip(np.searchsorted(t_cmd, times, side="right") - 1, 0, None)
        return _approach(start[idx], v_cmd[idx], (times - t_cmd[idx])[:, None], tau)

    def lines_at(self, times: npt.ArrayLike) -> npt.NDArray[np.int64]:
        with self._lock:
            events = list(self.line_events)
        t_ev = np.array([t for t, _ in events])
        masks = np.array([m for _, m in events], dtype=np.int64)
        idx = np.searchsorted(t_ev, np.asarray(times, dtype=np.float64), side="right") - 1
        return masks[np.clip(idx, 0, None)]

    def _check_open(self) -> None:
        if self.closed:
            raise HardwareError("backend is closed")


def _approach(
    start: npt.NDArray[np.float64],
    target: npt.NDArray[np.float64],
    dt: npt.NDArray[np.float64] | float,
    tau: float,
) -> npt.NDArray[np.float64]:
    if tau == 0:
        return np.broadcast_to(
            target, np.broadcast_shapes(np.shape(target), np.shape(start))
        ).copy()
    with np.errstate(over="ignore"):
        decay = np.exp(-np.asarray(dt) / tau)
    result: npt.NDArray[np.float64] = target + (start - target) * decay
    return result
