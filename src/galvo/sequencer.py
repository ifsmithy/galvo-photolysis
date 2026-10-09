"""Turns "expose this pattern with this laser" into hardware steps, runs them, logs them.

Two halves:

* :func:`plan_exposure` is pure. Pattern + calibration + rig config become an
  :class:`ExposurePlan`: a list of steps in channel volts and line masks. It can be
  inspected, tested and drawn without any hardware.
* :func:`execute` walks the steps on a backend, timestamps what actually happened, and
  writes one exposure-log record, whether the exposure completed, was aborted or failed.

Safety rules enforced here:

* Out-of-range targets are rejected at planning time, never clamped.
* The mirrors only move with every gate closed, then wait the settling time before the
  gate opens. (Kept from the original: closing the shutter while jumping between shapes.)
* An exposure refuses to start if any gate is already open. (Kept from the original.)
* All gates are closed on exit, including on error or abort.

The one deliberate exception is :func:`run_aim`: an *aiming* laser held on while the
mirrors follow the crosshair, as in the original software. It is restricted to lasers
whose role is "aim", and every position it visits is logged.
"""

from __future__ import annotations

import queue
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from galvo.calibration import Calibration
from galvo.config import LaserConfig, RigConfig
from galvo.explog import SCHEMA_VERSION, ExposureLog
from galvo.geometry import Points
from galvo.hw.base import DaqBackend
from galvo.patterns import Pattern, pattern_to_dict, rasterize

# Steps


@dataclass(frozen=True)
class MoveTo:
    ao: tuple[float, float]


@dataclass(frozen=True)
class Settle:
    seconds: float


@dataclass(frozen=True)
class SetLines:
    mask: int
    label: str


@dataclass(frozen=True)
class Hold:
    seconds: float


@dataclass(frozen=True, eq=False)
class Scan:
    samples: Points


@dataclass(frozen=True)
class WaitTrigger:
    timeout_s: float


Step = MoveTo | Settle | SetLines | Hold | Scan | WaitTrigger


class PlanError(ValueError):
    pass


class InterlockError(RuntimeError):
    pass


class ExposureAborted(Exception):
    pass


# Planning


@dataclass(frozen=True)
class ExposureRequest:
    pattern: Pattern
    laser: str
    dwell_s: float
    """Time on each spot. For a single point, the whole exposure."""
    wait_for_trigger: bool = False
    trigger_timeout_s: float = 60.0
    camera_trigger: bool = False
    note: str = ""
    until_stopped: bool = False
    """Keep a single point on until stopped (aiming, calibration); ``dwell_s`` is the cap."""
    frame: Literal["image", "galvo"] = "image"
    """Pattern coordinates: image pixels, or logical galvo volts (used by calibration)."""


@dataclass(frozen=True, eq=False)
class ExposurePlan:
    request: ExposureRequest
    laser: LaserConfig
    config: RigConfig
    calibration: Calibration
    segments_px: list[Points]
    segments_ao: list[Points]
    samples_per_point: int
    steps: tuple[Step, ...]

    @property
    def dwell_s_actual(self) -> float:
        return self.samples_per_point / self.config.daq.sample_rate_hz

    @property
    def n_points(self) -> int:
        return sum(len(s) for s in self.segments_px)

    @property
    def expected_duration_s(self) -> float:
        rate = self.config.daq.sample_rate_hz
        total = 0.0
        for step in self.steps:
            if isinstance(step, Settle | Hold):
                total += step.seconds
            elif isinstance(step, Scan):
                total += len(step.samples) / rate
        return total


def to_channel_volts(pixels: Points, calibration: Calibration, config: RigConfig) -> Points:
    """Image pixels -> logical galvo volts -> channel volts (ao0, ao1)."""
    galvo = calibration.px_to_galvo.apply(pixels)
    ao: Points = galvo @ config.axes.as_array().T
    return ao


