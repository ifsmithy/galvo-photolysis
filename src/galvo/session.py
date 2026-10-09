"""One working session at the rig, without any GUI: the object the window drives.

Holds the rig config, the open backend, the current calibration and the exposure log,
and runs exposures on a background thread so the window stays responsive. Only one
exposure runs at a time.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from galvo.calibration import Calibration
from galvo.config import RigConfig
from galvo.explog import ExposureLog
from galvo.geometry import Points
from galvo.hw.base import DaqBackend
from galvo.patterns import PointTarget
from galvo.sequencer import (
    AimTarget,
    ExposurePlan,
    ExposureRequest,
    ExposureResult,
    InterlockError,
    execute,
    plan_exposure,
    run_aim,
)

DoneCallback = Callable[[ExposureResult | BaseException], None]

#: Where the calibration targets sit, as fractions of the view (4 corners + centre).
CALIBRATION_FRACTIONS = ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8), (0.5, 0.5))


@dataclass
class Session:
    config: RigConfig
    backend: DaqBackend
    calibration: Calibration
    log: ExposureLog
    calibration_path: Path | None = None
    _running: bool = field(default=False, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _aim_targets: queue.Queue[AimTarget | None] | None = field(default=None, init=False)

    @property
    def busy(self) -> bool:
        return self._running

    @property
    def aiming(self) -> bool:
        return self._running and self._aim_targets is not None

    def set_calibration(self, calibration: Calibration, save: bool = True) -> None:
        self.calibration = calibration
        if save and self.calibration_path is not None and not calibration.is_default:
            self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
            calibration.save(self.calibration_path)

    def plan(self, request: ExposureRequest) -> ExposurePlan:
        return plan_exposure(request, self.config, self.calibration)

    def start(self, plan: ExposurePlan, on_done: DoneCallback) -> None:
        """Run ``plan`` in the background. ``on_done`` is called from that thread."""
        self._run(
            lambda: execute(plan, self.backend, self.log, should_abort=self._stop.is_set),
            on_done,
        )

    def start_aim(self, plan: ExposurePlan, on_done: DoneCallback) -> None:
        """Hold the aiming laser on at ``plan``'s point; follow :meth:`move_aim` until stopped."""
        if self._running:
            raise InterlockError("an exposure is already running")
        targets: queue.Queue[AimTarget | None] = queue.Queue()
        self._aim_targets = targets
        self._run(lambda: run_aim(plan, self.backend, targets, self.log), on_done)

    def move_aim(self, laser: str, px: tuple[float, float]) -> None:
        """Send the held aiming laser to image pixel ``px`` (validated like any target)."""
        if self._aim_targets is None:
            return
        plan = self.plan(ExposureRequest(PointTarget(px), laser, dwell_s=0.01))  # validates
        a0, a1 = plan.segments_ao[0][0]
        self._aim_targets.put(((float(a0), float(a1)), px))

    def _run(self, job: Callable[[], ExposureResult], on_done: DoneCallback) -> None:
        if self._running:
            raise InterlockError("an exposure is already running")
        self._running = True
        self._stop.clear()

        def work() -> None:
            outcome: ExposureResult | BaseException
            try:
                outcome = job()
            except BaseException as exc:
                outcome = exc
            # Clear busy before reporting, so the callback may start the next exposure.
            self._aim_targets = None
            self._running = False
            on_done(outcome)

        self._thread = threading.Thread(target=work, name="exposure", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the running exposure to stop; gates close within about 10 ms."""
        self._stop.set()
        if self._aim_targets is not None:
            self._aim_targets.put(None)

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def calibration_targets(self, width: float, height: float) -> Points:
        """Logical galvo volts to aim at during calibration, spread across the view.

        Placed using the current calibration, so repeating calibration converges even if
        the first attempt's spots were off target.
        """
        px = np.array(CALIBRATION_FRACTIONS) * [width, height]
        return self.calibration.px_to_galvo.apply(px)

    def close(self) -> None:
        self.stop()
        self.wait(2.0)
        self.backend.close()
