"""The NI backend, driven against a fake ``nidaqmx`` module (runs on any OS).

The fake records every call and, like the real driver, refuses to let two tasks hold
the analog-output channels at once.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from galvo.calibration import Calibration
from galvo.config import RigConfig
from galvo.hw import FakeClock, VoltageLimitError, open_backend
from galvo.hw.nidaqmx_backend import BackendUnavailableError, NIDAQmxBackend
from galvo.patterns import LineTarget, PointTarget
from galvo.sequencer import ExposureRequest, execute, plan_exposure


class FakeDriver:
    def __init__(self, devices: list[str]) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.open_ao: set[int] = set()
        self.di_values: list[bool] = []
        self.devices = devices


class FakeTask:
    driver: FakeDriver

    def __init__(self) -> None:
        self.kind = ""
        self.names: list[str] = []
        self.ao_channels = SimpleNamespace(add_ao_voltage_chan=self._add_ao)
        self.do_channels = SimpleNamespace(add_do_chan=self._add_do)
        self.di_channels = SimpleNamespace(add_di_chan=self._add_di)
        self.timing = SimpleNamespace(cfg_samp_clk_timing=self._timing)

    def _add_ao(self, name: str, min_val: float, max_val: float) -> None:
        if not self.names and id(self) not in self.driver.open_ao and self.driver.open_ao:
            raise RuntimeError("AO channels already reserved by another task")
        self.kind = "ao"
        self.names.append(name)
        self.driver.open_ao.add(id(self))
        self.driver.calls.append(("add_ao", name, min_val, max_val))

    def _add_do(self, name: str, line_grouping: object) -> None:
        self.kind = "do"
        self.driver.calls.append(("add_do", name))

    def _add_di(self, name: str) -> None:
        self.kind = "di"
        self.driver.calls.append(("add_di", name))

    def _timing(self, rate: float, sample_mode: object, samps_per_chan: int) -> None:
        self.driver.calls.append(("timing", rate, samps_per_chan))

    def write(self, data: Any, auto_start: bool = False) -> None:
        self.driver.calls.append((f"write_{self.kind}", data))

    def start(self) -> None:
        self.driver.calls.append(("start", self.kind))

    def wait_until_done(self, timeout: float) -> None:
        self.driver.calls.append(("wait", timeout))

    def read(self) -> bool:
        return self.driver.di_values.pop(0) if self.driver.di_values else False

    def close(self) -> None:
        self.driver.open_ao.discard(id(self))
        self.driver.calls.append(("close", self.kind))


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch, config: RigConfig) -> FakeDriver:
    drv = FakeDriver([config.daq.device])
    task_cls = type("Task", (FakeTask,), {"driver": drv})
    fake = SimpleNamespace(
        Task=task_cls,
        constants=SimpleNamespace(
            LineGrouping=SimpleNamespace(CHAN_FOR_ALL_LINES="all"),
            AcquisitionType=SimpleNamespace(FINITE="finite"),
        ),
        system=SimpleNamespace(
            System=SimpleNamespace(
                local=lambda: SimpleNamespace(
                    devices=[SimpleNamespace(name=n) for n in drv.devices]
                )
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "nidaqmx", fake)
    return drv


def writes(driver: FakeDriver, kind: str) -> list[Any]:
    return [c[1] for c in driver.calls if c[0] == f"write_{kind}"]


def test_gates_are_driven_low_before_anything_else(driver: FakeDriver, config: RigConfig) -> None:
    backend = open_backend("nidaqmx", config, FakeClock())
    assert isinstance(backend, NIDAQmxBackend) and not backend.simulated
    first_write = next(c for c in driver.calls if c[0].startswith("write"))
    assert first_write == ("write_do", 0)
    assert ("add_do", f"{config.daq.device}/port0/line0:7") in driver.calls


def test_missing_device_names_what_is_present(driver: FakeDriver, config: RigConfig) -> None:
    driver.devices = ["Dev7"]
    with pytest.raises(BackendUnavailableError, match="Dev7"):
        NIDAQmxBackend(config, FakeClock())


def test_set_position_and_lines(driver: FakeDriver, config: RigConfig) -> None:
    backend = NIDAQmxBackend(config, FakeClock())
    backend.set_position((1.5, -2.0))
    assert writes(driver, "ao") == [[1.5, -2.0]]
    backend.set_lines(0b100)
    assert writes(driver, "do")[-1] == 0b100 and backend.lines == 0b100
    with pytest.raises(VoltageLimitError):
        backend.set_position((0.0, 12.0))
    assert len(writes(driver, "ao")) == 1  # nothing out of range reached the device


def test_scan_is_hardware_timed_without_channel_clash(
    driver: FakeDriver, config: RigConfig
) -> None:
    backend = NIDAQmxBackend(config, FakeClock())
    samples = np.column_stack([np.linspace(-1, 1, 50), np.zeros(50)])
    backend.write_waveform(samples)  # the fake raises if AO channels were double-booked
    assert ("timing", config.daq.sample_rate_hz, 50) in driver.calls
    data = writes(driver, "ao")[-1]
    assert np.asarray(data).shape == (2, 50)  # one row per channel
    assert ("start", "ao") in driver.calls
    backend.set_position((0.0, 0.0))  # the on-demand task is back afterwards


def test_close_drives_gates_low_and_releases(driver: FakeDriver, config: RigConfig) -> None:
    backend = NIDAQmxBackend(config, FakeClock())
    backend.set_lines(0b101)
    backend.close()
    assert writes(driver, "do")[-1] == 0
    assert driver.open_ao == set()


def test_trigger_waits_for_rising_edge(
    driver: FakeDriver, synced_config: RigConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = NIDAQmxBackend(synced_config, FakeClock())
    driver.di_values = [True, True, False, False, True]  # already high: must see low first
    t = backend.wait_for_trigger(1.0)
    assert t == pytest.approx(4 * 0.0005)
    driver.di_values = []
    with pytest.raises(TimeoutError):
        backend.wait_for_trigger(0.01)


def test_full_exposure_through_ni_backend(
    driver: FakeDriver, config: RigConfig, calibration: Calibration
) -> None:
    backend = NIDAQmxBackend(config, FakeClock())
    laser = config.lasers[0]
    for pattern in (PointTarget((256, 256)), LineTarget((100, 100), (300, 100), 5)):
        plan = plan_exposure(ExposureRequest(pattern, laser.name, 0.01), config, calibration)
        result = execute(plan, backend)
        assert result.status == "completed"
        assert backend.lines == 0
    gate_writes = writes(driver, "do")
    assert 1 << laser.do_line in gate_writes
    assert gate_writes[-1] == 0