def plan_exposure(
    request: ExposureRequest, config: RigConfig, calibration: Calibration
) -> ExposurePlan:
    laser = config.laser(request.laser)
    rate = config.daq.sample_rate_hz
    samples_per_point = round(request.dwell_s * rate)
    if samples_per_point < 1:
        raise PlanError(f"dwell {request.dwell_s} s is shorter than one sample (1/{rate:g} s)")

    segments = rasterize(request.pattern)
    if not segments:
        raise PlanError("the pattern contains no spots (region smaller than the spacing?)")
    if request.until_stopped and (len(segments) > 1 or len(segments[0]) > 1):
        raise PlanError("only a single point can be held on until stopped")
    if request.frame == "galvo":
        # Logged pixel positions are where the current calibration predicts the spot lands.
        to_px = calibration.px_to_galvo.inverse()
        segments_px = [to_px.apply(s) for s in segments]
        segments_ao = [s @ config.axes.as_array().T for s in segments]
    else:
        segments_px = segments
        segments_ao = [to_channel_volts(s, calibration, config) for s in segments_px]
    segments_ao = _check_in_range(segments_px, segments_ao, config)

    settle = config.galvo.settling_time_s
    steps: list[Step] = [SetLines(0, "gates_closed")]
    steps += [_move(segments_ao[0][0]), Settle(settle)]

    if request.wait_for_trigger:
        if config.sync.trigger_in_line is None:
            raise PlanError("wait_for_trigger needs [sync] trigger_in_line in rig.toml")
        steps.append(WaitTrigger(request.trigger_timeout_s))
    if request.camera_trigger:
        if config.sync.trigger_out_line is None:
            raise PlanError("camera_trigger needs [sync] trigger_out_line in rig.toml")
        steps += [
            SetLines(1 << config.sync.trigger_out_line, "trigger_out"),
            Hold(config.sync.trigger_out_pulse_s),
            SetLines(0, "trigger_out_end"),
        ]

    gate = 1 << laser.do_line
    for i, seg in enumerate(segments_ao):
        if i:
            steps += [_move(seg[0]), Settle(settle)]
        steps.append(SetLines(gate, "gate_open"))
        if len(seg) == 1:
            steps.append(Hold(samples_per_point / rate))
        else:
            steps.append(Scan(np.repeat(seg, samples_per_point, axis=0)))
        steps.append(SetLines(0, "gate_close"))

    check_steps(steps)
    return ExposurePlan(
        request,
        laser,
        config,
        calibration,
        segments_px,
        segments_ao,
        samples_per_point,
        tuple(steps),
    )


def check_steps(steps: list[Step] | tuple[Step, ...]) -> None:
    """Invariants every plan must satisfy, whoever built it."""
    mask = None  # unknown until the first SetLines
    for step in steps:
        if isinstance(step, SetLines):
            mask = step.mask
        elif isinstance(step, MoveTo) and mask != 0:
            raise PlanError("plan moves the mirrors with a gate open (or before closing gates)")
    if mask != 0:
        raise PlanError("plan must end with all gates closed")


def _move(ao: Points) -> MoveTo:
    return MoveTo((float(ao[0]), float(ao[1])))


# Floating-point rounding can put a target exactly on the edge of the range a hair
# outside it (e.g. -10.0000000001 V). Far below one DAC step (~1.2 mV at 14 bits).
EDGE_TOLERANCE_V = 1e-6


def _check_in_range(px: list[Points], ao: list[Points], config: RigConfig) -> list[Points]:
    """Reject spots outside the galvo range; snap rounding error at the edge back inside."""
    all_px, all_ao = np.vstack(px), np.vstack(ao)
    lo, hi = config.daq.ao_min_v, config.daq.ao_max_v
    tol = EDGE_TOLERANCE_V
    bad = ((all_ao < lo - tol) | (all_ao > hi + tol)).any(axis=1)
    if bad.any():
        first = all_px[bad][0]
        raise PlanError(
            f"{int(bad.sum())} of {len(bad)} spots are outside the galvo range "
            f"[{lo}, {hi}] V, first at pixel ({first[0]:.1f}, {first[1]:.1f})"
        )
    return [np.clip(s, lo, hi) for s in ao]


# Execution


