"""Drive the window the way a user would, on the simulated rig in real time."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from galvo.config import RigConfig
from galvo.explog import read_log
from galvo.gui.app import build_window
from galvo.gui.canvas import Tool
from galvo.gui.main_window import MainWindow
from galvo.hw import RealClock, SimulatedBackend
from galvo.simfield import simulated_truth


@pytest.fixture
def window(qtbot: QtBot, config: RigConfig, tmp_path: Path) -> MainWindow:
    w = build_window(config, SimulatedBackend(config, RealClock()), tmp_path)
    w._ask = lambda text: True  # type: ignore[method-assign]
    w._alert = lambda text: None  # type: ignore[method-assign]
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w


def click(window: MainWindow, x: float, y: float) -> None:
    QTest.mouseClick(
        window.canvas.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(round(x), round(y))
    )


def records(window: MainWindow) -> list[dict[str, Any]]:
    return read_log(window.session.log.path)


def wait_idle(qtbot: QtBot, window: MainWindow) -> None:
    qtbot.waitUntil(lambda: not window.session.busy and window._running_laser is None, timeout=5000)


def test_not_calibrated_warning_shown_until_calibrated(window: MainWindow) -> None:
    assert window.canvas.warning.isVisible()
    assert "NOT CALIBRATED" in window.canvas.warning.text()


def test_banner_says_simulated(window: MainWindow) -> None:
    assert "SIMULATED" in window.banner.text()
    assert "axis convention" in window.banner.text()
    assert window.session.log.path.name.startswith("SIMULATED-")


def test_fire_point_logs_and_marks(qtbot: QtBot, window: MainWindow) -> None:
    click(window, 300, 200)
    assert window.canvas.point == QPointF(300, 200)
    window.dwell_ms.setValue(20)
    window.note.setText("test cell")
    window.fire("Laser 2")
    assert window.session.busy
    wait_idle(qtbot, window)

    (rec,) = records(window)
    assert rec["status"] == "completed"
    assert rec["note"] == "test cell"
    assert rec["targets_px"] == [[[300.0, 200.0]]]
    assert window.canvas._exposed_paths  # marked on screen
    assert window.session.backend.lines == 0


def test_fire_without_target_is_refused(window: MainWindow) -> None:
    window.fire("Laser 1")
    assert not window.session.busy
    assert "target" in window.status.text()
    assert records(window) == []


def test_line_and_region_preview_and_fire(qtbot: QtBot, window: MainWindow) -> None:
    window.canvas.set_tool(Tool.LINE)
    vp = window.canvas.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(vp, QPoint(200, 150))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
    window.line_points.setValue(7)
    assert window.canvas._preview.path().elementCount() > 0
    window.dwell_ms.setValue(1)
    window.fire("Laser 1")
    wait_idle(qtbot, window)
    rec = records(window)[-1]
    assert rec["pattern"]["type"] == "line"
    assert rec["n_points"] == 7


def test_on_holds_until_unchecked(qtbot: QtBot, window: MainWindow) -> None:
    click(window, 400, 300)
    window.on_buttons["Laser 1"].setChecked(True)
    qtbot.waitUntil(lambda: window.session.backend.lines == 1, timeout=2000)
    qtbot.wait(100)
    assert window.session.busy  # still on
    window.on_buttons["Laser 1"].setChecked(False)
    wait_idle(qtbot, window)
    (rec,) = records(window)
    assert rec["status"] == "stopped"
    assert rec["until_stopped"] is True


def test_moving_target_stops_exposure(qtbot: QtBot, window: MainWindow) -> None:
    click(window, 400, 300)
    window.on_buttons["Laser 2"].setChecked(True)
    qtbot.waitUntil(lambda: window.session.busy, timeout=2000)
    click(window, 450, 300)
    wait_idle(qtbot, window)
    assert records(window)[-1]["status"] == "stopped"


def test_escape_stops(qtbot: QtBot, window: MainWindow) -> None:
    click(window, 400, 300)
    window.dwell_ms.setValue(5000)
    window.fire("Laser 1")
    qtbot.waitUntil(lambda: window.session.backend.lines == 1, timeout=2000)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    wait_idle(qtbot, window)
    assert records(window)[-1]["status"] == "aborted"


def test_calibration_on_simulated_spot(qtbot: QtBot, window: MainWindow, config: RigConfig) -> None:
    """Click where the simulated beam lands; the fit should recover the simulator's truth."""
    truth = window.sim_truth
    assert truth is not None
    window.start_calibration("Laser 1")
    for _ in range(5):
        qtbot.waitUntil(lambda: window.session.backend.lines == 1, timeout=2000)
        cal = window._cal
        assert cal is not None
        index = cal.index
        spot = truth.inverse().apply(cal.targets[index])[0]
        click(window, *spot)

        def advanced(i: int = index) -> bool:
            return window._cal is None or window._cal.index != i

        qtbot.waitUntil(advanced, timeout=2000)
    wait_idle(qtbot, window)

    result = window.session.calibration
    assert not result.is_default
    assert not window.canvas.warning.isVisible()
    assert result.rms_px < 1.0  # clicks are rounded to whole pixels
    # Anywhere in the view, a target now lands within ~1 px of where it was clicked.
    grid = np.array([[x, y] for x in range(0, 801, 100) for y in range(0, 601, 100)], float)
    landed = truth.inverse().apply(result.px_to_galvo.apply(grid))
    assert np.abs(landed - grid).max() < 1.5
    assert (Path(window.session.calibration_path or "")).exists()
    assert [r["note"] for r in records(window)] == [f"calibration point {i}" for i in range(1, 6)]

    # The window is now locked; moving it disables firing until restored.
    click(window, 300, 200)
    window.move(window.pos() + QPoint(40, 0))
    qtbot.waitUntil(lambda: not window.view_matches_calibration(), timeout=2000)
    window.fire("Laser 1")
    assert not window.session.busy
    assert "moved" in window.status.text()
    window.apply_lock()
    # The headless test display misplaces window frames by a pixel or two after a move;
    # on a real macOS display the restore is exact (checked by hand).
    x, y, *_ = window.current_view_rect()
    cx, cy, *_ = result.view_rect or (0, 0)
    assert abs(x - cx) <= 3 and abs(y - cy) <= 3


