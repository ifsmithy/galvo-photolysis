"""Start the application.

uv run galvo                       # simulated rig (default)
uv run galvo --backend nidaqmx     # real rig (Windows, Phase 3)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from galvo.calibration import Calibration, default_calibration
from galvo.config import RigConfig, load_config
from galvo.explog import ExposureLog
from galvo.gui.main_window import MainWindow
from galvo.hw import BackendKind, DaqBackend, HardwareError, RealClock, open_backend
from galvo.session import Session
from galvo.simfield import simulated_truth

CANVAS_SIZE = (800, 600)


def data_dir_for(backend: DaqBackend, base: Path | None = None) -> Path:
    """Simulated sessions keep their own calibration and logs, apart from the real rig's."""
    if base is None:
        location = QStandardPaths.StandardLocation.AppDataLocation
        base = Path(QStandardPaths.writableLocation(location))
    return base / "simulated" if backend.simulated else base


def build_window(config: RigConfig, backend: DaqBackend, data_dir: Path) -> MainWindow:
    calibration_path = data_dir / "calibration.json"
    width, height = CANVAS_SIZE
    calibration = (
        Calibration.load(calibration_path)
        if calibration_path.exists()
        else default_calibration(width, height, config.daq.ao_min_v, config.daq.ao_max_v)
    )
    prefix = "SIMULATED-" if backend.simulated else ""
    log = ExposureLog(data_dir / "logs" / f"{prefix}exposures-{date.today().isoformat()}.jsonl")
    session = Session(config, backend, calibration, log, calibration_path)

    truth = simulated_truth(width, height, config) if backend.simulated else None
    window = MainWindow(session, sim_truth=truth)
    window.canvas.resize(width, height)
    window.resize(width + 300, height + 40)
    return window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="galvo", description="Galvo photolysis controller")
    parser.add_argument("--backend", choices=["simulated", "nidaqmx"], default="simulated")
    parser.add_argument("--config", type=Path, help="rig.toml (default: config/rig.toml)")
    parser.add_argument("--data-dir", type=Path, help="where calibration and logs are kept")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("GalvoPhotolysis")
    config = load_config(args.config)
    kind: BackendKind = args.backend
    try:
        backend = open_backend(kind, config, RealClock())
    except HardwareError as exc:
        QMessageBox.critical(None, "Galvo photolysis", f"Could not open the {kind} backend:\n{exc}")
        return 1

    window = build_window(config, backend, data_dir_for(backend, args.data_dir))
    window.show()
    window.apply_lock()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