@dataclass(frozen=True)
class Event:
    t_s: float
    """Seconds since the exposure started (monotonic clock)."""
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(eq=False)
class ExposureResult:
    plan: ExposurePlan
    backend_name: str
    simulated: bool
    started_utc: str
    events: list[Event]
    status: str  # "completed" | "stopped" (held on, then stopped) | "aborted" | "failed"
    error: str | None
    exposure_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    mode: str = "exposure"  # "exposure", or "aim" (laser followed the crosshair)

    @property
    def gate_intervals(self) -> list[tuple[float, float]]:
        intervals, opened = [], None
        for ev in self.events:
            if ev.kind == "gate_open":
                opened = ev.t_s
            elif ev.kind in ("gate_close", "gates_closed_on_exit") and opened is not None:
                intervals.append((opened, ev.t_s))
                opened = None
        return intervals

    @property
    def duration_s(self) -> float:
        return self.events[-1].t_s if self.events else 0.0

    def to_record(self) -> dict[str, Any]:
        p, cfg, cal = self.plan, self.plan.config, self.plan.calibration
        return {
            "schema_version": SCHEMA_VERSION,
            "exposure_id": self.exposure_id,
            "mode": self.mode,
            "note": p.request.note,
            "status": self.status,
            "error": self.error,
            "backend": {"name": self.backend_name, "simulated": self.simulated},
            "started_utc": self.started_utc,
            "duration_s": _r(self.duration_s),
            "laser": {
                "name": p.laser.name,
                "role": p.laser.role,
                "wavelength_nm": p.laser.wavelength_nm,
                "do_line": p.laser.do_line,
            },
            "pattern": pattern_to_dict(p.request.pattern),
            "pattern_frame": p.request.frame,
            "until_stopped": p.request.until_stopped,
            "dwell_s_requested": p.request.dwell_s,
            "dwell_s_per_point": p.dwell_s_actual,
            "samples_per_point": p.samples_per_point,
            "sample_rate_hz": cfg.daq.sample_rate_hz,
            "settling_time_s": cfg.galvo.settling_time_s,
            "n_points": p.n_points,
            "targets_px": [s.round(3).tolist() for s in p.segments_px],
            "targets_ao_v": [s.round(5).tolist() for s in p.segments_ao],
            "calibration": {
                "id": cal.id,
                "is_default": cal.is_default,
                "rms_px": _r(cal.rms_px),
                "created_utc": cal.created_utc,
            },
            "rig_config_sha256": cfg.source_sha256,
            "unverified_rig_facts": cfg.unverified(),
            "gate_open_intervals_s": [[_r(a), _r(b)] for a, b in self.gate_intervals],
            "events": [{"t_s": _r(e.t_s), "event": e.kind, **e.detail} for e in self.events],
            "timing": (
                "t_s is seconds after started_utc. Gate and trigger times are software-timed "
                "(about 1 ms); scans are hardware-timed at sample_rate_hz."
            ),
        }


