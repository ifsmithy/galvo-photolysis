"""The main window: targeting canvas on the left, controls on the right.

As in the original, the whole window can be made see-through and laid over the imaging
software's live view. Once calibrated, the canvas is locked to where it was during
calibration, and firing is refused if the window has moved since.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QKeySequence,
    QMoveEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from galvo.calibration import CalibrationError, calibrate, default_calibration
from galvo.config import LaserConfig
from galvo.geometry import Affine2D, Points
from galvo.gui.canvas import TargetCanvas, Tool
from galvo.hw import SimulatedBackend
from galvo.patterns import PointTarget, rasterize
from galvo.sequencer import ExposureRequest, ExposureResult, PlanError
from galvo.session import Session
from galvo.simfield import channel_volts_to_px

MAX_HOLD_S = 300.0  # safety cap for "On" and calibration spots
MAX_AIM_S = 1800.0  # safety cap for the aiming laser held on
BEAM_TRAIL_S = 0.4


def laser_colour(laser: LaserConfig, index: int) -> QColor:
    if laser.wavelength_nm and laser.wavelength_nm < 430:
        return QColor("#b36bff")  # violet / UV
    if laser.wavelength_nm and laser.wavelength_nm < 500:
        return QColor("#00c8ff")  # blue
    return QColor(["#ff9f0a", "#ff375f", "#30d158", "#64d2ff"][index % 4])


def _form(parent: QWidget | None = None) -> QFormLayout:
    form = QFormLayout(parent)
    # macOS otherwise keeps fields at their (too narrow) size hint.
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return form


class _Bridge(QObject):
    """Carries exposure results from the worker thread to the GUI thread."""

    done = Signal(object)


@dataclass
class _Calibrating:
    laser: str
    targets: Points  # logical galvo volts
    index: int = 0
    clicks: list[QPointF] = field(default_factory=list)
    galvo: list[tuple[float, float]] = field(default_factory=list)
    cancelled: bool = False


class MainWindow(QMainWindow):
    def __init__(self, session: Session, sim_truth: Affine2D | None = None) -> None:
        super().__init__()
        self.session = session
        self.config = session.config
        self.sim_truth = sim_truth
        self._bridge = _Bridge()
        self._bridge.done.connect(self._on_done)
        self._cal: _Calibrating | None = None
        self._running_laser: str | None = None
        self._last_segments: list[Points] = []

        self.setWindowTitle("Galvo photolysis")
        self.canvas = TargetCanvas()
        self.canvas.targetChanged.connect(self._on_target_changed)
        self.canvas.pointMoved.connect(self._on_point_moved)
        self.canvas.calibrationClick.connect(self._on_calibration_click)
        self.canvas.resized.connect(self._on_canvas_resized)

        panel = QWidget()
        panel.setFixedWidth(320)
        layout = QVBoxLayout(panel)
        layout.addWidget(self._build_banner())
        layout.addWidget(self._build_tools())
        layout.addWidget(self._build_exposure())
        layout.addWidget(self._build_lasers())
        layout.addWidget(self._build_calibration())
        layout.addWidget(self._build_window_box())
        layout.addStretch(1)

        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.canvas, 1)
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(panel.width() + 16)
        row.addWidget(scroll)
        self.setCentralWidget(central)
        self._build_menu()

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.stop_all)
        QShortcut(QKeySequence(Qt.Key.Key_S), self, self.skip_calibration_point)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_beam)
        if isinstance(session.backend, SimulatedBackend) and sim_truth is not None:
            self._timer.start(33)

        self.statusBar().showMessage(f"Exposure log: {session.log.path}")
        self._refresh_calibration_label()

    # Building the panel

    def _build_banner(self) -> QWidget:
        backend = self.session.backend
        text = "SIMULATED RIG - no laser will fire" if backend.simulated else "REAL RIG"
        unverified = self.config.unverified()
        if unverified:
            text += "\nUnverified: " + ", ".join(unverified)
        self.banner = QLabel(text)
        self.banner.setWordWrap(True)
        colour = "#ff9f0a" if backend.simulated else "#30d158"
        self.banner.setStyleSheet(
            f"background:{colour}; color:black; padding:6px; border-radius:4px; font-weight:600"
        )
        return self.banner

    def _build_tools(self) -> QWidget:
        box = QGroupBox("Target")
        grid = QVBoxLayout(box)
        row = QHBoxLayout()
        self.tool_group = QButtonGroup(self)
        for tool in Tool:
            button = QPushButton(tool.value)
            button.setCheckable(True)
            button.setChecked(tool is Tool.POINT)
            button.clicked.connect(lambda _=False, t=tool: self.canvas.set_tool(t))
            self.tool_group.addButton(button)
            row.addWidget(button)
        grid.addLayout(row)

        form = _form()
        self.line_points = QSpinBox(minimum=2, maximum=1000, value=10)
        self.line_points.valueChanged.connect(self._update_preview)
        form.addRow("Line spots", self.line_points)
        self.spacing = QDoubleSpinBox(minimum=1.0, maximum=500.0, value=10.0, suffix=" px")
        self.spacing.valueChanged.connect(self._update_preview)
        form.addRow("Region spacing", self.spacing)
        grid.addLayout(form)

        row = QHBoxLayout()
        clear = QPushButton("Clear target")
        clear.clicked.connect(self.canvas.clear_target)
        marks = QPushButton("Clear marks")
        marks.clicked.connect(self.canvas.clear_exposed)
        row.addWidget(clear)
        row.addWidget(marks)
        grid.addLayout(row)
        return box

    def _build_exposure(self) -> QWidget:
        box = QGroupBox("Exposure")
        form = _form(box)
        rate = self.config.daq.sample_rate_hz
        self.dwell_ms = QDoubleSpinBox(decimals=1, minimum=1000 / rate, maximum=600_000.0)
        self.dwell_ms.setValue(100.0)
        self.dwell_ms.setSuffix(" ms")
        self.dwell_ms.setToolTip("Time on each spot. For a point, the whole exposure.")
        form.addRow("Dwell per spot", self.dwell_ms)
        self.wait_trigger = QCheckBox("Wait for trigger in")
        self.wait_trigger.setEnabled(self.config.sync.trigger_in_line is not None)
        self.camera_trigger = QCheckBox("Send camera trigger")
        self.camera_trigger.setEnabled(self.config.sync.trigger_out_line is not None)
        form.addRow(self.wait_trigger)
        form.addRow(self.camera_trigger)
        self.note = QLineEdit(placeholderText="e.g. cell 3, NP-EGTA")
        form.addRow("Log note", self.note)
        return box

    def _build_lasers(self) -> QWidget:
        box = QGroupBox("Lasers")
        layout = QVBoxLayout(box)
        self.fire_buttons: dict[str, QPushButton] = {}
        self.on_buttons: dict[str, QPushButton] = {}
        self.aim_on_buttons: dict[str, QPushButton] = {}
        self.colours: dict[int, QColor] = {}
        for i, laser in enumerate(self.config.lasers):
            colour = laser_colour(laser, i)
            self.colours[laser.do_line] = colour
            label = laser.name
            if laser.wavelength_nm and f"{laser.wavelength_nm:g}" not in label:
                label += f" ({laser.wavelength_nm:g} nm)"
            row = QHBoxLayout()
            swatch = QLabel("●")
            swatch.setStyleSheet(f"color:{colour.name()}; font-size:18px")
            row.addWidget(swatch)
            if laser.role == "aim":
                row.addWidget(QLabel(f"{label}  (aim)"), 1)
                aim_on = QPushButton("On")
                aim_on.setToolTip("Turn the aiming laser on; it follows the crosshair until Off")
                aim_on.clicked.connect(lambda _=False, n=laser.name: self.aim_on(n))
                aim_off = QPushButton("Off")
                aim_off.clicked.connect(lambda _=False, n=laser.name: self.aim_off(n))
                row.addWidget(aim_on)
                row.addWidget(aim_off)
                self.aim_on_buttons[laser.name] = aim_on
                layout.addLayout(row)
                continue
            row.addWidget(QLabel(label), 1)
            fire = QPushButton("Fire")
            fire.setToolTip("Expose the current target for the dwell time")
            fire.clicked.connect(lambda _=False, n=laser.name: self.fire(n))
            on = QPushButton("On")
            on.setCheckable(True)
            on.setToolTip("Hold this laser on at the point target until clicked again (aiming)")
            on.toggled.connect(lambda checked, n=laser.name: self.toggle_on(n, checked))
            row.addWidget(fire)
            row.addWidget(on)
            self.fire_buttons[laser.name] = fire
            self.on_buttons[laser.name] = on
            layout.addLayout(row)

        self.stop_button = QPushButton("STOP  (Esc)")
        self.stop_button.setStyleSheet("background:#ff3b30; color:white; font-weight:700")
        self.stop_button.clicked.connect(self.stop_all)
        layout.addWidget(self.stop_button)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        return box

    def _build_calibration(self) -> QWidget:
        box = QGroupBox("Calibration")
        layout = QVBoxLayout(box)
        self.calibration_label = QLabel()
        self.calibration_label.setWordWrap(True)
        layout.addWidget(self.calibration_label)
        row = QHBoxLayout()
        self.calibrate_button = QPushButton("Calibrate...")
        self.calibrate_button.setToolTip(
            "Aim a laser at 5 positions and click where the spot appears"
        )
        self.calibrate_button.clicked.connect(lambda: self.start_calibration())
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset_calibration)
        row.addWidget(self.calibrate_button)
        row.addWidget(reset)
        layout.addLayout(row)
        self.restore_button = QPushButton("Restore window position")
        self.restore_button.clicked.connect(self.apply_lock)
        self.restore_button.setVisible(False)
        layout.addWidget(self.restore_button)
        return box

    def _build_window_box(self) -> QWidget:
        box = QGroupBox("Window")
        form = _form(box)
        # Kept from the original: a see-through window laid over the imaging display.
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(20, 100)
        self.opacity.setValue(85)
        self.opacity.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100))
        self.setWindowOpacity(0.85)
        form.addRow("Opacity", self.opacity)
        on_top = QCheckBox("Keep on top")
        on_top.toggled.connect(self._set_on_top)
        form.addRow(on_top)
        return box

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("File")
        open_logs = QAction("Open log folder", self)
        open_logs.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.log.path.parent)))
        )
        menu.addAction(open_logs)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    # Firing

    def fire(self, laser: str) -> None:
        pattern = self.canvas.pattern(self.line_points.value(), self.spacing.value())
        if pattern is None:
            self._say("Draw a target first.", error=True)
            return
        self._start(
            ExposureRequest(
                pattern,
                laser,
                dwell_s=self.dwell_ms.value() / 1000,
                wait_for_trigger=self.wait_trigger.isChecked(),
                camera_trigger=self.camera_trigger.isChecked(),
                note=self.note.text(),
            )
        )

    def toggle_on(self, laser: str, checked: bool) -> None:
        if not checked:
            if self._running_laser == laser:
                self.session.stop()
            return
        if self.canvas.tool is not Tool.POINT or self.canvas.point is None:
            self._say("'On' needs a point target.", error=True)
            self._uncheck_on_buttons()
            return
        p = self.canvas.point
        request = ExposureRequest(
            PointTarget((p.x(), p.y())),
            laser,
            dwell_s=MAX_HOLD_S,
            until_stopped=True,
            note=self.note.text() or "held on",
        )
        if not self._start(request):
            self._uncheck_on_buttons()

    def aim_on(self, laser: str) -> None:
        """Aiming laser on at the crosshair; it then follows the crosshair until Off."""
        if self.session.aiming:
            return
        if self.canvas.tool is not Tool.POINT or self.canvas.point is None:
            self._say("Aiming needs a point target: pick Point and click.", error=True)
            return
        if not self._ready_to_fire():
            return
        p = self.canvas.point
        try:
            plan = self.session.plan(
                ExposureRequest(
                    PointTarget((p.x(), p.y())),
                    laser,
                    dwell_s=MAX_AIM_S,
                    until_stopped=True,
                    note=self.note.text() or "aim",
                )
            )
        except (PlanError, KeyError) as exc:
            self._say(str(exc), error=True)
            return
        self._running_laser = laser
        self._last_segments = []
        self.session.start_aim(plan, self._bridge.done.emit)
        self.aim_on_buttons[laser].setStyleSheet("background:#0a84ff; color:white; font-weight:700")
        self._say(f"{laser} ON: follows the crosshair. Off or Esc to stop.")

    def aim_off(self, laser: str) -> None:
        if self.session.aiming and self._running_laser == laser:
            self.session.stop()

    def _ready_to_fire(self) -> bool:
        if self.session.busy:
            self._say("An exposure is already running.", error=True)
            return False
        if self._cal is None and not self.view_matches_calibration():
            self._say(
                "Window has moved since calibration; targets would be wrong. "
                "Use 'Restore window position' or recalibrate.",
                error=True,
            )
            return False
        return True

    def _start(self, request: ExposureRequest) -> bool:
        if not self._ready_to_fire():
            return False
        try:
            plan = self.session.plan(request)
        except (PlanError, KeyError) as exc:
            self._say(str(exc), error=True)
            return False
        self._running_laser = request.laser
        self._last_segments = plan.segments_px
        held = " (held on; Esc or click On to stop)" if request.until_stopped else ""
        self._say(f"Running: {request.laser}, {plan.n_points} spot(s){held}")
        self.session.start(plan, self._bridge.done.emit)
        return True

    def stop_all(self) -> None:
        if self._cal is not None:
            self._cal.cancelled = True
        self.session.stop()

    def _on_done(self, outcome: ExposureResult | BaseException) -> None:
        laser = self._running_laser
        self._running_laser = None
        self._uncheck_on_buttons()
        for button in self.aim_on_buttons.values():
            button.setStyleSheet("")
        if isinstance(outcome, BaseException):
            self._say(f"Exposure FAILED: {outcome!r}. Gates were closed.", error=True)
            self._alert(
                f"Exposure failed:\n{outcome!r}\n\nAll gates were closed and it was logged."
            )
            if self._cal is not None:
                self._cal.cancelled = True
        else:
            if outcome.gate_intervals and laser is not None and outcome.mode != "aim":
                line = self.config.laser(laser).do_line
                self.canvas.add_exposed(self._last_segments, self.colours[line])
            open_ms = sum(b - a for a, b in outcome.gate_intervals) * 1000
            self._say(
                f"{outcome.status}: {laser}, {outcome.plan.n_points} spot(s), "
                f"laser on {open_ms:.1f} ms"
            )
        if self._cal is not None:
            self._continue_calibration()

    # Calibration

    def start_calibration(self, laser: str | None = None) -> None:
        if self.session.busy:
            self._say("Stop the running exposure first.", error=True)
            return
        if laser is None:
            laser = self._choose_calibration_laser()
            if laser is None:
                return
        targets = self.session.calibration_targets(*self.canvas.view_size())
        self._cal = _Calibrating(laser, targets)
        self.canvas.calibrating = True
        self.canvas.set_calibration_marks([])
        self._fire_calibration_point()

    def _choose_calibration_laser(self) -> str | None:
        names = [laser.name for laser in self.config.lasers]
        aim = [laser.name for laser in self.config.lasers if laser.role == "aim"]
        default = names.index(aim[0]) if aim else 0
        name, ok = QInputDialog.getItem(
            self,
            "Calibrate",
            "Calibrate with which laser? (the visible guide laser)",
            names,
            default,
            False,
        )
        return name if ok else None

    def _fire_calibration_point(self) -> None:
        cal = self._cal
        assert cal is not None
        gx, gy = (float(v) for v in cal.targets[cal.index])
        request = ExposureRequest(
            PointTarget((gx, gy)),
            cal.laser,
            dwell_s=MAX_HOLD_S,
            until_stopped=True,
            frame="galvo",
            note=f"calibration point {cal.index + 1}",
        )
        if self._start(request):
            self._say(
                f"Calibration {cal.index + 1}/{len(cal.targets)}: click the centre of the "
                "laser spot.  S = skip this point, Esc = cancel."
            )
        else:
            self._end_calibration()

    def _on_calibration_click(self, p: QPointF) -> None:
        cal = self._cal
        if cal is None or not self.session.busy:
            return
        cal.clicks.append(p)
        gx, gy = cal.targets[cal.index]
        cal.galvo.append((float(gx), float(gy)))
        self.canvas.set_calibration_marks(cal.clicks)
        self.session.stop()  # _on_done moves on to the next point

    def skip_calibration_point(self) -> None:
        if self._cal is not None:
            self.session.stop()

    def _continue_calibration(self) -> None:
        cal = self._cal
        assert cal is not None
        if cal.cancelled:
            self._say("Calibration cancelled.")
            self._end_calibration()
            return
        cal.index += 1
        if cal.index < len(cal.targets):
            self._fire_calibration_point()
            return
        self._end_calibration()
        px = np.array([[p.x(), p.y()] for p in cal.clicks]).reshape(-1, 2)
        try:
            result = calibrate(px, cal.galvo, view_rect=self.current_view_rect())
        except CalibrationError as exc:
            self._say(f"Calibration failed: {exc}", error=True)
            return
        residuals = ", ".join(f"{r:.1f}" for r in result.residuals_px)
        if self._ask(
            f"Calibration fitted from {len(px)} points.\n\n"
            f"RMS error: {result.rms_px:.2f} px\nPer point: {residuals} px\n\n"
            "Use it? The window will be locked to its current position and size."
        ):
            self.session.set_calibration(result)
            self._refresh_calibration_label()
            self.apply_lock()
            self._say(f"Calibrated, RMS {result.rms_px:.2f} px.")

    def _end_calibration(self) -> None:
        self._cal = None
        self.canvas.calibrating = False
        self.canvas.set_calibration_marks([])

    def reset_calibration(self) -> None:
        if self.session.busy:
            return
        width, height = self.canvas.view_size()
        daq = self.config.daq
        self.session.set_calibration(
            default_calibration(width, height, daq.ao_min_v, daq.ao_max_v), save=False
        )
        if self.session.calibration_path is not None:
            self.session.calibration_path.unlink(missing_ok=True)
        self.apply_lock()
        self._refresh_calibration_label()
        self._update_preview()

    def _refresh_calibration_label(self) -> None:
        cal = self.session.calibration
        if cal.is_default:
            text = "Not calibrated: the view is mapped to the full mirror range."
        else:
            text = f"Calibrated {cal.created_utc[:16]} UTC\nID {cal.id}, RMS {cal.rms_px:.2f} px"
        self.calibration_label.setText(text)
        self.canvas.set_warning(
            "NOT CALIBRATED: shots will miss the target. Click Calibrate... first."
            if cal.is_default
            else None
        )

    # Window locking

    def current_view_rect(self) -> tuple[int, int, int, int]:
        viewport = self.canvas.viewport()
        top_left = viewport.mapToGlobal(QPoint(0, 0))
        return top_left.x(), top_left.y(), viewport.width(), viewport.height()

    def view_matches_calibration(self) -> bool:
        cal = self.session.calibration
        return cal.is_default or cal.view_rect is None or self.current_view_rect() == cal.view_rect

    def apply_lock(self) -> None:
        """Fix the canvas to the calibrated size and put it back where it was calibrated."""
        cal = self.session.calibration
        if cal.is_default or cal.view_rect is None:
            self.canvas.setMinimumSize(200, 200)
            self.canvas.setMaximumSize(16_777_215, 16_777_215)
        else:
            x, y, width, height = cal.view_rect
            self.canvas.setFixedSize(width, height)
            # Place the window's inner area (not its title bar, whose size varies by
            # platform) so the canvas lands exactly where it was calibrated.
            offset = self.canvas.viewport().mapTo(self, QPoint(0, 0))
            self.setGeometry(QRect(QPoint(x, y) - offset, self.size()))
        self._check_position()

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self._check_position()

    def _check_position(self) -> None:
        moved = not self.view_matches_calibration()
        self.restore_button.setVisible(moved)
        if moved:
            self._say("Window moved since calibration: firing is disabled.", error=True)

    def _on_canvas_resized(self) -> None:
        if self.session.calibration.is_default and not self.session.busy:
            width, height = self.canvas.view_size()
            daq = self.config.daq
            self.session.set_calibration(
                default_calibration(width, height, daq.ao_min_v, daq.ao_max_v), save=False
            )
        self._update_preview()

    # Live display

    def _on_point_moved(self) -> None:
        """While aiming, the laser follows the crosshair as it is clicked or dragged."""
        p = self.canvas.point
        if not self.session.aiming or self._running_laser is None or p is None:
            return
        try:
            self.session.move_aim(self._running_laser, (p.x(), p.y()))
        except PlanError as exc:
            self._say(f"Aim laser stays put: {exc}", error=True)

    def _on_target_changed(self) -> None:
        if self.session.aiming:
            if self.canvas.tool is Tool.POINT and self.canvas.point is not None:
                self._on_point_moved()
            else:
                self.session.stop()  # target cleared or tool changed
            self._update_preview()
            return
        if self.session.busy and self._cal is None:
            self.session.stop()  # moving the target stops the laser
            self._say("Target changed: exposure stopped.")
        self._update_preview()

    def _update_preview(self) -> None:
        pattern = self.canvas.pattern(self.line_points.value(), self.spacing.value())
        self.canvas.set_preview(rasterize(pattern) if pattern is not None else [])

    def _update_beam(self) -> None:
        backend = self.session.backend
        if not isinstance(backend, SimulatedBackend) or self.sim_truth is None:
            return
        now = backend.clock.monotonic()
        times = np.linspace(now - BEAM_TRAIL_S, now, 800)
        px = channel_volts_to_px(backend.mirror_position(times), self.sim_truth, self.config)
        lines = backend.lines_at(times)
        laser_mask = sum(1 << laser.do_line for laser in self.config.lasers)
        on = (lines & laser_mask) != 0
        colour = None
        for laser in self.config.lasers:
            if lines[-1] & (1 << laser.do_line):
                colour = self.colours[laser.do_line]
        trail_colour = colour
        if trail_colour is None and on.any():
            last = int(lines[on][-1])
            trail_colour = next((c for line, c in self.colours.items() if last & (1 << line)), None)
        self.canvas.set_trail(px[on], trail_colour)
        self.canvas.set_beam((float(px[-1, 0]), float(px[-1, 1])), colour)

    # Misc

    def _set_on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    def _uncheck_on_buttons(self) -> None:
        for button in self.on_buttons.values():
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)

    def _say(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color:#ff453a; font-weight:600" if error else "")

    # Dialogs, separated so tests can replace them.

    def _ask(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Galvo photolysis", text)
        return answer == QMessageBox.StandardButton.Yes

    def _alert(self, text: str) -> None:
        QMessageBox.critical(self, "Galvo photolysis", text)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        self.session.close()  # stops any exposure and closes all gates
        super().closeEvent(event)
