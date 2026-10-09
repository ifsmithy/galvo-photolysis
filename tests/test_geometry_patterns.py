from __future__ import annotations

import numpy as np
import pytest

from galvo.geometry import Affine2D, as_points
from galvo.patterns import LineTarget, PointTarget, RegionTarget, pattern_to_dict, rasterize


def test_affine_apply_inverse_compose() -> None:
    a = Affine2D.from_linear([[0.0, -2.0], [2.0, 0.0]], (1.0, 3.0))
    b = Affine2D.from_linear([[1.0, 0.0], [0.0, -1.0]], (5.0, 0.0))
    pts = as_points([[0, 0], [1, 2], [-3, 4]])

    assert np.allclose(a.inverse().apply(a.apply(pts)), pts)
    assert np.allclose(a.then(b).apply(pts), b.apply(a.apply(pts)))
    assert np.allclose(Affine2D.from_list(a.to_list()).matrix, a.matrix)


def test_as_points_validates_shape() -> None:
    assert as_points((1, 2)).shape == (1, 2)
    with pytest.raises(ValueError):
        as_points([1, 2, 3])


def test_point_and_line() -> None:
    assert np.allclose(rasterize(PointTarget((3, 4))), [[[3, 4]]])
    (line,) = rasterize(LineTarget((0, 0), (10, 20), 11))
    assert line.shape == (11, 2)
    assert np.allclose(line[0], [0, 0]) and np.allclose(line[-1], [10, 20])
    assert np.allclose(np.diff(line, axis=0), [1, 2])
    with pytest.raises(ValueError):
        LineTarget((0, 0), (1, 1), 1)


def test_square_region_is_one_serpentine_segment() -> None:
    (seg,) = rasterize(RegionTarget(((0, 0), (100, 0), (100, 50), (0, 50)), spacing=10))
    assert seg.shape == (10 * 5, 2)
    rows = seg.reshape(5, 10, 2)
    assert np.all(np.diff(rows[0, :, 0]) > 0)  # first row left to right
    assert np.all(np.diff(rows[1, :, 0]) < 0)  # second row right to left
    assert np.allclose(np.unique(seg[:, 0]), np.arange(5, 100, 10))  # columns line up


def test_concave_region_splits_at_the_gap() -> None:
    # A U shape: the gate must close while crossing the empty middle.
    u = ((0, 0), (90, 0), (90, 90), (60, 90), (60, 30), (30, 30), (30, 90), (0, 90))
    segments = rasterize(RegionTarget(u, spacing=10))
    assert len(segments) > 1
    spots = np.vstack(segments)
    in_gap = (spots[:, 0] > 30) & (spots[:, 0] < 60) & (spots[:, 1] > 30)
    assert not in_gap.any()
    for seg in segments:
        assert np.all(np.linalg.norm(np.diff(seg, axis=0), axis=1) <= 15)


def test_tiny_region_has_no_spots() -> None:
    assert rasterize(RegionTarget(((0, 0), (2, 0), (2, 2)), spacing=10)) == []


def test_region_validation() -> None:
    with pytest.raises(ValueError):
        RegionTarget(((0, 0), (1, 1)), spacing=1)
    with pytest.raises(ValueError):
        RegionTarget(((0, 0), (1, 0), (1, 1)), spacing=0)


def test_pattern_to_dict() -> None:
    assert pattern_to_dict(LineTarget((0, 1), (2, 3), 4)) == {
        "type": "line",
        "start": [0, 1],
        "end": [2, 3],
        "n_points": 4,
    }
