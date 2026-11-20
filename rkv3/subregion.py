# -*- coding: utf-8 -*-
"""A játék feliratának helye: a képen egérrel kijelölöd azt a sávot, ahol a beszélők szövege megjelenik. A felirat-olvasó
(subby.subocr) ezt a téglalapot olvassa; a „Próba-olvasás” gomb azonnal megmutatja, mit lát benne."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from .hudmark import _Canvas


class SubRegionDialog(QDialog):
    def __init__(self, img: QImage, rect, profile_name="", test_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Felirat helyének kijelölése – felirat-olvasó")
        self.resize(1280, 860)
        self.test_fn = test_fn
        self.cleared = False
        lay = QVBoxLayout(self)
        hint = QLabel(
            "<b>Húzz egy keretet a játék feliratsávja köré</b> – oda, ahol a beszélők angol szövege megjelenik "
            "(általában a kép alsó része). Az új keret felváltja a régit."
            "<br><span style='color:#8a92b8'>Szűk keret a legjobb: csak a feliratsáv legyen benne, a HUD szövegei "
            "(célkitűzés, súgó, pontszám) ne. A képen az a pillanat látszik, amikor megnyitottad az ablakot – ha "
            "nincs rajta felirat, zárd be, várd meg, amíg valaki beszél (vagy állítsd meg a játékot egy feliratnál), "
            f"és nyisd meg újra. Játékprofil: {profile_name}.</span>")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.canvas = _Canvas(img, [rect] if rect else [], max_rects=1)
        lay.addWidget(self.canvas, 1)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFixedHeight(92)
        self.out.setPlaceholderText("A „Próba-olvasás” itt mutatja, mit olvas le a program a kijelölt keretből.")
        lay.addWidget(self.out)
        row = QHBoxLayout()
        self.info = QLabel("")
        self.info.setObjectName("Muted")
        self.btn_test = QPushButton("Próba-olvasás")
        self.btn_test.clicked.connect(self._test)
        self.btn_clear = QPushButton("Hely törlése")
        self.btn_clear.setToolTip("Nincs kijelölt hely: a felirat-olvasó nem fut, minden a hangból megy (a régi mód).")
        self.btn_clear.clicked.connect(self._clear)
        cancel = QPushButton("Mégse")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("Mentés és olvasás")
        self.ok.setObjectName("Primary")
        self.ok.clicked.connect(self.accept)
        row.addWidget(self.info, 1)
        for b in (self.btn_test, self.btn_clear, cancel, self.ok):
            row.addWidget(b)
        lay.addLayout(row)
        self.canvas.on_change = self._changed
        self._changed()

    def _changed(self):
        has = bool(self.canvas.rects)
        self.ok.setEnabled(has)
        self.btn_test.setEnabled(has and self.test_fn is not None)
        self.info.setText("Kijelölt hely: a mentés után másodpercenként néhányszor ezt olvassa."
                          if has else "Még nincs kijelölt keret – húzz egyet a képen.")

    def _clear(self):
        self.cleared = True
        self.canvas.rects.clear()
        self.accept()

    def rect(self):
        """(x, y, szélesség, magasság) 0..1 arányban, vagy None."""
        if not self.canvas.rects:
            return None
        r = self.canvas.rects[0]
        return (r.x(), r.y(), r.width(), r.height())

    def _test(self):
        rect = self.rect()
        if rect is None or self.test_fn is None:
            return
        self.out.setPlainText("Olvasás… (az első alkalommal pár másodperc, amíg az OCR-modell betöltődik)")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            rd = self.test_fn(rect)
        except ImportError:
            self.out.setPlainText("Az OCR-csomag nincs telepítve (rapidocr-onnxruntime). Telepítsd: "
                                  "python -m pip install rapidocr-onnxruntime")
            return
        except Exception as e:
            self.out.setPlainText(f"Az olvasás nem sikerült: {e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        if not rd.lines:
            self.out.setPlainText("Nem olvasok szöveget a keretben" + (f" ({rd.why})." if rd.why else ".")
                                  + "\nHa a képen van felirat: húzd a keretet pontosabban rá, vagy nagyobbra.")
            return
        rows = [f"{i + 1}. sor ({c:.0%}): {t}" for i, (t, c) in enumerate(rd.lines)]
        verdict = (f"Felirat-sorként elfogadva: „{rd.text}”" if not rd.why
                   else f"Nem fogadnám el feliratnak ({rd.why}): „{rd.text}”")
        self.out.setPlainText("\n".join(rows + ["", verdict]))
