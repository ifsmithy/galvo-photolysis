from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from galvo.calibration import Calibration
from galvo.config import RigConfig, parse_config
from galvo.explog import ExposureLog
from galvo.geometry import Affine2D
from galvo.hw import FakeClock, SimulatedBackend
from galvo.patterns import LineTarget, PointTarget, RegionTarget
from galvo.sequencer import (
    AimTarget,
    ExposureRequest,
    InterlockError,
    MoveTo,
    PlanError,
    Scan,
    SetLines,
    check_steps,
    execute,
    plan_exposure,
    run_aim,
)

CENTRE = PointTarget((256, 256))


def test_point_plan_order(config: RigConfig, calibration: Calibration) -> None:
    plan = plan_exposure(ExposureRequest(CENTRE, "Laser 2", 0.1), config, calibration)
    kinds = [type(s).__name__ for s in plan.steps]
    assert kinds == ["SetLines", "MoveTo", "Settle", "SetLines", "Hold", "SetLines"]
    gate = plan.steps[3]
    assert isinstance(gate, SetLines) and gate.mask == 1 << config.laser("Laser 2").do_line
    assert plan.expected_duration_s == pytest.approx(0.1 + config.galvo.settling_time_s)


def test_pixels_reach_channels_through_calibration_and_axes(
    config: RigConfig, calibration: Calibration
) -> None:
    # Default calibration: pixel (512, 256) is logical (+10, 0).
    # The original axis convention then gives (ao0, ao1) = (y, -x) = (0, -10).
    plan = plan_exposure(
        ExposureRequest(PointTarget((512, 256)), "Laser 1", 0.01), config, calibration
    )
    assert np.allclose(plan.segments_ao[0], [[0.0, -10.0]])


def test_line_is_one_hardware_timed_scan(config: RigConfig, calibration: Calibration) -> None:
    plan = plan_exposure(
        ExposureRequest(LineTarget((0, 0), (100, 0), 5), "Laser 1", 0.002), config, calibration
    )
    (scan,) = [s for s in plan.steps if isinstance(s, Scan)]
    assert len(scan.samples) == 5 * 10  # 2 ms at 5 kHz = 10 samples per spot


def test_rejects_targets_outside_galvo_range(config: RigConfig, calibration: Calibration) -> None:
    with pytest.raises(PlanError, match="outside the galvo range"):
        plan_exposure(
            ExposureRequest(PointTarget((600, 256)), "Laser 1", 0.01), config, calibration
        )


def test_rounding_at_the_edge_is_accepted(config: RigConfig, calibration: Calibration) -> None:
    # A hair outside -10 V from floating-point rounding must not refuse an edge target...
    nudged = Calibration(
        Affine2D(calibration.px_to_galvo.matrix + np.array([[0, 0, -1e-10], [0, 0, 0]])),
        calibration.pixel_points,
        calibration.galvo_points,
        calibration.created_utc,
        is_default=True,
    )
    plan = plan_exposure(ExposureRequest(PointTarget((0, 0)), "Laser 1", 0.01), config, nudged)
    assert np.abs(plan.segments_ao[0]).max() <= 10.0
    # ...but a real overshoot still is.
    with pytest.raises(PlanError):
        plan_exposure(ExposureRequest(PointTarget((-1, 0)), "Laser 1", 0.01), config, nudged)


def test_rejects_dwell_below_one_sample(config: RigConfig, calibration: Calibration) -> None:
    with pytest.raises(PlanError, match="one sample"):
        plan_exposure(ExposureRequest(CENTRE, "Laser 1", 1e-5), config, calibration)


def test_sync_requires_wiring(config: RigConfig, calibration: Calibration) -> None:
    with pytest.raises(PlanError, match="trigger_in_line"):
        plan_exposure(
            ExposureRequest(CENTRE, "Laser 1", 0.01, wait_for_trigger=True), config, calibration
        )
    with pytest.raises(PlanError, match="trigger_out_line"):
        plan_exposure(
            ExposureRequest(CENTRE, "Laser 1", 0.01, camera_trigger=True), config, calibration
        )


def test_invariant_no_moves_with_gate_open() -> None:
    with pytest.raises(PlanError, match="gate open"):
        check_steps([SetLines(0, "x"), SetLines(1, "gate_open"), MoveTo((0, 0)), SetLines(0, "y")])
    with pytest.raises(PlanError, match="end"):
        check_steps([SetLines(0, "x"), SetLines(1, "gate_open")])


def test_every_plan_moves_only_with_gates_closed(
    config: RigConfig, calibration: Calibration
) -> None:
    u = (
        (300, 300),
        (460, 300),
        (460, 460),
        (420, 460),
        (420, 340),
        (340, 340),
        (340, 460),
        (300, 460),
    )
    plan = plan_exposure(
        ExposureRequest(RegionTarget(u, 10), "Laser 2", 0.001), config, calibration
    )
    check_steps(plan.steps)
    assert sum(isinstance(s, MoveTo) for s in plan.steps) == len(plan.segments_ao) > 1


