# -*- coding: utf-8 -*-
"""HUD megjelölése: a kimerevített képen egérrel bekeretezed a mindig látszó elemeket (minitérkép, életerő…)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

MAX_RECTS = 4
COLORS = ["#ff3fb4", "#2f8cff", "#38d27a", "#ffc53d"]


def frame_to_qimage(frame) -> QImage:
    arr = frame.to_ndarray(format="rgb24")
    h, w = arr.shape[:2]
    return QImage(arr.tobytes(), w, h, 3 * w, QImage.Format_RGB888).copy()


class _Canvas(QWidget):
    def __init__(self, img: QImage, rects):
        super().__init__()
        self.img = QPixmap.fromImage(img)
        self.rects = [QRectF(*r) for r in rects]        # 0..1 arányban
        self.drag = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setMinimumSize(640, 360)
        self.on_change = lambda: None

    def _area(self) -> QRectF:
        s = self.img.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRectF((self.width() - s.width()) / 2, (self.height() - s.height()) / 2, s.width(), s.height())

    def _rel(self, pos) -> QPointF:
        a = self._area()
        return QPointF(min(1.0, max(0.0, (pos.x() - a.x()) / a.width())),
                       min(1.0, max(0.0, (pos.y() - a.y()) / a.height())))

    def mousePressEvent(self, e):
        p = self._rel(e.position())
        if e.button() == Qt.RightButton:
            for r in reversed(self.rects):
                if r.contains(p):
                    self.rects.remove(r)
                    break
            self.on_change()
            self.update()
        elif e.button() == Qt.LeftButton and len(self.rects) < MAX_RECTS:
            self.drag = (p, p)
            self.update()

    def mouseMoveEvent(self, e):
        if self.drag:
            self.drag = (self.drag[0], self._rel(e.position()))
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag and e.button() == Qt.LeftButton:
            r = QRectF(self.drag[0], self._rel(e.position())).normalized()
            self.drag = None
            if r.width() > 0.015 and r.height() > 0.015:
                self.rects.append(r)
            self.on_change()
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(4, 6, 13))
        a = self._area()
        p.drawPixmap(a.toRect(), self.img)
        p.setRenderHint(QPainter.Antialiasing)

        def to_px(r):
            return QRectF(a.x() + r.x() * a.width(), a.y() + r.y() * a.height(), r.width() * a.width(),
                          r.height() * a.height())
        for i, r in enumerate(self.rects):
            c = QColor(COLORS[i % len(COLORS)])
            p.setPen(QPen(c, 2))
            fill = QColor(c)
            fill.setAlpha(45)
            p.setBrush(fill)
            p.drawRect(to_px(r))
            p.setPen(QColor("white"))
            p.drawText(to_px(r).adjusted(5, 3, 0, 0), Qt.AlignLeft | Qt.AlignTop, str(i + 1))
        if self.drag:
            p.setPen(QPen(QColor("white"), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(to_px(QRectF(self.drag[0], self.drag[1]).normalized()))
        p.end()


class HudMarkDialog(QDialog):
    def __init__(self, img: QImage, rects=(), profile_name="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("HUD megjelölése – cutscene-figyelő")
        self.resize(1280, 820)
        lay = QVBoxLayout(self)
        hint = QLabel(
            f"<b>Keretezd be egérrel a játék közben mindig látszó elemeket</b> (minitérkép, életerő, lőszer, óra) – "
            f"legfeljebb {MAX_RECTS}-et. Jobb kattintás: törlés. Olyan pillanatot válassz, amikor a HUD látszik."
            f"<br><span style='color:#8a92b8'>Játékprofil: {profile_name}. Mentés után kb. 8 másodpercig tanul: közben "
            f"játssz tovább, mozogj – így megtanulja, mi állandó (a keret) és mi változik (pl. a térkép tartalma).</span>")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.canvas = _Canvas(img, rects)
        lay.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        self.info = QLabel("")
        self.info.setObjectName("Muted")
        clear = QPushButton("Mind törlése")
        clear.clicked.connect(self._clear)
        cancel = QPushButton("Mégse")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("Mentés és figyelés")
        self.ok.setObjectName("Primary")
        self.ok.clicked.connect(self.accept)
        row.addWidget(self.info, 1)
        for b in (clear, cancel, self.ok):
            row.addWidget(b)
        lay.addLayout(row)
        self.canvas.on_change = self._changed
        self._changed()

    def _clear(self):
        self.canvas.rects.clear()
        self.canvas.update()
        self._changed()

    def _changed(self):
        n = len(self.canvas.rects)
        self.ok.setEnabled(n > 0)
        self.info.setText(f"{n} elem megjelölve" if n else "Még nincs megjelölt elem – húzz keretet a képen.")

    def rects(self):
        return [(r.x(), r.y(), r.width(), r.height()) for r in self.canvas.rects]
