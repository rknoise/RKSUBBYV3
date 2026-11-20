# -*- coding: utf-8 -*-
"""A felirat-olvasó két Qt-ablaka kijelző nélkül: Beállítások › Felirat-olvasó és a felirat helyének kijelölése.
PySide6 nélkül a tesztek kimaradnak. Futtatás: python -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RKSUBBY_HOME", tempfile.mkdtemp(prefix="rksubby-test-ui-"))   # a valódi beállításokhoz nem nyúlunk
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")                                 # nincs szükség kijelzőre

try:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:       # nincs PySide6 (vagy nincs hozzá grafikus könyvtár)
    HAVE_QT = False

from subby import subocr  # noqa: E402
from subby.config import Config  # noqa: E402


def make_cfg(**changes):
    c = Config(path=os.path.join(os.environ["RKSUBBY_HOME"], "config.json"), load=False)
    c.update(dict({"log_enabled": False}, **changes))
    return c


@unittest.skipUnless(HAVE_QT, "PySide6 nincs telepítve")
class OcrDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from rkv3.theme import QSS
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(QSS)

    def test_settings_page_roundtrip(self):
        from rkv3.settings import SettingsDialog
        cfg = make_cfg()
        dlg = SettingsDialog(cfg, [], None, 0, outputs=[])
        names = [dlg.nav.item(i).text() for i in range(dlg.nav.count())]
        self.assertIn("Felirat-olvasó", names)
        ch = dlg.changes()
        for key in ("subocr_mode", "subocr_fps", "subocr_min_conf", "subocr_linger_s", "subocr_static_s"):
            self.assertIn(key, ch)
        self.assertEqual(ch["subocr_mode"], "auto")
        cfg.update(ch)                                   # a mentés útja: a Config elfogadja és határok közé szorítja
        self.assertEqual(cfg.subocr_mode, "auto")
        self.assertTrue(1 <= cfg.subocr_fps <= 10)
        dlg.nav.setCurrentRow(names.index("Felirat-olvasó"))
        dlg.show()
        self.app.processEvents()
        dlg.close()

    def test_settings_page_changes_apply(self):
        from rkv3.settings import SettingsDialog
        cfg = make_cfg(subocr_mode="off", subocr_fps=2, subocr_linger_s=1.5)
        dlg = SettingsDialog(cfg, [], None, 0, outputs=[])
        ch = dlg.changes()
        self.assertEqual(ch["subocr_mode"], "off")       # a megnyitott ablak a mentett értékeket mutatja
        self.assertEqual(ch["subocr_fps"], 2)
        self.assertAlmostEqual(ch["subocr_linger_s"], 1.5, places=2)
        dlg.close()

    def test_region_dialog_rect_and_test_reading(self):
        from rkv3.subregion import SubRegionDialog
        img = QImage(1280, 720, QImage.Format_RGB888)
        img.fill(0x203040)
        seen = []

        def test_fn(rect):
            seen.append(rect)
            return subocr.Reading(text="Get in the car, Lucia.", conf=0.93, lines=[("Get in the car, Lucia.", 0.93)])

        d = SubRegionDialog(img, subocr.DEFAULT_RECT, "GTA V", test_fn)
        d.show()
        self.app.processEvents()
        self.assertEqual(d.rect(), subocr.clean_rect(subocr.DEFAULT_RECT))
        d._test()
        self.assertEqual(len(seen), 1)
        self.assertIn("Get in the car, Lucia.", d.out.toPlainText())
        # új keret felváltja a régit (csak egy felirat-sáv lehet)
        d.canvas.rects = [QRectF(0.1, 0.1, 0.2, 0.1)]
        self.assertEqual(d.rect(), subocr.clean_rect((0.1, 0.1, 0.2, 0.1)))
        d.close()

    def test_region_dialog_without_rect_cannot_save(self):
        from rkv3.subregion import SubRegionDialog
        img = QImage(640, 360, QImage.Format_RGB888)
        img.fill(0x102030)
        d = SubRegionDialog(img, None, "teszt", lambda rect: None)
        self.assertIsNone(d.rect())
        self.assertFalse(d.ok.isEnabled())
        self.assertFalse(d.btn_test.isEnabled())
        d.close()


if __name__ == "__main__":
    unittest.main()