def test_execute_point_logs_exposure(
    tmp_path: Path, config: RigConfig, calibration: Calibration, backend: SimulatedBackend
) -> None:
    log = ExposureLog(tmp_path / "log.jsonl")
    plan = plan_exposure(
        ExposureRequest(CENTRE, "Laser 2", 0.1, note="cell 3"), config, calibration
    )
    result = execute(plan, backend, log)

    assert result.status == "completed"
    ((on, off),) = result.gate_intervals
    assert off - on == pytest.approx(0.1, abs=1e-3)
    assert on >= config.galvo.settling_time_s  # gate opened only after settling
    assert backend.lines == 0

    (rec,) = log.read()
    assert rec["note"] == "cell 3"
    assert rec["backend"] == {"name": "simulated", "simulated": True}
    assert rec["laser"]["name"] == "Laser 2"
    assert rec["targets_px"] == [[[256.0, 256.0]]]
    assert rec["calibration"]["is_default"] is True
    assert rec["rig_config_sha256"] == config.source_sha256
    assert "axis convention" in rec["unverified_rig_facts"]
    assert rec["started_utc"].startswith("2026-01-01T00:00:00")


def test_execute_with_sync(
    tmp_path: Path, synced_config: RigConfig, calibration: Calibration
) -> None:
    backend = SimulatedBackend(synced_config, FakeClock(), trigger_delay_s=0.5)
    req = ExposureRequest(CENTRE, "Laser 1", 0.02, wait_for_trigger=True, camera_trigger=True)
    result = execute(plan_exposure(req, synced_config, calibration), backend)
    kinds = [e.kind for e in result.events]
    assert kinds.index("trigger_in") < kinds.index("trigger_out") < kinds.index("gate_open")
    trigger = next(e for e in result.events if e.kind == "trigger_in")
    assert trigger.t_s == pytest.approx(synced_config.galvo.settling_time_s + 0.5)


def test_interlock_refuses_when_gate_already_open(
    config: RigConfig, calibration: Calibration, backend: SimulatedBackend
) -> None:
    backend.set_lines(1)
    plan = plan_exposure(ExposureRequest(CENTRE, "Laser 1", 0.01), config, calibration)
    with pytest.raises(InterlockError):
        execute(plan, backend)


def test_abort_closes_gates_and_is_logged(
    tmp_path: Path, config: RigConfig, calibration: Calibration, backend: SimulatedBackend
) -> None:
    log = ExposureLog(tmp_path / "log.jsonl")
    plan = plan_exposure(ExposureRequest(CENTRE, "Laser 1", 1.0), config, calibration)
    clock = backend.clock
    result = execute(plan, backend, log, should_abort=lambda: clock.monotonic() > 0.3)

    assert result.status == "aborted"
    assert backend.lines == 0
    ((on, off),) = result.gate_intervals
    assert off - on < 0.31
    assert log.read()[0]["status"] == "aborted"


def test_hardware_failure_closes_gates_logs_and_raises(
    tmp_path: Path,
    config: RigConfig,
    calibration: Calibration,
    backend: SimulatedBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = ExposureLog(tmp_path / "log.jsonl")
    plan = plan_exposure(
        ExposureRequest(LineTarget((0, 0), (100, 0), 5), "Laser 1", 0.01), config, calibration
    )

    def broken(samples: object) -> tuple[float, float]:
        raise RuntimeError("USB unplugged")

    monkeypatch.setattr(backend, "write_waveform", broken)
    with pytest.raises(RuntimeError, match="USB unplugged"):
        execute(plan, backend, log)
    assert backend.lines == 0
    (rec,) = log.read()
    assert rec["status"] == "failed" and "USB unplugged" in rec["error"]
    assert rec["events"][-1]["event"] == "gates_closed_on_exit"


def _aim_config(raw: dict[str, Any]) -> RigConfig:
    raw["lasers"][0]["role"] = "aim"
    return parse_config(raw)


def test_aim_follows_targets_with_gate_open(
    raw_config: dict[str, Any], calibration: Calibration, tmp_path: Path
) -> None:
    config = _aim_config(raw_config)
    aim = config.lasers[0]
    backend = SimulatedBackend(config, FakeClock())
    log = ExposureLog(tmp_path / "log.jsonl")
    plan = plan_exposure(
        ExposureRequest(CENTRE, aim.name, 60.0, until_stopped=True), config, calibration
    )
    targets: queue.Queue[AimTarget | None] = queue.Queue()
    for px in ((300.0, 256.0), (320.0, 280.0)):
        ao = plan_exposure(
            ExposureRequest(PointTarget(px), aim.name, 0.01), config, calibration
        ).segments_ao[0][0]
        targets.put(((float(ao[0]), float(ao[1])), px))
    targets.put(None)

    result = run_aim(plan, backend, targets, log)
    assert result.status == "stopped" and result.mode == "aim"
    assert backend.lines == 0
    kinds = [e.kind for e in result.events]
    assert kinds == ["gates_closed", "move", "gate_open", "move", "move", "gate_close"]
    (rec,) = log.read()
    assert rec["mode"] == "aim"
    assert [e["px"] for e in rec["events"] if e["event"] == "move"] == [
        [256.0, 256.0],
        [300.0, 256.0],
        [320.0, 280.0],
    ]


def test_only_the_aim_laser_may_follow(
    config: RigConfig, calibration: Calibration, backend: SimulatedBackend
) -> None:
    plan = plan_exposure(
        ExposureRequest(CENTRE, "Laser 2", 60.0, until_stopped=True), config, calibration
    )
    with pytest.raises(PlanError, match="not the aiming laser"):
        run_aim(plan, backend, queue.Queue())
    assert backend.lines == 0
