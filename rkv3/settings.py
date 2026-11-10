# -*- coding: utf-8 -*-
"""Beállítások (a fogaskerék mögött): fordítás, beszédfelismerés, felirat, felvétel, kép és hang, névjegy."""
from __future__ import annotations

import os
import threading
import webbrowser

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
                               QRadioButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from subby import config as sc
from subby.textutil import fmt_int, short_err

from . import __version__
from .icons import icon

MODELS = [("distil-large-v3", "distil-large-v3 – gyors és pontos (ajánlott)"),
          ("large-v3-turbo", "large-v3-turbo – pontosabb, kicsit lassabb"),
          ("large-v3", "large-v3 – a legpontosabb, a leglassabb"),
          ("small.en", "small.en – kicsi, gyenge géphez")]
TRANSLATORS = [("deepl", "DeepL (online, a legjobb minőség, kulcs kell)"),
               ("ollama", "Lokális AI (Ollama a saját gépeden, semmi nem megy ki)"),
               ("google", "Google (online, ingyenes, kulcs nélkül)"),
               ("none", "Nincs fordítás (angol felirat)")]


class _Bridge(QObject):
    result = Signal(str, object)


def _row(*widgets, stretch=True):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    for x in widgets:
        lay.addWidget(x)
    if stretch:
        lay.addStretch(1)
    return w


def _hint(text):
    lb = QLabel(text)
    lb.setObjectName("Small")
    lb.setWordWrap(True)
    lb.setTextFormat(Qt.RichText)
    lb.setOpenExternalLinks(True)
    return lb


class ColorButton(QPushButton):
    def __init__(self, color):
        super().__init__()
        self.setFixedWidth(90)
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, c):
        self.color = c
        self.setText(c)
        self.setStyleSheet(f"QPushButton {{ background: {c}; color: {'#000' if QColor(c).lightness() > 140 else '#fff'};"
                           f" border-radius: 8px; }}")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Szín")
        if c.isValid():
            self.set_color(c.name())