def execute(
    plan: ExposurePlan,
    backend: DaqBackend,
    log: ExposureLog | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> ExposureResult:
    """Run ``plan`` on ``backend``. Always closes the gates and writes a log record."""
    if backend.lines != 0:
        raise InterlockError(f"a gate is already open (lines {backend.lines:#x}); not starting")

    clock = backend.clock
    t0 = clock.monotonic()
    started = clock.wall().isoformat()
    events: list[Event] = []
    status, error = "completed", None
    failure: BaseException | None = None

    def record(t: float, kind: str, **detail: Any) -> None:
        events.append(Event(t - t0, kind, detail))

    def wait(seconds: float) -> None:
        end = clock.monotonic() + seconds
        while (remaining := end - clock.monotonic()) > 0:
            if should_abort and should_abort():
                raise ExposureAborted
            clock.sleep(min(remaining, 0.01))

    try:
        for step in plan.steps:
            if should_abort and should_abort():
                raise ExposureAborted
            match step:
                case MoveTo(ao):
                    record(backend.set_position(ao), "move", ao_v=[_r(ao[0]), _r(ao[1])])
                case Settle(s) | Hold(s):
                    wait(s)
                case SetLines(mask, label):
                    record(backend.set_lines(mask), label, mask=mask)
                case Scan(samples):
                    start, end = backend.write_waveform(samples)
                    record(start, "scan_start", n_samples=len(samples))
                    record(end, "scan_end")
                case WaitTrigger(timeout):
                    record(backend.wait_for_trigger(timeout), "trigger_in")
    except ExposureAborted:
        status = "stopped" if plan.request.until_stopped else "aborted"
    except BaseException as exc:
        failure = exc
        status = "aborted" if isinstance(exc, KeyboardInterrupt) else "failed"
        error = repr(exc)
    finally:
        if status != "completed" or backend.lines != 0:
            try:
                record(backend.set_lines(0), "gates_closed_on_exit", mask=0)
            except BaseException as exc:
                error = f"{error} | FAILED TO CLOSE GATES: {exc!r}"
                failure = failure or exc
        result = ExposureResult(
            plan, backend.name, backend.simulated, started, events, status, error
        )
        if log is not None:
            log.append(result.to_record())

    if failure is not None:
        raise failure
    return result


# An aim target: channel volts plus the image pixel it came from (for the log).
AimTarget = tuple[tuple[float, float], tuple[float, float]]


def run_aim(
    plan: ExposurePlan,
    backend: DaqBackend,
    targets: queue.Queue[AimTarget | None],
    log: ExposureLog | None = None,
) -> ExposureResult:
    """Hold an aiming laser on, moving to each new target as it arrives, until ``None``.

    Unlike every other exposure, the mirrors move with the gate open: following the
    crosshair is the point of aiming. ``plan`` must be a held point for a laser whose
    role is "aim"; ``plan.request.dwell_s`` caps how long it can stay on.
    """
    if plan.laser.role != "aim":
        raise PlanError(f"{plan.laser.name} is not the aiming laser; it cannot follow targets")
    if not plan.request.until_stopped:
        raise PlanError("aiming needs a held (until_stopped) point plan")
    if backend.lines != 0:
        raise InterlockError(f"a gate is already open (lines {backend.lines:#x}); not starting")

    clock = backend.clock
    t0 = clock.monotonic()
    started = clock.wall().isoformat()
    events: list[Event] = []
    status, error = "stopped", None
    failure: BaseException | None = None

    def record(t: float, kind: str, **detail: Any) -> None:
        events.append(Event(t - t0, kind, detail))

    first_ao = plan.segments_ao[0][0]
    first_px = plan.segments_px[0][0]
    try:
        record(backend.set_lines(0), "gates_closed", mask=0)
        ao = (float(first_ao[0]), float(first_ao[1]))
        px = [_r(first_px[0]), _r(first_px[1])]
        record(backend.set_position(ao), "move", ao_v=[_r(ao[0]), _r(ao[1])], px=px)
        clock.sleep(plan.config.galvo.settling_time_s)
        record(
            backend.set_lines(1 << plan.laser.do_line), "gate_open", mask=1 << plan.laser.do_line
        )
        deadline = clock.monotonic() + plan.request.dwell_s
        while (remaining := deadline - clock.monotonic()) > 0:
            try:
                item = targets.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if item is None:
                break
            (a0, a1), (x, y) = item
            record(backend.set_position((a0, a1)), "move", ao_v=[_r(a0), _r(a1)], px=[_r(x), _r(y)])
        else:
            status = "completed"  # reached the safety cap
        record(backend.set_lines(0), "gate_close", mask=0)
    except BaseException as exc:
        failure = exc
        status = "aborted" if isinstance(exc, KeyboardInterrupt) else "failed"
        error = repr(exc)
    finally:
        if backend.lines != 0 or failure is not None:
            try:
                record(backend.set_lines(0), "gates_closed_on_exit", mask=0)
            except BaseException as exc:
                error = f"{error} | FAILED TO CLOSE GATES: {exc!r}"
                failure = failure or exc
        result = ExposureResult(
            plan, backend.name, backend.simulated, started, events, status, error, mode="aim"
        )
        if log is not None:
            log.append(result.to_record())

    if failure is not None:
        raise failure
    return result


def _r(x: float) -> float:
    return round(float(x), 6)