def test_calibration_can_be_cancelled(qtbot: QtBot, window: MainWindow) -> None:
    window.start_calibration("Laser 1")
    qtbot.waitUntil(lambda: window.session.backend.lines == 1, timeout=2000)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    wait_idle(qtbot, window)
    assert window._cal is None
    assert window.session.calibration.is_default


def test_closing_closes_gates(qtbot: QtBot, window: MainWindow) -> None:
    click(window, 400, 300)
    window.on_buttons["Laser 1"].setChecked(True)
    qtbot.waitUntil(lambda: window.session.backend.lines == 1, timeout=2000)
    window.close()
    assert window.session.backend.lines == 0


def test_simulated_truth_is_not_the_default_mapping(config: RigConfig) -> None:
    # Otherwise calibrating in simulation would prove nothing.
    truth = simulated_truth(800, 600, config)
    assert not np.allclose(truth.linear, np.diag([20 / 800, 20 / 600]), atol=1e-3)


@pytest.fixture
def aim_window(qtbot: QtBot, raw_config: dict[str, Any], tmp_path: Path) -> MainWindow:
    from galvo.config import parse_config

    raw_config["lasers"][0]["role"] = "aim"
    config = parse_config(raw_config)
    w = build_window(config, SimulatedBackend(config, RealClock()), tmp_path)
    w._ask = lambda text: True  # type: ignore[method-assign]
    w._alert = lambda text: None  # type: ignore[method-assign]
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w


def test_aim_laser_has_on_off_and_follows_crosshair(qtbot: QtBot, aim_window: MainWindow) -> None:
    w = aim_window
    assert "Laser 1" in w.aim_on_buttons and "Laser 1" not in w.fire_buttons
    click(w, 300, 200)
    w.aim_on("Laser 1")
    qtbot.waitUntil(lambda: w.session.backend.lines == 1, timeout=2000)
    click(w, 400, 250)  # moving the crosshair keeps the aim laser on
    click(w, 420, 260)
    qtbot.wait(150)
    assert w.session.aiming and w.session.backend.lines == 1
    w.aim_off("Laser 1")
    wait_idle(qtbot, w)
    (rec,) = records(w)
    assert rec["mode"] == "aim" and rec["status"] == "stopped"
    moves = [e["px"] for e in rec["events"] if e["event"] == "move"]
    assert moves[0] == [300.0, 200.0] and moves[-1] == [420.0, 260.0]
