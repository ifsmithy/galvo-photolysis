"""Real NI USB-6001 backend, via NI's ``nidaqmx`` Python package (Windows, with NI-DAQmx).

``nidaqmx`` is imported only when this backend is opened, so the rest of the code (and
the Mac) never needs it.

How the USB-6001 is driven:

* **Gates** (laser TTL lines): one digital-output task on the whole port, written as a
  bit mask. The very first thing the backend does is drive every line low, because the
  405 driver turns ON when its line is not actively driven (HARDWARE.md).
* **Mirrors, one position**: an on-demand (software-timed) analog-output task writing
  one sample to both channels.
* **Mirrors, a scan**: a finite, hardware-timed analog-output task at the sample rate.
  The on-demand task is released for the scan and re-created afterwards; the outputs
  hold the scan's last value in between.
* **Trigger in**: a digital input polled for a rising edge (software-timed, ~1 ms).

Timestamps are taken from the clock around each call, so they are software timestamps
(about 1 ms on Windows), as the exposure log states.
"""

from __future__ import annotations

import contextlib
from typing import Any

import numpy as np

from galvo.config import RigConfig
from galvo.geometry import Points
from galvo.hw.base import Clock, DaqBackend, HardwareError

POLL_INTERVAL_S = 0.0005


class BackendUnavailableError(HardwareError):
    pass


def _import_nidaqmx() -> Any:
    try:
        import nidaqmx
    except ImportError as exc:
        raise BackendUnavailableError(
            "nidaqmx is not installed. The NI backend runs only on Windows with NI-DAQmx; "
            "install it with: uv sync --extra nidaq"
        ) from exc
    return nidaqmx


class NIDAQmxBackend(DaqBackend):
    name = "nidaqmx"
    simulated = False

    def __init__(self, config: RigConfig, clock: Clock) -> None:
        super().__init__(config, clock)
        self._nidaqmx = _import_nidaqmx()
        daq = config.daq
        self._device = daq.device
        self._check_device_present()

        self._ao_names = [f"{daq.device}/{ch}" for ch in daq.ao_channels]
        self._ao_static: Any = None
        self._do: Any = None
        self._di: Any = None
        self.closed = False
        try:
            # Gates first: every line low before anything else can happen.
            self._do = self._nidaqmx.Task()
            last = daq.do_line_count - 1
            line_grouping = self._nidaqmx.constants.LineGrouping.CHAN_FOR_ALL_LINES
            self._do.do_channels.add_do_chan(
                f"{daq.device}/{daq.do_port}/line0:{last}", line_grouping=line_grouping
            )
            self._do.write(0, auto_start=True)
            self._lines = 0

            self._ao_static = self._new_ao_task()
            if config.sync.trigger_in_line is not None:
                self._di = self._nidaqmx.Task()
                self._di.di_channels.add_di_chan(f"{daq.device}/{config.sync.trigger_in_line}")
        except BaseException:
            self._close_tasks()
            raise

    # DaqBackend

    def set_position(self, ao: tuple[float, float]) -> float:
        self._check_open()
        pts = self._check_volts(np.array([ao]))
        self._ao_static.write([float(pts[0, 0]), float(pts[0, 1])], auto_start=True)
        return self.clock.monotonic()

    def write_waveform(self, samples: Points) -> tuple[float, float]:
        self._check_open()
        pts = self._check_volts(samples)
        n = len(pts)
        rate = self.config.daq.sample_rate_hz
        acquisition = self._nidaqmx.constants.AcquisitionType.FINITE

        # The channels can belong to only one task at a time.
        self._ao_static.close()
        self._ao_static = None
        task = self._new_ao_task()
        try:
            task.timing.cfg_samp_clk_timing(rate, sample_mode=acquisition, samps_per_chan=n)
            task.write(np.ascontiguousarray(pts.T), auto_start=False)
            start = self.clock.monotonic()
            task.start()
            task.wait_until_done(timeout=n / rate + 5.0)
            end = self.clock.monotonic()
        finally:
            task.close()
            self._ao_static = self._new_ao_task()
        return start, end

    def set_lines(self, mask: int) -> float:
        self._check_open()
        mask = self._check_mask(mask)
        self._do.write(mask, auto_start=True)
        self._lines = mask
        return self.clock.monotonic()

    def wait_for_trigger(self, timeout_s: float) -> float:
        self._check_open()
        if self._di is None:
            raise HardwareError("no trigger-in line configured in rig.toml [sync]")
        deadline = self.clock.monotonic() + timeout_s
        seen_low = False
        while self.clock.monotonic() < deadline:
            high = bool(self._di.read())
            if high and seen_low:
                return self.clock.monotonic()
            seen_low = seen_low or not high
            self.clock.sleep(POLL_INTERVAL_S)
        raise TimeoutError(f"no trigger within {timeout_s} s")

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._do is not None:
                self._do.write(0, auto_start=True)
                self._lines = 0
        finally:
            self._close_tasks()
            self.closed = True

    # Helpers

    def _new_ao_task(self) -> Any:
        daq = self.config.daq
        task = self._nidaqmx.Task()
        for name in self._ao_names:
            task.ao_channels.add_ao_voltage_chan(name, min_val=daq.ao_min_v, max_val=daq.ao_max_v)
        return task

    def _check_device_present(self) -> None:
        try:
            names = [d.name for d in self._nidaqmx.system.System.local().devices]
        except Exception as exc:
            raise BackendUnavailableError(f"NI-DAQmx driver not usable: {exc}") from exc
        if self._device not in names:
            found = ", ".join(names) or "none"
            raise BackendUnavailableError(
                f"DAQ device {self._device!r} (rig.toml [daq] device) not found. "
                f"Devices present: {found}. Check NI MAX for the name."
            )

    def _close_tasks(self) -> None:
        for task in (self._ao_static, self._do, self._di):
            if task is not None:
                with contextlib.suppress(Exception):
                    task.close()
        self._ao_static = self._do = self._di = None

    def _check_open(self) -> None:
        if self.closed:
            raise HardwareError("backend is closed")


def open_nidaqmx(config: RigConfig, clock: Clock) -> NIDAQmxBackend:
    return NIDAQmxBackend(config, clock)
