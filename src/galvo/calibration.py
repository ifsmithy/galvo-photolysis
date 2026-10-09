"""Calibration from image pixels to logical galvo volts.

Procedure: send the 488 nm guide laser to a few known galvo positions, click where its
spot lights up the Cal-520 in the image, and fit an affine map from the clicks. Because
an affine can rotate, scale, shear and mirror, any error in the rig's axis convention
is absorbed here.

The original software had a two-corner, axis-aligned version of this (scale and offset
only), applied to an overlay window placed over the imaging display.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from galvo.geometry import Affine2D, Points, as_points

# Overlay window geometry (x, y, width, height) in screen pixels at calibration time.
ViewRect = tuple[int, int, int, int]


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True, eq=False)
class Calibration:
    px_to_galvo: Affine2D
    pixel_points: Points
    galvo_points: Points
    created_utc: str
    view_rect: ViewRect | None = None
    is_default: bool = False

    @property
    def residuals_px(self) -> npt.NDArray[np.float64]:
        """Distance in pixels between each click and where the fit puts that galvo position."""
        predicted = self.px_to_galvo.inverse().apply(self.galvo_points)
        dist: npt.NDArray[np.float64] = np.linalg.norm(predicted - self.pixel_points, axis=1)
        return dist

    @property
    def rms_px(self) -> float:
        return float(np.sqrt(np.mean(self.residuals_px**2)))

    @property
    def id(self) -> str:
        """Short content hash, recorded in every exposure log entry."""
        blob = json.dumps(self.to_dict(include_id=False), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def to_dict(self, include_id: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "px_to_galvo": self.px_to_galvo.to_list(),
            "pixel_points": self.pixel_points.tolist(),
            "galvo_points": self.galvo_points.tolist(),
            "created_utc": self.created_utc,
            "view_rect": list(self.view_rect) if self.view_rect else None,
            "is_default": self.is_default,
        }
        if include_id:
            d["id"] = self.id
            d["rms_px"] = self.rms_px
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Calibration:
        rect = d.get("view_rect")
        return cls(
            px_to_galvo=Affine2D.from_list(d["px_to_galvo"]),
            pixel_points=as_points(d["pixel_points"]),
            galvo_points=as_points(d["galvo_points"]),
            created_utc=d["created_utc"],
            view_rect=(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])) if rect else None,
            is_default=bool(d.get("is_default", False)),
        )

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> Calibration:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def fit_affine(src: npt.ArrayLike, dst: npt.ArrayLike) -> Affine2D:
    """Least-squares affine taking ``src`` points to ``dst`` points (at least 3, not collinear)."""
    s, d = as_points(src), as_points(dst)
    if len(s) != len(d):
        raise CalibrationError("need the same number of pixel and galvo points")
    if len(s) < 3:
        raise CalibrationError("need at least 3 points to calibrate")
    design = np.hstack([s, np.ones((len(s), 1))])
    if np.linalg.matrix_rank(design) < 3:
        raise CalibrationError("calibration points are collinear; spread them across the field")
    solution, *_ = np.linalg.lstsq(design, d, rcond=None)
    return Affine2D(solution.T)


def calibrate(
    pixel_points: npt.ArrayLike,
    galvo_points: npt.ArrayLike,
    view_rect: ViewRect | None = None,
    now: datetime | None = None,
) -> Calibration:
    return Calibration(
        px_to_galvo=fit_affine(pixel_points, galvo_points),
        pixel_points=as_points(pixel_points),
        galvo_points=as_points(galvo_points),
        created_utc=(now or datetime.now(UTC)).isoformat(),
        view_rect=view_rect,
    )


def default_calibration(width: float, height: float, v_min: float, v_max: float) -> Calibration:
    """Uncalibrated starting point: the view spans the full galvo range on both axes.

    This is the original software's behaviour before its corner calibration.
    """
    px = as_points([[0.0, 0.0], [width, 0.0], [0.0, height], [width, height]])
    volts = as_points([[v_min, v_min], [v_max, v_min], [v_min, v_max], [v_max, v_max]])
    cal = calibrate(px, volts, now=datetime(1970, 1, 1, tzinfo=UTC))
    return Calibration(
        cal.px_to_galvo, cal.pixel_points, cal.galvo_points, cal.created_utc, is_default=True
    )
