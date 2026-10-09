"""Targets drawn on the image (points, lines, regions) and how the beam visits them.

Patterns are defined in image-pixel space, so spot spacing is uniform in the specimen.
:func:`rasterize` turns a pattern into *segments*: ordered beam positions the beam
moves through with the gate open. Between segments the gate closes and the mirrors jump.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from galvo.geometry import Points, as_points

XY = tuple[float, float]


@dataclass(frozen=True)
class PointTarget:
    xy: XY


@dataclass(frozen=True)
class LineTarget:
    start: XY
    end: XY
    n_points: int

    def __post_init__(self) -> None:
        if self.n_points < 2:
            raise ValueError("a line needs at least 2 points")


@dataclass(frozen=True)
class RegionTarget:
    """A closed polygon filled with a square grid of spots ``spacing`` pixels apart."""

    vertices: tuple[XY, ...]
    spacing: float

    def __post_init__(self) -> None:
        if len(self.vertices) < 3:
            raise ValueError("a region needs at least 3 vertices")
        if self.spacing <= 0:
            raise ValueError("region spacing must be positive")


Pattern = PointTarget | LineTarget | RegionTarget


def rasterize(pattern: Pattern) -> list[Points]:
    """Ordered beam positions for ``pattern``, split into gate-open segments."""
    match pattern:
        case PointTarget(xy):
            return [as_points(xy)]
        case LineTarget(start, end, n):
            t = np.linspace(0.0, 1.0, n)[:, None]
            return [as_points(start) + t * (as_points(end) - as_points(start))]
        case RegionTarget(vertices, spacing):
            return _raster_region(as_points(vertices), spacing)


def _raster_region(vertices: Points, spacing: float) -> list[Points]:
    # Kept from the original: serpentine (boustrophedon) rows, alternating direction, so
    # the beam never flies back across the region between rows.
    x_min, y_min = vertices.min(axis=0)
    y_max = vertices[:, 1].max()
    edges = np.stack([vertices, np.roll(vertices, -1, axis=0)], axis=1)  # (E, 2, 2)

    ordered: list[Points] = []
    for row, y in enumerate(np.arange(y_min + spacing / 2, y_max, spacing)):
        runs = [_grid_run(a, b, y, x_min, spacing) for a, b in _scanline(edges, y)]
        runs = [r for r in runs if len(r)]
        if row % 2:
            runs = [r[::-1] for r in reversed(runs)]
        ordered.extend(runs)
    if not ordered:
        return []

    # Split where consecutive spots are not neighbours (a gap in a concave shape, or a
    # big shift between rows), so the gate closes rather than painting outside the region.
    points = np.vstack(ordered)
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    breaks = np.flatnonzero(steps > 1.5 * spacing) + 1
    return [s for s in np.split(points, breaks) if len(s)]


def _scanline(edges: Points, y: float) -> list[tuple[float, float]]:
    """Inside intervals of a horizontal line through the polygon (even-odd rule)."""
    (x0, y0), (x1, y1) = edges[:, 0].T, edges[:, 1].T
    crosses = (y0 <= y) != (y1 <= y)
    xs = np.sort(x0[crosses] + (y - y0[crosses]) * (x1 - x0)[crosses] / (y1 - y0)[crosses])
    return [(float(xs[i]), float(xs[i + 1])) for i in range(0, len(xs) - 1, 2)]


def _grid_run(a: float, b: float, y: float, x_min: float, spacing: float) -> Points:
    # Columns sit on one global grid so spots line up from row to row.
    first = math.ceil((a - x_min) / spacing - 0.5)
    last = math.floor((b - x_min) / spacing - 0.5)
    xs = x_min + spacing / 2 + spacing * np.arange(first, last + 1)
    return np.column_stack([xs, np.full_like(xs, y)])


def pattern_to_dict(pattern: Pattern) -> dict[str, Any]:
    match pattern:
        case PointTarget(xy):
            return {"type": "point", "xy": list(xy)}
        case LineTarget(start, end, n):
            return {"type": "line", "start": list(start), "end": list(end), "n_points": n}
        case RegionTarget(vertices, spacing):
            return {"type": "region", "vertices": [list(v) for v in vertices], "spacing": spacing}
