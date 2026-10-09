from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from galvo.calibration import (
    Calibration,
    CalibrationError,
    calibrate,
    default_calibration,
    fit_affine,
)
from galvo.geometry import Affine2D

# A rig where the camera is rotated 10 degrees and mirrored relative to the galvos.
_c, _s = math.cos(math.radians(10)), math.sin(math.radians(10))
TRUE = Affine2D.from_linear(0.04 * np.array([[_c, -_s], [_s, _c]]) @ [[1, 0], [0, -1]], (-9, 8))
GALVO = np.array([[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0]])


def test_exact_fit_recovers_rotation_and_mirror() -> None:
    clicks = TRUE.inverse().apply(GALVO)
    cal = calibrate(clicks, GALVO)
    assert np.allclose(cal.px_to_galvo.matrix, TRUE.matrix)
    assert cal.rms_px < 1e-9


def test_noisy_clicks_give_sensible_rms() -> None:
    rng = np.random.default_rng(1)
    clicks = TRUE.inverse().apply(GALVO) + rng.normal(0, 1.0, GALVO.shape)
    cal = calibrate(clicks, GALVO)
    assert 0.1 < cal.rms_px < 2.0
    assert cal.residuals_px.shape == (4,)


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [1, 1]],  # too few
        [[0, 0], [1, 1], [2, 2], [3, 3]],  # collinear
    ],
)
def test_bad_point_sets_rejected(points: list[list[float]]) -> None:
    with pytest.raises(CalibrationError):
        fit_affine(points, points)


def test_save_load_roundtrip(tmp_path: Path) -> None:
    cal = calibrate(TRUE.inverse().apply(GALVO), GALVO, view_rect=(10, 20, 640, 480))
    cal.save(tmp_path / "cal.json")
    loaded = Calibration.load(tmp_path / "cal.json")
    assert np.allclose(loaded.px_to_galvo.matrix, cal.px_to_galvo.matrix)
    assert loaded.view_rect == (10, 20, 640, 480)
    assert loaded.id == cal.id


def test_default_maps_view_corners_to_full_range() -> None:
    cal = default_calibration(800, 600, -10, 10)
    assert cal.is_default
    assert np.allclose(
        cal.px_to_galvo.apply([[0, 0], [800, 600], [400, 300]]), [[-10, -10], [10, 10], [0, 0]]
    )