class SettingsDialog(QDialog):
    def __init__(self, cfg, devices: list[str], parent=None, page=0):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Beállítások – rksubby V3")
        self.resize(940, 640)
        self._bridge = _Bridge()
        self._bridge.result.connect(self._on_result)
        self.key_changed = False

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(14)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(210)
        self.stack = QStackedWidget()
        for name, ic, builder in (("Fordítás", "translate", self._page_translate),
                                  ("Beszédfelismerés", "mic", self._page_asr),
                                  ("Felirat", "cc", self._page_subs),
                                  ("Felvétel", "record", self._page_rec),
                                  ("Kép és hang", "video", lambda: self._page_av(devices)),
                                  ("Névjegy", "info", self._page_about)):
            self.nav.addItem(name)
            self.nav.item(self.nav.count() - 1).setIcon(icon(ic, "#c9b8ff", 18))
            page_w = QWidget()
            lay = QVBoxLayout(page_w)
            lay.setContentsMargins(4, 0, 4, 0)
            title = QLabel(name)
            title.setStyleSheet("font-size: 15pt; font-weight: 600;")
            lay.addWidget(title)
            builder_layout = builder()
            lay.addLayout(builder_layout) if not isinstance(builder_layout, QWidget) else lay.addWidget(builder_layout)
            lay.addStretch(1)
            self.stack.addWidget(page_w)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(page)

        right = QVBoxLayout()
        right.addWidget(self.stack, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Mégse")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Mentés")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(save)
        right.addLayout(btns)
        left = QVBoxLayout()
        left.addWidget(self.nav, 1)
        root.addLayout(left)
        root.addLayout(right, 1)

    # ------------------------------------------------------------------ pages
    def _page_translate(self):
        c = self.cfg
        lay = QVBoxLayout()
        box = QGroupBox("Fordító")
        bl = QVBoxLayout(box)
        self.tr_group = QButtonGroup(self)
        self.tr_radios = {}
        cur = c.translator if c.translator != "auto" else "deepl"
        for key, label in TRANSLATORS:
            rb = QRadioButton(label)
            rb.setChecked(key == cur)
            self.tr_group.addButton(rb)
            self.tr_radios[key] = rb
            bl.addWidget(rb)
        self.fallback = QCheckBox("Tartalék fordító: ha a választott nem elérhető, a Lokális AI, végül a Google fordít "
                                  "(így a felirat magyar marad)")
        self.fallback.setChecked(c.translate_fallback)
        bl.addWidget(self.fallback)
        lay.addWidget(box)

        dbox = QGroupBox("DeepL")
        form = QFormLayout(dbox)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.key_edit = QLineEdit(sc.load_deepl_key())
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("pl. 1a2b3c4d-....-....:fx")
        self.key_edit.textChanged.connect(lambda: setattr(self, "key_changed", True))
        show = QPushButton()
        show.setIcon(icon("eye", "#c9b8ff", 16))
        show.setCheckable(True)
        show.setToolTip("Kulcs mutatása")
        show.toggled.connect(lambda on: self.key_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        paste = QPushButton("Beillesztés")
        paste.setIcon(icon("paste", "#c9b8ff", 16))
        paste.clicked.connect(lambda: self.key_edit.setText(QGuiApplication.clipboard().text().strip()))
        check = QPushButton("Ellenőrzés")
        check.clicked.connect(self._check_deepl)
        krow = QWidget()
        kl = QHBoxLayout(krow)
        kl.setContentsMargins(0, 0, 0, 0)
        kl.addWidget(self.key_edit, 1)
        for b in (show, paste, check):
            kl.addWidget(b)
        form.addRow("API-kulcs:", krow)
        self.deepl_status = QLabel("")
        self.deepl_status.setWordWrap(True)
        form.addRow("", self.deepl_status)
        form.addRow("", _hint('Ingyenes kulcs (havi 500 000 karakter): <a href="https://www.deepl.com/pro-api" '
                              'style="color:#c9a8ff">deepl.com/pro-api</a> → a kulcs: <a href="https://www.deepl.com/'
                              'your-account/keys" style="color:#c9a8ff">Fiók › Kulcsok</a>. A Free kulcs vége <b>:fx</b>. '
                              'A kulcs a felhasználói mappádba kerül (%USERPROFILE%\\.rksubby), nem a programmappába.'))
        self.formality = QComboBox()
        self.formality.addItem("Tegező", "prefer_less")
        self.formality.addItem("Magázó", "prefer_more")
        self.formality.setCurrentIndex(1 if c.deepl_formality == "prefer_more" else 0)
        form.addRow("Hangnem:", self.formality)
        self.glossary = QCheckBox("A játékprofil szótára (nevek, kifejezések) DeepL-glosszáriumként")
        self.glossary.setChecked(c.deepl_glossary)
        form.addRow("", self.glossary)
        self.spec = QCheckBox("Előre fordítás a mondat vége előtt (gyorsabb felirat, kicsit több karakter)")
        self.spec.setChecked(c.spec_translate)
        form.addRow("", self.spec)
        lay.addWidget(dbox)

        obox = QGroupBox("Lokális AI (Ollama)")
        of = QFormLayout(obox)
        self.ollama_url = QLineEdit(c.ollama_url)
        self.ollama_model = QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.addItems([c.ollama_model, "translategemma:12b", "translategemma:4b", "gemma3:12b"])
        self.ollama_model.setCurrentText(c.ollama_model)
        ocheck = QPushButton("Ellenőrzés")
        ocheck.clicked.connect(self._check_ollama)
        of.addRow("Cím:", self.ollama_url)
        of.addRow("Modell:", _row(self.ollama_model, ocheck))
        self.ollama_status = QLabel("")
        self.ollama_status.setWordWrap(True)
        of.addRow("", self.ollama_status)
        of.addRow("", _hint('Telepítés: <a href="https://ollama.com" style="color:#c9a8ff">ollama.com</a>, majd '
                            'parancssorban: <b>ollama pull translategemma:12b</b>. Minden a gépeden marad.'))
        lay.addWidget(obox)
        return lay

    def _page_asr(self):
        c = self.cfg
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.model = QComboBox()
        for k, label in MODELS:
            self.model.addItem(label, k)
        idx = max(0, self.model.findData(c.model))
        self.model.setCurrentIndex(idx)
        form.addRow("Modell:", self.model)
        self.device = QComboBox()
        for k, label in (("auto", "Automatikus (NVIDIA GPU, ha van)"), ("cuda", "NVIDIA GPU"), ("cpu", "Processzor")):
            self.device.addItem(label, k)
        self.device.setCurrentIndex(max(0, self.device.findData(c.asr_device)))
        form.addRow("Futtatás:", self.device)
        self.profile = QComboBox()
        try:
            for p in sc.list_profiles():
                self.profile.addItem(p["name"], p["id"])
        except Exception:
            pass
        self.profile.setCurrentIndex(max(0, self.profile.findData(c.profile)))
        form.addRow("Játékprofil:", self.profile)
        form.addRow("", _hint("A profil a játék neveit és helyeit adja a felismerőnek és a fordítónak "
                              "(GTA V, GTA VI, RDR2, általános). Saját profil: %USERPROFILE%\\.rksubby\\profiles"))
        self.partial = QCheckBox("Beszéd közben szürke angol előnézet")
        self.partial.setChecked(c.show_partial_en)
        form.addRow("", self.partial)
        self.en_under = QCheckBox("A magyar alatt kicsiben az angol eredeti is")
        self.en_under.setChecked(c.show_en_under_hu)
        form.addRow("", self.en_under)
        self.silence = QSpinBox()
        self.silence.setRange(250, 1500)
        self.silence.setSingleStep(50)
        self.silence.setSuffix(" ms")
        self.silence.setValue(c.end_silence_ms)
        form.addRow("Mondat vége csend:", self.silence)
        form.addRow("", _hint("Kisebb = gyorsabb felirat, de a lassan beszélők mondatai darabolódhatnak."))
        self.autostart = QCheckBox("A feliratozás induláskor magától induljon")
        self.autostart.setChecked(c.autostart)
        form.addRow("", self.autostart)
        return form

    def _page_subs(self):
        c, st = self.cfg, dict(self.cfg.style)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.on_video = QCheckBox("Felirat a képen")
        self.on_video.setChecked(c.subs_on_video)
        form.addRow("", self.on_video)
        self.font_size = QSpinBox()
        self.font_size.setRange(16, 120)
        self.font_size.setSuffix(" px (1080p-n)")
        self.font_size.setValue(int(st.get("font_size", 40)))
        form.addRow("Betűméret:", self.font_size)
        self.color = ColorButton(st.get("color", "#ffffff"))
        self.en_color = ColorButton(st.get("en_color", "#dddddd"))
        form.addRow("Szín:", _row(self.color, QLabel("angol:"), self.en_color))
        self.box = QCheckBox("Háttérdoboz a szöveg mögött")
        self.box.setChecked(bool(st.get("box")))
        self.box_op = QSpinBox()
        self.box_op.setRange(10, 100)
        self.box_op.setSuffix(" %")
        self.box_op.setValue(int(float(st.get("box_opacity", 0.5)) * 100))
        form.addRow("", _row(self.box, QLabel("átlátszatlanság:"), self.box_op))
        self.max_lines = QSpinBox()
        self.max_lines.setRange(1, 4)
        self.max_lines.setValue(c.max_lines)
        form.addRow("Egyszerre látszó mondatok:", self.max_lines)
        self.bottom = QSpinBox()
        self.bottom.setRange(0, 400)
        self.bottom.setSuffix(" px")
        self.bottom.setValue(int(st.get("bottom", 40)))
        form.addRow("Távolság a kép aljától:", self.bottom)
        self.max_width = QSpinBox()
        self.max_width.setRange(600, 1900)
        self.max_width.setSuffix(" px")
        self.max_width.setValue(int(st.get("max_width", 1400)))
        form.addRow("Legnagyobb sorszélesség:", self.max_width)
        self.obs = QCheckBox(f"OBS böngészőforrás-felirat: http://127.0.0.1:{c.http_port}/")
        self.obs.setChecked(c.obs_server)
        form.addRow("", self.obs)
        form.addRow("", _hint("Ha OBS-ben is szeretnéd a feliratot: új Böngészőforrás, ez a webcím, 1920×1080."))
        return form

    def _page_rec(self):
        c = self.cfg
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.rec_dir = QLineEdit(c.rec_dir)
        self.rec_dir.setPlaceholderText("Alapértelmezett: Videók\\rksubby")
        browse = QPushButton("Tallózás")
        browse.setIcon(icon("folder", "#c9b8ff", 16))
        browse.clicked.connect(self._browse)
        form.addRow("Mappa:", _row(self.rec_dir, browse, stretch=False))
        self.quality = QComboBox()
        for k, label in (("normal", "Normál – kisebb fájl"), ("high", "Magas – ajánlott"), ("max", "Maximális – nagy fájl")):
            self.quality.addItem(label, k)
        self.quality.setCurrentIndex(max(0, self.quality.findData(c.rec_quality)))
        form.addRow("Minőség:", self.quality)
        self.codec = QComboBox()
        self.codec.addItem("HEVC (H.265) – kisebb fájl, ajánlott", "hevc")
        self.codec.addItem("H.264 – mindenhol lejátszható", "h264")
        self.codec.setCurrentIndex(max(0, self.codec.findData(c.rec_codec)))
        form.addRow("Kódolás:", self.codec)
        self.rec_srt = QCheckBox("Felirat mentése a felvétel mellé (.srt – a VLC és az MPC magától betölti)")
        self.rec_srt.setChecked(c.rec_srt)
        form.addRow("", self.rec_srt)
        form.addRow("", _hint("A felvétel felbontása és képkockasebessége mindig a bejövő jelé (pl. 1080p120). "
                              "Kódolás: NVIDIA NVENC (videokártyán, a gépet alig terheli). A fájl áramszünet vagy "
                              "összeomlás után is lejátszható. Feliratos (beégetett) változat: Felvételek › Export."))
        form.addRow("", _hint("<b>Gyorsbillentyűk:</b> F9 felvétel · F10 szünet · F8 felirat a képen · F11 teljes "
                              "képernyő · F12 pillanatkép · F5 jel újrafelismerése"))
        return form

    def _page_av(self, devices):
        c = self.cfg
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.dev = QComboBox()
        self.dev.addItem("Automatikus (capture kártya keresése)", "")
        for d in devices:
            self.dev.addItem(d, d)
        self.dev.setCurrentIndex(max(0, self.dev.findData(c.dshow_device)))
        form.addRow("Capture kártya:", self.dev)
        form.addRow("", _hint("A kép és a hang is a kártyáról jön, egyetlen csatornán (DirectShow). A kártyát egyszerre "
                              "csak egy program használhatja: a RECentral és az OBS legyen bezárva."))
        self.monitor = QCheckBox("A játék hangja szóljon ezen a gépen is")
        self.monitor.setChecked(c.monitor_audio)
        form.addRow("", self.monitor)
        return form

    def _page_about(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(_hint(
            f"<b style='font-size:12pt;color:#eef0ff'>rksubby V{__version__}</b><br>"
            "Xbox (vagy bármilyen HDMI-forrás) képe 1080p120-ig, felvétel NVENC-cel, élő magyar felirat angol "
            "beszédhez.<br><br>"
            "Felhasznált projektek: faster-whisper (MIT), Whisper / distil-whisper modellek, Silero VAD (MIT), "
            "FFmpeg és PyAV (LGPL), SDL2 (zlib), Qt for Python / PySide6 (LGPL), DeepL API.<br><br>"
            "Forráskód és kiadások: <a style='color:#c9a8ff' href='https://github.com/rknoise/rksubbyV2'>"
            "github.com/rknoise/rksubbyV2</a>"))
        open_logs = QPushButton("Naplómappa megnyitása")
        open_logs.setIcon(icon("folder", "#c9b8ff", 16))
        open_logs.clicked.connect(lambda: os.startfile(sc.HOME_DIR))
        lay.addWidget(_row(open_logs))
        return w

    # ------------------------------------------------------------------ actions
    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Felvételek mappája", self.rec_dir.text() or os.path.expanduser("~"))
        if d:
            self.rec_dir.setText(os.path.normpath(d))

    def _async(self, tag, fn):
        def run():
            try:
                res = fn()
            except Exception as e:
                res = e
            self._bridge.result.emit(tag, res)

        threading.Thread(target=run, daemon=True).start()

    def _check_deepl(self):
        key = self.key_edit.text().strip()
        if not key:
            self.deepl_status.setText("<span style='color:#ffc53d'>Nincs megadva kulcs.</span>")
            return
        self.deepl_status.setText("Ellenőrzés…")
        from subby.translate import deepl_usage

        self._async("deepl", lambda: deepl_usage(key, timeout=6.0))

    def _check_ollama(self):
        self.ollama_status.setText("Ellenőrzés…")
        from subby.translate import ollama_status

        url, model = self.ollama_url.text().strip(), self.ollama_model.currentText().strip()
        self._async("ollama", lambda: ollama_status(url, model))

    def _on_result(self, tag, res):
        if tag == "deepl":
            if isinstance(res, Exception):
                self.deepl_status.setText(f"<span style='color:#ff5c7a'>Hiba: {short_err(res)}</span>")
            else:
                used, limit = res
                self.deepl_status.setText(f"<span style='color:#38d27a'>A kulcs működik.</span> Havi keret: "
                                          f"{fmt_int(used)} / {fmt_int(limit)} karakter felhasználva.")
        elif tag == "ollama":
            if isinstance(res, Exception):
                self.ollama_status.setText(f"<span style='color:#ff5c7a'>Hiba: {short_err(res)}</span>")
                return
            running, model = res
            if model:
                self.ollama_status.setText(f"<span style='color:#38d27a'>Fut, a fordítómodell: {model}</span>")
            elif running:
                self.ollama_status.setText("<span style='color:#ffc53d'>Fut, de nincs letöltve fordítómodell "
                                           "(ollama pull translategemma:12b).</span>")
            else:
                self.ollama_status.setText("<span style='color:#ff5c7a'>Az Ollama nem fut, vagy nincs telepítve."
                                           "</span>")

    # ------------------------------------------------------------------ result
    def changes(self) -> dict:
        tr = next(k for k, rb in self.tr_radios.items() if rb.isChecked())
        style = dict(self.cfg.style)
        style.update(font_size=self.font_size.value(), color=self.color.color, en_color=self.en_color.color,
                     box=self.box.isChecked(), box_opacity=self.box_op.value() / 100.0, bottom=self.bottom.value(),
                     max_width=self.max_width.value())
        return {
            "translator": tr, "translate_fallback": self.fallback.isChecked(),
            "deepl_formality": self.formality.currentData(), "deepl_glossary": self.glossary.isChecked(),
            "spec_translate": self.spec.isChecked(), "ollama_url": self.ollama_url.text().strip(),
            "ollama_model": self.ollama_model.currentText().strip(),
            "model": self.model.currentData(), "asr_device": self.device.currentData(),
            "profile": self.profile.currentData() or self.cfg.profile, "show_partial_en": self.partial.isChecked(),
            "show_en_under_hu": self.en_under.isChecked(), "end_silence_ms": self.silence.value(),
            "autostart": self.autostart.isChecked(),
            "subs_on_video": self.on_video.isChecked(), "style": style, "max_lines": self.max_lines.value(),
            "obs_server": self.obs.isChecked(),
            "rec_dir": self.rec_dir.text().strip(), "rec_quality": self.quality.currentData(),
            "rec_codec": self.codec.currentData(), "rec_srt": self.rec_srt.isChecked(),
            "dshow_device": self.dev.currentData(), "monitor_audio": self.monitor.isChecked(),
        }

    def new_key(self):
        return self.key_edit.text().strip() if self.key_changed else None
