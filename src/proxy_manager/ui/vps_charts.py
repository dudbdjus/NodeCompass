from datetime import datetime
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


RING_REMAINING = "#2166d9"
RING_USED = "#909baa"
RING_UNKNOWN = "#e2e7ee"


class UsageRing(QWidget):
    def __init__(self):
        super().__init__()
        self.usage = None
        self.setFixedHeight(128)
        self.setMinimumWidth(128)

    def show_usage(self, usage):
        self.usage = usage
        text = (f"灰色：已用 {usage:.1f}% · 蓝色：剩余 {max(0, 100 - usage):.1f}%"
                if usage is not None else "暂无使用比例")
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.update()

    def paintEvent(self, event):
        size = min(self.width(), self.height()) - 22
        ring = QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(RING_UNKNOWN if self.usage is None else RING_REMAINING), 13))
            painter.drawEllipse(ring)
            if self.usage is not None:
                painter.setPen(QPen(QColor(RING_USED), 13))
                painter.drawArc(ring, 90 * 16, -round(min(100, max(0, self.usage)) / 100 * 360 * 16))
            font = painter.font()
            font.setBold(True)
            font.setPointSize(13)
            painter.setFont(font)
            painter.setPen(QColor("#164580"))
            painter.drawText(ring, Qt.AlignmentFlag.AlignCenter,
                             f"已用\n{self.usage:.1f}%" if self.usage is not None else "—")


def series_segments(points, direction: str, gap_seconds: float | None):
    segments = []
    current = []
    last = None
    for point in points:
        value = getattr(point, direction)
        if last is not None and (gap_seconds is None or point.timestamp - last.timestamp > gap_seconds
                                 or point.interface != last.interface):
            if current:
                segments.append(current)
            current = []
        if value is None or not math.isfinite(value):
            if current:
                segments.append(current)
            current = []
        else:
            current.append((point.timestamp, value))
        last = point
    if current:
        segments.append(current)
    return segments


class TrafficChart(QWidget):
    def __init__(self, unit, parent=None, *, colors=("#178595", "#b86429"), grid=False, legend=("VPS RX", "VPS TX")):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.unit = unit
        self.colors = tuple(QColor(color) for color in colors)
        self.grid = grid
        self.legend = legend
        self.update_series([], None)

    def update_series(self, points, gap_seconds, *, bounds=None):
        self.rx = series_segments(points, "rx", gap_seconds)
        self.tx = series_segments(points, "tx", gap_seconds)
        self.bounds = bounds or ((points[0].timestamp, points[-1].timestamp) if points else None)
        self.update()

    def paintEvent(self, event):
        with QPainter(self) as p:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            text = self.palette().color(self.foregroundRole())
            p.setPen(text)
            p.drawText(12, 20, self.unit)
            p.setPen(self.colors[0])
            p.drawText(self.width() - 125, 20, self.legend[0])
            p.setPen(self.colors[1])
            p.drawText(self.width() - 64, 20, self.legend[1])
            if self.bounds is None or (not self.rx and not self.tx):
                p.setPen(text)
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无有效采样")
                return
            left = 38 if self.grid else 78
            rect = QRectF(left, 32, max(1, self.width() - left - 22), max(1, self.height() - 66))
            start, end = self.bounds
            maximum = max(1, max(value for segment in self.rx + self.tx for _, value in segment))
            span = max(1, end - start)
            p.setPen(text)
            p.drawText(8, int(rect.top() + 9), f"{maximum:.3g}")
            p.drawText(8, int(rect.bottom()), "0")
            stamp = "%m-%d %H:%M"
            p.drawText(QRectF(rect.left(), rect.bottom() + 3, rect.width(), 25), Qt.AlignmentFlag.AlignLeft,
                       datetime.fromtimestamp(start).strftime(stamp))
            p.drawText(QRectF(rect.left(), rect.bottom() + 3, rect.width(), 25), Qt.AlignmentFlag.AlignRight,
                       datetime.fromtimestamp(end).strftime(stamp))
            if self.grid:
                p.setPen(QPen(QColor("#dce6f4"), 1, Qt.PenStyle.DashLine))
                for index in range(5):
                    y = rect.top() + rect.height() * index / 4
                    p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            else:
                p.setPen(self.palette().mid().color())
                p.drawRect(rect)
            p.setClipRect(rect)
            for color, segments in zip(self.colors, (self.rx, self.tx)):
                p.setPen(QPen(color, 2))
                for segment in segments:
                    path = QPainterPath()
                    for index, (timestamp, value) in enumerate(segment):
                        point = QPointF(rect.left() + (timestamp - start) / span * rect.width(),
                                        rect.bottom() - value / maximum * rect.height())
                        if index == 0:
                            path.moveTo(point)
                            p.drawEllipse(point, 2, 2)
                        else:
                            path.lineTo(point)
                    p.drawPath(path)
