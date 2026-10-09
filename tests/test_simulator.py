from __future__ import annotations

import numpy as np
import pytest

from galvo.config import RigConfig
from galvo.hw import (
    FakeClock,
    HardwareError,
    SimulatedBackend,
    VoltageLimitError,
    open_backend,
)


def test_rejects_out_of_range_volts(backend: SimulatedBackend) -> None:
    with pytest.raises(VoltageLimitError):
        backend.set_position((10.5, 0.0))
    with pytest.raises(VoltageLimitError):
        backend.write_waveform(np.array([[0.0, 0.0], [0.0, -11.0]]))
    with pytest.raises(VoltageLimitError):
        backend.set_position((float("nan"), 0.0))


def test_waveform_takes_samples_over_rate(backend: SimulatedBackend) -> None:
    start, end = backend.write_waveform(np.zeros((500, 2)))
    assert end - start == pytest.approx(500 / 5000)
    times, _ = backend.commands()
    assert len(times) == 1 + 500  # power-on state + samples


def test_mirror_settles_to_one_percent_at_settling_time(
    backend: SimulatedBackend, config: RigConfig
) -> None:
    settle = config.galvo.settling_time_s
    t0 = backend.set_position((5.0, -5.0))
    pos = backend.mirror_position([t0, t0 + settle / 2, t0 + settle, t0 + 10 * settle])
    assert np.allclose(pos[0], [0, 0])  # has not moved yet
    assert 0 < pos[1, 0] < 5
    assert pos[2, 0] == pytest.approx(5 * 0.99)
    assert np.allclose(pos[3], [5, -5])


def test_lines(backend: SimulatedBackend) -> None:
    t = backend.set_lines(0b10)
    assert backend.lines == 0b10
    assert backend.lines_at([t - 1, t]).tolist() == [0, 0b10]
    with pytest.raises(HardwareError):
        backend.set_lines(1 << 8)


def test_close_shuts_gates_and_refuses_further_use(backend: SimulatedBackend) -> None:
    backend.set_lines(1)
    backend.close()
    assert backend.lines == 0
    with pytest.raises(HardwareError):
        backend.set_position((0.0, 0.0))


def test_trigger_needs_wiring(backend: SimulatedBackend, synced_config: RigConfig) -> None:
    with pytest.raises(HardwareError):
        backend.wait_for_trigger(1.0)
    wired = SimulatedBackend(synced_config, FakeClock(), trigger_delay_s=0.2)
    assert wired.wait_for_trigger(1.0) == pytest.approx(0.2)
    late = SimulatedBackend(synced_config, FakeClock(), trigger_delay_s=5.0)
    with pytest.raises(TimeoutError):
        late.wait_for_trigger(1.0)


def test_backend_choice_is_explicit(config: RigConfig) -> None:
    assert isinstance(open_backend("simulated", config, FakeClock()), SimulatedBackend)
    with pytest.raises(HardwareError):
        open_backend("nidaqmx", config, FakeClock())
