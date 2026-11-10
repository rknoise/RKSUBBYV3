# -*- coding: utf-8 -*-
"""Ikonok a Windows saját ikonbetűtípusából (Segoe Fluent Icons / Segoe MDL2 Assets) – nincs külön képfájl."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

GLYPH = {
    "settings": "", "fullscreen": "", "window": "", "camera": "", "volume": "",
    "mute": "", "record": "", "pause": "", "play": "", "stop": "",
    "delete": "", "folder": "", "info": "", "cc": "", "video": "",
    "export": "", "mic": "", "translate": "", "refresh": "", "check": "",
    "warning": "", "paste": "", "eye": "", "link": "", "sidebar": "",
}
_family = None


def family():
    global _family
    if _family is None:
        fams = set(QFontDatabase.families())
        _family = "Segoe Fluent Icons" if "Segoe Fluent Icons" in fams else "Segoe MDL2 Assets"
    return _family


def icon(name, color="#ececf1", size=20):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing)
    f = QFont(family())
    f.setPixelSize(int(size * 2 * 0.72))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRectF(0, 0, size * 2, size * 2), Qt.AlignCenter, GLYPH.get(name, "?"))
    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)
