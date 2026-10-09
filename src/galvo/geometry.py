"""2-D affine transforms between coordinate spaces.

The coordinate chain is::

    image pixels --(calibration)--> logical galvo volts --(axis matrix)--> channel volts

Points are always numpy arrays of shape (N, 2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

Points = npt.NDArray[np.float64]


def as_points(values: npt.ArrayLike) -> Points:
    """Coerce to a float (N, 2) array, accepting a single (x, y) pair."""
    pts = np.asarray(values, dtype=np.float64)
    if pts.shape == (2,):
        pts = pts.reshape(1, 2)
    if pts.ndim != 2 or pts.shape[1] != 2:
        raise ValueError(f"expected points of shape (N, 2), got {pts.shape}")
    return pts


@dataclass(frozen=True, eq=False)
class Affine2D:
    """``y = A @ x + b``, stored as a 2x3 matrix ``[A | b]``."""

    matrix: npt.NDArray[np.float64] = field(default_factory=lambda: np.eye(2, 3))

    def __post_init__(self) -> None:
        m = np.asarray(self.matrix, dtype=np.float64)
        if m.shape != (2, 3):
            raise ValueError(f"affine matrix must be 2x3, got {m.shape}")
        m.setflags(write=False)
        object.__setattr__(self, "matrix", m)

    @classmethod
    def from_linear(cls, linear: npt.ArrayLike, offset: npt.ArrayLike = (0.0, 0.0)) -> Affine2D:
        a = np.asarray(linear, dtype=np.float64).reshape(2, 2)
        b = np.asarray(offset, dtype=np.float64).reshape(2, 1)
        return cls(np.hstack([a, b]))

    @property
    def linear(self) -> npt.NDArray[np.float64]:
        return self.matrix[:, :2]

    @property
    def offset(self) -> npt.NDArray[np.float64]:
        return self.matrix[:, 2]

    def apply(self, points: npt.ArrayLike) -> Points:
        pts = as_points(points)
        result: Points = pts @ self.linear.T + self.offset
        return result

    def inverse(self) -> Affine2D:
        inv = np.linalg.inv(self.linear)
        return Affine2D.from_linear(inv, -inv @ self.offset)

    def then(self, other: Affine2D) -> Affine2D:
        """The transform that applies ``self`` first, then ``other``."""
        return Affine2D.from_linear(other.linear @ self.linear, other.apply(self.offset)[0])

    def to_list(self) -> list[list[float]]:
        return [[float(v) for v in row] for row in self.matrix]

    @classmethod
    def from_list(cls, rows: Sequence[Sequence[float]]) -> Affine2D:
        return cls(np.array(rows, dtype=np.float64))

    def __repr__(self) -> str:
        return f"Affine2D({self.to_list()})"
