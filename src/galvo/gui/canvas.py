"""The targeting area: draw a point, line or region over the microscope image.

Scene coordinates are the view's own pixels (no zoom, no scrolling), so a click at
(x, y) is image pixel (x, y) in the calibration's terms.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QWidget,
)

from galvo.geometry import Points
from galvo.patterns import LineTarget, Pattern, PointTarget, RegionTarget

TARGET_COLOUR = QColor("#ff3b30")
PREVIEW_COLOUR = QColor("#ffcc00")
MIN_REGION_AREA_PX = 100.0


class Tool(Enum):
    POINT = "Point"
    LINE = "Line"
    REGION = "Region"


class TargetCanvas(QGraphicsView):
    targetChanged = Signal()  # a target was placed, moved, finished or cleared
    pointMoved = Signal()  # the point target moved (fires continuously while dragging)
    calibrationClick = Signal(QPointF)
    resized = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#101418"))
        self.setMouseTracking(False)
        self.setMinimumSize(200, 200)

        self.tool = Tool.POINT
        self.calibrating = False
        self.point: QPointF | None = None
        self.line: tuple[QPointF, QPointF] | None = None
        self.region: list[QPointF] | None = None
        self._dragging = False

        scene = self.scene()
        self._exposed = scene.addPath(QPainterPath(), QPen(Qt.PenStyle.NoPen))
        self._exposed.setZValue(1)
        self._preview = scene.addPath(QPainterPath(), QPen(PREVIEW_COLOUR, 1))
        self._preview.setZValue(2)
        self._target = scene.addPath(QPainterPath(), QPen(TARGET_COLOUR, 2))
        self._target.setZValue(3)
        self._trail = scene.addPath(QPainterPath(), QPen(Qt.PenStyle.NoPen))
        self._trail.setZValue(4)
        self._beam = QGraphicsEllipseItem(-6, -6, 12, 12)
        self._beam.setZValue(5)
        self._beam.setVisible(False)
        scene.addItem(self._beam)
        self._cal_marks = scene.addPath(QPainterPath(), QPen(QColor("#ff2d55"), 2))
        self._cal_marks.setZValue(6)
        self._exposed_paths: list[QGraphicsPathItem] = []

        # Warning strip across the top of the drawing area; clicks pass through it.
        self.warning = QLabel(self.viewport())
        self.warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning.setWordWrap(True)
        self.warning.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.warning.setStyleSheet(
            "background: rgba(255, 59, 48, 210); color: white; font-weight: 700;"
            "font-size: 15px; padding: 8px; border-radius: 4px"
        )
        self.warning.setVisible(False)

    # Geometry

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.setSceneRect(QRectF(0, 0, self.viewport().width(), self.viewport().height()))
        self._place_warning()
        self.resized.emit()

    def set_warning(self, text: str | None) -> None:
        self.warning.setText(text or "")
        self.warning.setVisible(bool(text))
        self._place_warning()

    def _place_warning(self) -> None:
        width = self.viewport().width() - 24
        self.warning.setFixedWidth(max(width, 100))
        self.warning.adjustSize()
        self.warning.move(12, 12)

    def view_size(self) -> tuple[int, int]:
        return self.viewport().width(), self.viewport().height()

    # Mouse: drawing targets

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p = self.mapToScene(event.position().toPoint())
        if self.calibrating:
            self.calibrationClick.emit(p)
            return
        self._dragging = True
        if self.tool is Tool.POINT:
            self.point = p
            self.pointMoved.emit()
        elif self.tool is Tool.LINE:
            self.line = (p, p)
        else:
            self.region = [p]
        self._redraw_target()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            return
        p = self.mapToScene(event.position().toPoint())
        if self.tool is Tool.POINT:
            self.point = p
            self.pointMoved.emit()
        elif self.tool is Tool.LINE and self.line:
            self.line = (self.line[0], p)
        elif self.region is not None and _dist(self.region[-1], p) >= 3:
            self.region.append(p)
        self._redraw_target()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        if self.line and _dist(*self.line) < 3:
            self.line = None
        if self.region is not None and _polygon_area(self.region) < MIN_REGION_AREA_PX:
            self.region = None
        self._redraw_target()
        self.targetChanged.emit()

    # Targets

    def set_tool(self, tool: Tool) -> None:
        self.tool = tool
        self._redraw_target()
        self.targetChanged.emit()

    def clear_target(self) -> None:
        self.point, self.line, self.region = None, None, None
        self._redraw_target()
        self.targetChanged.emit()

    def pattern(self, line_points: int, spacing_px: float) -> Pattern | None:
        """The current tool's target as a pattern, or None if nothing is drawn."""
        if self.tool is Tool.POINT and self.point is not None:
            return PointTarget((self.point.x(), self.point.y()))
        if self.tool is Tool.LINE and self.line is not None:
            a, b = self.line
            return LineTarget((a.x(), a.y()), (b.x(), b.y()), line_points)
        if self.tool is Tool.REGION and self.region is not None:
            return RegionTarget(tuple((p.x(), p.y()) for p in self.region), spacing_px)
        return None

    def _redraw_target(self) -> None:
        path = QPainterPath()
        if self.tool is Tool.POINT and self.point is not None:
            # Kept from the original: a circled crosshair marks the point target.
            x, y, r = self.point.x(), self.point.y(), 8.0
            path.addEllipse(self.point, r, r)
            path.moveTo(x - r - 4, y)
            path.lineTo(x + r + 4, y)
            path.moveTo(x, y - r - 4)
            path.lineTo(x, y + r + 4)
        elif self.tool is Tool.LINE and self.line is not None:
            path.moveTo(self.line[0])
            path.lineTo(self.line[1])
        elif self.tool is Tool.REGION and self.region:
            path.addPolygon(QPolygonF(self.region))
            if not self._dragging:
                path.closeSubpath()
        self._target.setPath(path)

    # Overlays

    def set_preview(self, segments: list[Points]) -> None:
        """Small circles where the beam will dwell."""
        path = QPainterPath()
        for seg in segments:
            for x, y in seg:
                path.addEllipse(QPointF(x, y), 2.0, 2.0)
        self._preview.setPath(path)

    def add_exposed(self, segments: list[Points], colour: QColor) -> None:
        """Mark spots that were exposed, so you can see what has been hit this session."""
        path = QPainterPath()
        for seg in segments:
            for x, y in seg:
                path.addEllipse(QPointF(x, y), 3.0, 3.0)
        fill = QColor(colour)
        fill.setAlpha(90)
        item = self.scene().addPath(path, QPen(Qt.PenStyle.NoPen), QBrush(fill))
        item.setZValue(1)
        self._exposed_paths.append(item)

    def clear_exposed(self) -> None:
        for item in self._exposed_paths:
            self.scene().removeItem(item)
        self._exposed_paths.clear()

    def set_beam(self, xy: tuple[float, float] | None, colour: QColor | None) -> None:
        """The simulated beam: a filled dot when a gate is open, a faint ring when closed."""
        if xy is None:
            self._beam.setVisible(False)
            return
        self._beam.setPos(QPointF(*xy))
        if colour is None:
            self._beam.setPen(QPen(QColor(255, 255, 255, 90), 1))
            self._beam.setBrush(Qt.BrushStyle.NoBrush)
        else:
            self._beam.setPen(QPen(colour.lighter(150), 2))
            self._beam.setBrush(QBrush(colour))
        self._beam.setVisible(True)

    def set_trail(self, points: Points, colour: QColor | None) -> None:
        path = QPainterPath()
        for x, y in points:
            path.addEllipse(QPointF(x, y), 1.5, 1.5)
        self._trail.setPath(path)
        self._trail.setBrush(QBrush(colour) if colour is not None else Qt.BrushStyle.NoBrush)

    def set_calibration_marks(self, points: list[QPointF]) -> None:
        path = QPainterPath()
        for p in points:
            path.moveTo(p.x() - 7, p.y())
            path.lineTo(p.x() + 7, p.y())
            path.moveTo(p.x(), p.y() - 7)
            path.lineTo(p.x(), p.y() + 7)
        self._cal_marks.setPath(path)


def _dist(a: QPointF, b: QPointF) -> float:
    return float(np.hypot(a.x() - b.x(), a.y() - b.y()))


def _polygon_area(points: list[QPointF]) -> float:
    if len(points) < 3:
        return 0.0
    x = np.array([p.x() for p in points])
    y = np.array([p.y() for p in points])
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)
