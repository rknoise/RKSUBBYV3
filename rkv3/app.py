# -*- coding: utf-8 -*-
"""rksubby V3 – Xbox képe 1080p120-ig, felvétel és élő magyar felirat egy ablakban.

Felépítés (egy folyamat):
  capture kártya (DirectShow, PyAV) ─┬─ kép  ─> VideoWidget (SDL2/D3D11, GPU) ─> felirat a képen
                                     ├─ kép+hang ─> Recorder (NVENC) ─> .mp4 (+ .srt)
                                     └─ hang ─┬─> hangszóró (SDL)
                                              └─> Engine: Silero VAD -> faster-whisper -> DeepL/Lokális AI/Google
"""
from __future__ import annotations

import ctypes
import glob
import logging
import logging.handlers
import os
import sys
import threading
import time
from ctypes import wintypes

import numpy as np

from subby import config as sc

APP = "rksubby"
LOG_PATH = os.path.join(sc.HOME_DIR, "rksubby.log")
os.makedirs(sc.HOME_DIR, exist_ok=True)
_h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_h)
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("rksubby")

import sdl2  # noqa: E402
from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal  # noqa: E402
from PySide6.QtGui import (QAction, QActionGroup, QDesktopServices, QGuiApplication, QIcon, QImage,  # noqa: E402
                           QKeySequence, QPixmap, QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,  # noqa: E402
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton, QSlider,
                               QToolButton, QVBoxLayout, QWidget)

from subby.display import SubtitleSink, display_seconds  # noqa: E402
from subby.engine import Engine  # noqa: E402

from . import __version__  # noqa: E402
from . import capture  # noqa: E402
from . import desktop  # noqa: E402
from .icons import icon  # noqa: E402
from .theme import QSS  # noqa: E402
from .video import ScreenOverlay, VideoWidget, render_subtitle  # noqa: E402

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
INSTANCE_KEY = "rksubby-v3-instance"


# ====================================================================== helpers
def recycle(path: str) -> bool:
    """Lomtárba helyezés (SHFileOperationW + FOF_ALLOWUNDO)."""

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    op = SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None, 0x40 | 0x10 | 0x4 | 0x400, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path)


def rec_folder(cfg):
    return cfg.rec_dir or os.path.join(os.path.dirname(capture.videos_dir()), "rksubby")


def fmt_size(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def srt_time(s):
    ms = int(round(max(0.0, s) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


class RecSrt:
    """A felvétel mellé írt felirat, a felvétel idővonalán (a szünetek kihagyva)."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "w", encoding="utf-8-sig", buffering=1)
        self.n = 0
        self.last_start = -1.0
        self.lock = threading.Lock()

    def add(self, start, end, text):
        with self.lock:
            if self.f is None:
                return
            start = max(start, self.last_start + 0.05, 0.0)
            end = max(end, start + 0.8)
            self.last_start = start
            self.n += 1
            self.f.write(f"{self.n}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None
        if self.n == 0:
            try:
                os.remove(self.path)
            except OSError:
                pass


class Sink(SubtitleSink):
    def __init__(self, cfg, on_cue):
        super().__init__(cfg, echo=lambda m: log.info(m.strip().replace("\n", " | ")))
        self.on_cue = on_cue

    def final(self, hu, en, asr_ms, tr_ms, latency_ms):
        super().final(hu, en, asr_ms, tr_ms, latency_ms)
        try:
            self.on_cue(hu, en, latency_ms)
        except Exception:
            log.exception("on_cue")


class Tap:
    """A rögzítő hangja a feliratozónak (az Engine hangforrásként használja)."""

    def __init__(self, win, on_audio):
        self.win, self.on_audio = win, on_audio
        self.ok, self.error = True, ""

    def start(self):
        self.win.taps.append(self.on_audio)
        return "a forrás hangja (capture kártya vagy a Windows rendszerhangja), közvetlenül a rögzítőből"

    def stop(self):
        try:
            self.win.taps.remove(self.on_audio)
        except ValueError:
            pass


class Bus(QObject):
    recordings_changed = Signal()
    export_progress = Signal(float)
    export_done = Signal(str)
    notify = Signal(str)
    source_changed = Signal()
    update_available = Signal(str, str)
    devices_ready = Signal(list)


def card(title):
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    head = QHBoxLayout()
    t = QLabel(title)
    t.setObjectName("CardTitle")
    head.addWidget(t)
    head.addStretch(1)
    lay.addLayout(head)
    return f, lay, head


def tool(name, tip, checkable=False, size=20, color="#dfe3ff"):
    b = QToolButton()
    b.setIcon(icon(name, color, size))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    return b


# ====================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"rksubby V3")
        self.setWindowIcon(QIcon(os.path.join(ASSETS, "rksubby.ico")))
        self.resize(1560, 900)
        self.cfg = sc.Config()
        if self.cfg.load_error:
            log.warning(self.cfg.load_error)
        self.bus = Bus()
        self.bus.recordings_changed.connect(self._refresh_recordings)
        self.bus.notify.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.taps: list = []
        self.rec = capture.Recorder()
        self.rec_srt: RecSrt | None = None
        self.sink = Sink(self.cfg, self._on_cue)
        self.engine = Engine(self.cfg, self.sink, log=self._engine_log)
        self.engine.monitor_enabled = False
        self.engine.source_factory = lambda on_audio: Tap(self, on_audio)
        self.engine_last = ""
        self.engine_autostarted = False
        self.cap: capture.Capture | None = None
        self.fmt = None
        self.dev = None
        self.caps = {}
        self.state = "search"
        self.src = ""                      # "card" | "desktop"
        self.cards: list[str] = []        # kép+hang DirectShow-eszközök (a forrásmenühöz)
        self.outputs: list[dict] = []     # rögzíthető monitorok
        self._probe_t = time.time()
        self._probing = False
        self._busy = threading.Lock()
        self._stalled = False
        self._slow = 0
        self._last_try = 0.0
        self._sub_version = -1
        self._sub_key = None
        self._hist_n = -1
        self.last_frame = None
        self._audio_dev = 0
        self._audio_fmt = None
        self.volume = float(self.cfg.monitor_volume)
        self.muted = not self.cfg.monitor_audio
        self.fullscreen = False
        self.server = None
        self.logo_img = QImage(os.path.join(ASSETS, "logo512.png"))
        capture.set_nosignal_ref(os.path.join(ASSETS, "nosignal_sig.npy"))
        sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_AUDIO)
        self.overlay = ScreenOverlay()
        self.bus.source_changed.connect(self._apply_source)
        self.bus.update_available.connect(self._show_update)
        self.bus.devices_ready.connect(lambda c: setattr(self, "cards", c))
        self._refresh_outputs()
        self._build_ui()
        self._shortcuts()
        self._refresh_recordings()
        self._start_server()
        self.t_house = QTimer(self, timeout=self._housekeeping)
        self.t_house.start(1000)
        self.t_ui = QTimer(self, timeout=self._ui_tick)
        self.t_ui.start(250)
        self.t_subs = QTimer(self, timeout=self._subs_tick)
        self.t_subs.start(40)
        QTimer.singleShot(400, self.restart_pipeline)
        threading.Thread(target=self._scan_cards, daemon=True).start()
        if self.cfg.update_check:
            QTimer.singleShot(5000, lambda: threading.Thread(target=self._check_update, daemon=True).start())

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 8)
        outer.setSpacing(14)
        main = QVBoxLayout()
        main.setSpacing(10)
        outer.addLayout(main, 1)

        # header
        self.header = QFrame()
        self.header.setObjectName("Header")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(2, 0, 2, 0)
        hl.setSpacing(8)
        logo = QLabel()
        pm = QPixmap(os.path.join(ASSETS, "wordmark.png"))
        logo.setPixmap(pm.scaledToHeight(34, Qt.SmoothTransformation))
        hl.addWidget(logo)
        hl.addSpacing(14)
        self.src_btn = QToolButton()
        self.src_btn.setObjectName("SrcBtn")
        self.src_btn.setPopupMode(QToolButton.InstantPopup)
        self.src_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.src_btn.setCursor(Qt.PointingHandCursor)
        self.src_btn.setToolTip("Forrás: capture kártya vagy Windows asztal (PC-s játék, program)")
        self.src_menu = QMenu(self)
        self.src_menu.aboutToShow.connect(self._build_source_menu)
        self.src_btn.setMenu(self.src_menu)
        hl.addWidget(self.src_btn)
        self.chip_sig = QLabel("Kártya keresése…")
        self.chip_sub = QLabel("Felirat: ki")
        self.chip_rec = QLabel("● REC")
        for c in (self.chip_sig, self.chip_sub, self.chip_rec):
            c.setObjectName("Chip")
            hl.addWidget(c)
        self.chip_rec.hide()
        self.btn_update = QPushButton()
        self.btn_update.setObjectName("UpdateBtn")
        self.btn_update.hide()
        hl.addWidget(self.btn_update)
        hl.addStretch(1)
        self.btn_panel = tool("sidebar", "Oldalsó panel (be/ki)", checkable=True)
        self.btn_panel.setChecked(self.cfg.side_panel)
        self.btn_panel.toggled.connect(self._toggle_panel)
        self.btn_settings = tool("settings", "Beállítások", size=22)
        self.btn_settings.clicked.connect(lambda: self.open_settings(0))
        hl.addWidget(self.btn_panel)
        hl.addWidget(self.btn_settings)
        main.addWidget(self.header)

        # video
        self.vframe = QFrame()
        self.vframe.setObjectName("VideoFrame")
        vl = QVBoxLayout(self.vframe)
        vl.setContentsMargins(1, 1, 1, 1)
        self.video = VideoWidget()
        self.video.double_clicked.connect(self.toggle_fullscreen)
        vl.addWidget(self.video)
        main.addWidget(self.vframe, 1)

        # control bar
        self.cbar = QFrame()
        self.cbar.setObjectName("ControlBar")
        cl = QHBoxLayout(self.cbar)
        cl.setContentsMargins(12, 8, 12, 8)
        cl.setSpacing(10)
        self.btn_rec = QPushButton("  Felvétel")
        self.btn_rec.setObjectName("RecBtn")
        self.btn_rec.setIcon(icon("record", "#ff4d6d", 18))
        self.btn_rec.setCheckable(True)
        self.btn_rec.setToolTip("Felvétel indítása / leállítása (F9)")
        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_pause = QPushButton("  Szünet")
        self.btn_pause.setIcon(icon("pause", "#c9b8ff", 16))
        self.btn_pause.setCheckable(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip("Felvétel szüneteltetése (F10)")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.lbl_timer = QLabel("00:00:00")
        self.lbl_timer.setObjectName("Timer")
        self.lbl_size = QLabel("")
        self.lbl_size.setObjectName("Size")
        for w in (self.btn_rec, self.btn_pause, self.lbl_timer, self.lbl_size):
            cl.addWidget(w)
        cl.addWidget(self._sep())
        self.btn_cc = QPushButton("  Felirat a képen")
        self.btn_cc.setObjectName("Toggle")
        self.btn_cc.setIcon(icon("cc", "#dfe3ff", 18))
        self.btn_cc.setCheckable(True)
        self.btn_cc.setChecked(self.cfg.subs_on_video)
        self.btn_cc.setToolTip("Felirat megjelenítése a képen (F8)")
        self.btn_cc.toggled.connect(self._toggle_cc)
        cl.addWidget(self.btn_cc)
        self.btn_snap = tool("camera", "Pillanatkép (F12)")
        self.btn_snap.clicked.connect(self.snapshot)
        cl.addWidget(self.btn_snap)
        cl.addStretch(1)
        self.btn_mute = tool("mute" if self.muted else "volume", "Hang be/ki")
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setValue(int(self.volume * 100))
        self.vol.setFixedWidth(130)
        self.vol.valueChanged.connect(self._volume_changed)
        cl.addWidget(self.btn_mute)
        cl.addWidget(self.vol)
        cl.addWidget(self._sep())
        self.lbl_fps = QLabel("")
        self.lbl_fps.setObjectName("Small")
        cl.addWidget(self.lbl_fps)
        self.btn_full = tool("fullscreen", "Teljes képernyő (F11, dupla kattintás)")
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        cl.addWidget(self.btn_full)
        main.addWidget(self.cbar)

        # side panel
        self.side = QFrame()
        self.side.setObjectName("Side")
        self.side.setFixedWidth(372)
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(12)
        outer.addWidget(self.side)
        self.side.setVisible(self.cfg.side_panel)

        c1, l1, h1 = card("Élő felirat")
        self.sub_state = QLabel("●")
        self.sub_state.setStyleSheet("color:#5a6290; font-size: 13pt;")
        h1.addWidget(self.sub_state)
        self.sub_status = QLabel("A feliratozó nem fut.")
        self.sub_status.setObjectName("Muted")
        self.sub_status.setWordWrap(True)
        l1.addWidget(self.sub_status)
        lvl_row = QHBoxLayout()
        lv = QLabel("Hang")
        lv.setObjectName("Small")
        self.level = QProgressBar()
        self.level.setObjectName("Level")
        self.level.setRange(0, 100)
        self.level.setFixedHeight(8)
        lvl_row.addWidget(lv)
        lvl_row.addWidget(self.level, 1)
        l1.addLayout(lvl_row)
        self.sub_tr = QLabel("")
        self.sub_tr.setObjectName("Small")
        self.sub_tr.setWordWrap(True)
        l1.addWidget(self.sub_tr)
        self.deepl_bar = QProgressBar()
        self.deepl_bar.setRange(0, 1000)
        self.deepl_bar.setFixedHeight(6)
        self.deepl_bar.hide()
        l1.addWidget(self.deepl_bar)
        br = QHBoxLayout()
        self.btn_engine = QPushButton("Feliratozás indítása")
        self.btn_engine.setObjectName("Primary")
        self.btn_engine.clicked.connect(self.toggle_engine)
        btr = QPushButton("Fordító")
        btr.setIcon(icon("translate", "#c9b8ff", 16))
        btr.clicked.connect(lambda: self.open_settings(0))
        btest = QPushButton("Próba")
        btest.setIcon(icon("play", "#c9b8ff", 14))
        btest.setToolTip("Próba a beépített angol mintafelvétellel (Xbox nélkül is)")
        btest.clicked.connect(self.engine_test)
        br.addWidget(self.btn_engine, 1)
        br.addWidget(btest)
        br.addWidget(btr)
        l1.addLayout(br)
        sl.addWidget(c1)

        c2, l2, _ = card("Utolsó mondatok")
        self.hist = QListWidget()
        self.hist.setWordWrap(True)
        self.hist.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        l2.addWidget(self.hist, 1)
        sl.addWidget(c2, 1)

        c3, l3, h3 = card("Felvételek")
        bopen = tool("folder", "Mappa megnyitása", size=17)
        bopen.clicked.connect(lambda: (os.makedirs(rec_folder(self.cfg), exist_ok=True),
                                       os.startfile(rec_folder(self.cfg))))
        h3.addWidget(bopen)
        self.recs = QListWidget()
        self.recs.setFixedHeight(150)
        self.recs.itemDoubleClicked.connect(lambda it: os.startfile(it.data(Qt.UserRole)))
        l3.addWidget(self.recs)
        rr = QHBoxLayout()
        bplay = QPushButton("Lejátszás")
        bplay.setIcon(icon("play", "#c9b8ff", 14))
        bplay.clicked.connect(self._play_selected)
        bexp = QPushButton("Export felirattal")
        bexp.setIcon(icon("export", "#c9b8ff", 14))
        bexp.setToolTip("A felvétel új változata, a felirat beégetve a képbe (NVENC)")
        bexp.clicked.connect(self._export_selected)
        bdel = tool("delete", "Lomtárba", size=17, color="#ff7a95")
        bdel.clicked.connect(self._delete_selected)
        rr.addWidget(bplay)
        rr.addWidget(bexp, 1)
        rr.addWidget(bdel)
        l3.addLayout(rr)
        sl.addWidget(c3)

        sb = self.statusBar()
        self.lbl_right = QLabel("")
        sb.addPermanentWidget(self.lbl_right)
        sb.showMessage(f"rksubby V{__version__} – napló: {LOG_PATH}", 6000)

    def _sep(self):
        s = QFrame()
        s.setObjectName("Sep")
        s.setFixedHeight(26)
        return s

    def _shortcuts(self):
        for key, fn in (("F9", self.toggle_record), ("F10", self.toggle_pause), ("F8", lambda: self.btn_cc.toggle()),
                        ("F11", self.toggle_fullscreen), ("F12", self.snapshot), ("F5", self.restart_pipeline),
                        ("Escape", lambda: self.fullscreen and self.toggle_fullscreen())):
            sc_ = QShortcut(QKeySequence(key), self)
            sc_.setContext(Qt.ApplicationShortcut)
            sc_.activated.connect(fn)

    # ------------------------------------------------------------------ capture pipeline
    def restart_pipeline(self):
        if self.rec.active:
            return
        if not self._busy.acquire(blocking=False):
            return
        threading.Thread(target=self._pipeline, name="pipeline", daemon=True).start()

    def _pipeline(self):
        try:
            self._last_try = time.time()
            if self.cap:
                self.cap.stop()
                self.cap = None
            src = self.cfg.source
            if src in ("auto", "card"):
                if self._start_card(fallback=src == "auto") or src == "card":
                    return
            self._start_desktop()
        except Exception:
            log.exception("pipeline")
        finally:
            self._busy.release()
            self.bus.source_changed.emit()

    def _start_card(self, fallback=False) -> bool:
        """Capture kártya. fallback=True (automatikus mód): jel nélkül nem indul, az asztal jön helyette."""
        self.state = "search"
        if not self.dev:
            self.dev = capture.find_device(self.cfg.dshow_device)
            if not self.dev:
                self.state = "nodev"
                return False
            self.caps = capture.list_caps(self.dev)
            log.info("device %s caps %s", self.dev, self.caps)
        self.state = "detect"
        try:
            fmt = capture.detect_format(self.dev, self.caps)
        except Exception as e:
            log.warning("detect failed: %s", e)
            self.state = "busy"
            return False
        if fmt.get("nosignal") and fallback:
            log.info("card has no signal -> desktop (auto)")
            self.state = "nosignal"
            return False
        fmt.update(src="card", pix="nv12", name=self.dev)
        self.fmt = fmt
        self._stalled = False
        cap = capture.Capture(self.dev, fmt, self._on_video, self._on_audio, self.rec)
        cap.start()
        self.cap, self.src = cap, "card"
        self.state = "nosignal" if fmt.get("nosignal") else "run"
        return True

    def _start_desktop(self):
        outs = self.outputs or desktop.list_outputs()
        if not outs:
            self.state = "nodesk"
            return
        o = next((x for x in outs if x["idx"] == self.cfg.desktop_output), None)
        if o is None:
            o = next((x for x in outs if x.get("primary")), outs[0])
        fps = capture.snap_rate(min(int(self.cfg.desktop_fps), max(30, int(o.get("hz", 60)))))
        fmt = {"w": o["w"], "h": o["h"], "fps": fps, "src": "desktop", "pix": "bgra", "name": o["name"],
               "output": o["idx"], "nosignal": False}
        cap = desktop.DesktopCapture(o, fmt, self._on_video, self._on_audio, self.rec)
        cap.start()
        self.fmt, self.cap, self.src = fmt, cap, "desktop"
        self._stalled = False
        self.state = "run"
        log.info("source: desktop %s %dx%d@%d (audio: %s)", o["name"], o["w"], o["h"], fps,
                 cap.audio_desc or cap.audio_error)

    def _refresh_outputs(self):
        try:
            self.outputs = desktop.list_outputs(QGuiApplication.screens())
        except Exception as e:
            log.warning("desktop outputs: %s", e)
            self.outputs = []

    def _scan_cards(self):
        try:
            cards = [d["name"] for d in capture.list_devices() if {"video", "audio"} <= d["types"]]
        except Exception:
            cards = []
        self.bus.devices_ready.emit(cards)

    def _apply_source(self):
        """GUI-szál: asztali módban a saját ablak kimarad a felvételből, a felirat a képernyő aljára kerül."""
        dsk = self.src == "desktop"
        desktop.exclude_from_capture(int(self.winId()), dsk)
        self._sub_key = None
        if dsk and self.fmt:
            o = next((x for x in self.outputs if x["idx"] == self.fmt.get("output")), None)
            scr = (o or {}).get("qt") or QGuiApplication.primaryScreen()
            self.overlay.place(scr, int(self.cfg.style.get("bottom", 40) * self.fmt["h"] / 1080))
            self.overlay.show()
            desktop.exclude_from_capture(int(self.overlay.winId()), not self.cfg.desktop_subs_in_rec)
        else:
            self.overlay.hide()

    def _build_source_menu(self):
        m = self.src_menu
        m.clear()
        grp = QActionGroup(m)
        cur = self.cfg.source

        def add(text, ic, checked, changes):
            a = QAction(icon(ic, "#c9b8ff", 16), text, m)
            a.setCheckable(True)
            a.setChecked(checked)
            a.triggered.connect(lambda _=False, c=changes: self.set_source(c))
            grp.addAction(a)
            m.addAction(a)

        add("Automatikus – kártya, ha van jele; különben a Windows asztal", "refresh", cur == "auto",
            {"source": "auto"})
        m.addSection("Capture kártya")
        cards = self.cards or ([self.dev] if self.dev else [])
        if not cards:
            na = m.addAction("(nem találok capture kártyát)")
            na.setEnabled(False)
        for c in cards:
            add(c, "video", cur == "card" and self.cfg.dshow_device in ("", c),
                {"source": "card", "dshow_device": c})
        m.addSection("Windows asztal (PC-s játék, program, videó)")
        self._refresh_outputs()
        for o in self.outputs:
            add(f"{o['name']}  –  {o['w']}×{o['h']}, {o['hz']} Hz{'  (fő)' if o.get('primary') else ''}", "desktop",
                cur == "desktop" and self.cfg.desktop_output == o["idx"],
                {"source": "desktop", "desktop_output": o["idx"]})

    def set_source(self, changes):
        if self.rec.active:
            QMessageBox.information(self, "Forrás", "Felvétel közben nem lehet forrást váltani. Előbb állítsd le.")
            return
        self.cfg.update(changes)
        self._save_cfg()
        self.dev = None
        self.restart_pipeline()

    def _probe_card(self):
        """Automatikus mód, asztalról: megjött-e az Xbox jele a kártyán? (pár mp-re megnyitja a kártyát)"""
        try:
            dev = capture.find_device(self.cfg.dshow_device)
            if not dev:
                return
            y, nos = capture.grab_y(dev, 1920, 1080, 60)
            if not nos and y is not None and capture.detail_ratio(y) is not None:
                log.info("auto: capture card has signal -> switching to card")
                self.dev = None
                if not self.rec.active and self.cfg.source == "auto" and self.src == "desktop":
                    self.restart_pipeline()
        except Exception as e:
            log.info("card probe: %s", e)
        finally:
            self._probing = False

    def _check_update(self):
        try:
            import requests

            r = requests.get("https://api.github.com/repos/rknoise/rksubbyV2/releases/latest", timeout=6,
                             headers={"Accept": "application/vnd.github+json"})
            if r.status_code != 200:
                return
            d = r.json()
            tag = (d.get("tag_name") or "").lstrip("vV")

            def ver(x):
                parts = [p for p in x.split("-")[0].split(".") if p.isdigit()]
                return tuple(int(p) for p in (parts + ["0", "0", "0"])[:3])
            if tag and ver(tag) > ver(__version__):
                self.bus.update_available.emit(tag, d.get("html_url") or "")
        except Exception as e:
            log.info("update check: %s", e)

    def _show_update(self, tag, url):
        self.btn_update.setText(f"  Új verzió: v{tag}")
        self.btn_update.setIcon(icon("export", "#ffffff", 16))
        self.btn_update.setToolTip("Megnyitja a letöltési oldalt (GitHub)")
        self.btn_update.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        self.btn_update.show()

    def _on_video(self, frame):
        self.last_frame = frame
        self.video.push_frame(frame)

    def _on_audio(self, frame):
        try:
            ch = frame.layout.nb_channels
            n = frame.samples * ch
            data = np.frombuffer(frame.planes[0], np.int16, count=n)
            if self.taps:
                x = data.reshape(-1, ch).astype(np.float32) * (1 / 32768.0)
                t = time.monotonic()
                for fn in list(self.taps):
                    fn(x, frame.sample_rate, t)
            if self.src == "desktop":       # a PC hangja már szól – visszajátszani visszhang lenne
                return
            fmt = (frame.sample_rate, ch)
            if fmt != self._audio_fmt:
                if self._audio_dev:
                    sdl2.SDL_CloseAudioDevice(self._audio_dev)
                    self._audio_dev = 0
                self._audio_fmt = fmt
                want = sdl2.SDL_AudioSpec(fmt[0], sdl2.AUDIO_S16LSB, ch, 256)
                have = sdl2.SDL_AudioSpec(0, 0, 0, 0)
                self._audio_dev = sdl2.SDL_OpenAudioDevice(None, 0, ctypes.byref(want), ctypes.byref(have), 0)
                sdl2.SDL_PauseAudioDevice(self._audio_dev, 0)
            if not self._audio_dev or self.muted or self.volume <= 0.001:
                return
            if sdl2.SDL_GetQueuedAudioSize(self._audio_dev) > fmt[0] * ch * 2 // 10:   # < 100 ms
                sdl2.SDL_ClearQueuedAudio(self._audio_dev)
            if abs(self.volume - 1.0) > 0.01:
                buf = np.clip(data.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16).tobytes()
                sdl2.SDL_QueueAudio(self._audio_dev, buf, len(buf))
            else:
                sdl2.SDL_QueueAudio(self._audio_dev, ctypes.c_void_p(frame.planes[0].buffer_ptr), n * 2)
        except Exception:
            log.exception("audio")

    def _housekeeping(self):
        self._hk_n = getattr(self, "_hk_n", 0) + 1
        if self._hk_n % 10 == 0:
            log.info("stats state=%s fmt=%s capture_in=%.1f preview=%.1f rec=%s dropped=%s engine=%s",
                     self.state, self.fmt and (self.fmt["w"], self.fmt["h"], self.fmt["fps"]),
                     self.cap.fps_in if self.cap else 0, self.video.fps, self.rec.active, self.rec.dropped,
                     self.engine.state)
        cap = self.cap
        if cap is None:
            if not self._busy.locked() and time.time() - self._last_try > 4:
                if self.state in ("nodev",):
                    self.dev = None
                self.restart_pipeline()
        elif cap.error and not self._busy.locked():
            log.warning("capture error: %s", cap.error)
            self.cap = None
            self.state = "busy"
            self.restart_pipeline()
        elif self.src == "desktop":
            if (self.cfg.source == "auto" and not self.rec.active and not self._probing
                    and time.time() - self._probe_t > 20):
                self._probe_t = time.time()
                self._probing = True
                threading.Thread(target=self._probe_card, name="card-probe", daemon=True).start()
        else:
            stalled = time.perf_counter() - cap.last_frame_wall > 1.5
            if self._stalled and not stalled and not self.rec.active:
                self._stalled = False
                log.info("frames resumed -> re-detect")
                self.restart_pipeline()
                return
            self._stalled = stalled
            fps = self.fmt["fps"] if self.fmt else 0
            if fps and not stalled and not cap.nosignal and cap.fps_in and cap.fps_in < fps * 0.9 and not self.rec.active:
                self._slow += 1
                if self._slow >= 3:
                    log.info("signal rate %.1f < opened %d -> re-detect", cap.fps_in, fps)
                    self._slow = 0
                    self.restart_pipeline()
                    return
            else:
                self._slow = 0
            if cap.signal_event.is_set() and not self.rec.active:
                cap.signal_event.clear()
                self.restart_pipeline()
                return
            if self.state in ("run", "nosignal"):
                self.state = "nosignal" if (cap.nosignal or stalled) else "run"
        # autostart feliratozó
        if (self.state == "run" and self.cfg.autostart and not self.engine_autostarted
                and self.engine.state == "idle"):
            self.engine_autostarted = True
            self.engine.start("live")
        self._update_video_message()
        if self.rec.error:
            err, self.rec.error = self.rec.error, None
            rs, self.rec_srt = self.rec_srt, None
            if rs:
                rs.close()
            QMessageBox.warning(self, "Felvétel", err)

    def _update_video_message(self):
        msgs = {
            "search": ("Capture kártya keresése…", ""),
            "detect": ("Jel felismerése…", "Felbontás és képkockasebesség mérése"),
            "nodev": ("Nem található capture kártya",
                      "Telepítsd a kártya illesztőprogramját (pl. AVerMedia GC573), vagy válaszd ki a kártyát a "
                      "Beállítások › Kép és hang oldalon."),
            "busy": ("A kártya nem nyitható meg",
                     "Valószínűleg más program használja (RECentral, OBS). Zárd be – a program magától újrapróbálja."),
            "nosignal": ("Nincs jel", "Kapcsold be az Xboxot, vagy ellenőrizd a HDMI-kábelt."),
            "nodesk": ("Nincs rögzíthető monitor", "A Windows asztal rögzítése ezen a gépen nem érhető el."),
        }
        m = msgs.get(self.state)
        if m and (not self.video.has_video() or self.state in ("nodev", "busy", "search", "detect")):
            self.video.set_message(m[0], m[1], self.logo_img)

    # ------------------------------------------------------------------ periodic UI
    def _ui_tick(self):
        f = self.fmt
        auto = " (auto)" if self.cfg.source == "auto" else ""
        if self.src == "desktop" and f:
            self.src_btn.setIcon(icon("desktop", "#c9b8ff", 16))
            self.src_btn.setText(f"  Asztal: {f.get('name', '')}{auto}  ")
        else:
            self.src_btn.setIcon(icon("video", "#c9b8ff", 16))
            self.src_btn.setText(f"  Kártya{auto}  ")
        # chips
        if self.state == "run" and f:
            self._chip(self.chip_sig, f"● {f['h']}p · {f['fps']} fps", "ok")
        else:
            txt = {"search": "Kártya keresése…", "detect": "Jel felismerése…", "nodev": "Nincs kártya",
                   "busy": "Kártya foglalt", "nosignal": "Nincs jel"}.get(self.state, self.state)
            self._chip(self.chip_sig, txt, "err" if self.state in ("nodev", "busy") else "warn")
        es = self.engine.state
        st = self.engine.status()
        chain = (st.get("translator") or {}).get("chain") or []
        trname = chain[0] if chain else ""
        if es == "running":
            self._chip(self.chip_sub, f"Felirat: {trname or 'angol'}", "ok")
        elif es == "loading":
            self._chip(self.chip_sub, "Felirat: indul…", "warn")
        elif es == "error":
            self._chip(self.chip_sub, "Felirat: hiba", "err")
        else:
            self._chip(self.chip_sub, "Felirat: ki", "")
        # recording
        rec = self.rec
        if rec.active:
            el = int(rec.elapsed)
            self.lbl_timer.setText(f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}")
            try:
                self.lbl_size.setText(fmt_size(os.path.getsize(rec.path)))
            except OSError:
                pass
            self.chip_rec.setVisible(True)
            self._chip(self.chip_rec, "❚❚ SZÜNET" if rec.paused else "● REC", "rec")
        else:
            self.chip_rec.setVisible(False)
        if self.btn_rec.isChecked() != rec.active:
            self.btn_rec.setChecked(rec.active)
        self.btn_rec.setText("  Leállítás" if rec.active else "  Felvétel")
        self.btn_pause.setEnabled(rec.active)
        self.btn_pause.setChecked(rec.active and rec.paused)
        # fps
        cap = self.cap
        self.lbl_fps.setText(f"{self.video.fps:.0f} fps" if self.video.has_video() else "")
        self.lbl_right.setText(
            (f"Kártya: {cap.fps_in:.0f} fps · előnézet: {self.video.fps:.0f} fps · " if cap else "")
            + (f"kódoló: {getattr(rec, 'encoder_name', '')} · " if rec.active else "")
            + (self.dev or ""))
        # engine card
        self._update_engine_card(st)
        title = "rksubby V3"
        if f and self.state == "run":
            title += f" – {f['w']}×{f['h']} @ {f['fps']} fps"
        if rec.active:
            title += "  ● REC"
        self.setWindowTitle(title)

    def _chip(self, lb, text, state):
        if lb.text() != text:
            lb.setText(text)
        if lb.property("state") != state:
            lb.setProperty("state", state)
            lb.style().unpolish(lb)
            lb.style().polish(lb)

    def _update_engine_card(self, st):
        es = st["state"]
        colors = {"running": "#38d27a", "loading": "#ffc53d", "error": "#ff3b5c", "stopping": "#ffc53d"}
        self.sub_state.setStyleSheet(f"color:{colors.get(es, '#5a6290')}; font-size: 13pt;")
        self.btn_engine.setText("Feliratozás leállítása" if es in ("running", "loading") else "Feliratozás indítása")
        if es == "running":
            a = st.get("audio") or {}
            msg = a.get("msg") or "Fut."
            if a.get("stalled"):
                msg = "Nem jön hang a kártyáról."
            info = st.get("info", {})
            self.sub_status.setText(f"{msg}\nModell: {info.get('model', '?')} · profil: {info.get('profile', '?')}")
            db = a.get("rms_db", -120)
            self.level.setValue(int(max(0, min(100, (db + 60) / 60 * 100))))
        elif es == "loading":
            steps = st.get("steps") or []
            self.sub_status.setText(steps[-1]["msg"] if steps else "Indul…")
            self.level.setValue(0)
        elif es == "error":
            self.sub_status.setText("Hiba: " + st.get("error", ""))
            self.level.setValue(0)
        else:
            self.sub_status.setText(st.get("finished") or "A feliratozó nem fut.")
            self.level.setValue(0)
        tr = st.get("translator") or {}
        chain = tr.get("chain") or []
        info = st.get("info", {})
        parts = []
        if chain:
            parts.append("Fordító: " + " → ".join(chain))
        if tr.get("requests"):
            parts.append(f"{tr['requests']} fordítás, {tr.get('sent_chars', 0):,} karakter".replace(",", " "))
        used, limit = info.get("deepl_used"), info.get("deepl_limit")
        if used is not None and limit:
            parts.append(f"DeepL havi keret: {used:,} / {limit:,}".replace(",", " "))
            self.deepl_bar.setValue(int(1000 * used / limit))
            self.deepl_bar.show()
        else:
            self.deepl_bar.hide()
        self.sub_tr.setText("\n".join(parts))

    # ------------------------------------------------------------------ subtitles
    def _subs_tick(self):
        v = self.sink.version
        style = self.cfg.style
        key = (v, self.cfg.subs_on_video, tuple(sorted(style.items())), self.video.tex_size, self.cfg.max_lines)
        if key == self._sub_key:
            return
        self._sub_key = key
        st = self.sink.state()
        if self.src == "desktop" and self.fmt:
            img = (render_subtitle(st["lines"], st["partial"], style, self.fmt["h"], self.fmt["w"])
                   if self.cfg.subs_on_video else None)
            self.overlay.set_image(img)
            self.video.set_subtitle_image(None, (1, 1), 0)
        elif self.cfg.subs_on_video and self.video.tex_size and self.video.tex_fmt:
            vw, vh = self.video.tex_size
            img = render_subtitle(st["lines"], st["partial"], style, vh, vw)
            self.video.set_subtitle_image(img, (vw, vh), int(style.get("bottom", 40) * vh / 1080))
        else:
            self.video.set_subtitle_image(None, (1, 1), 0)
        hist = self.sink.history_list(40)
        hkey = (len(hist), hist[-1]["t"] + hist[-1]["hu"] if hist else "")
        if hkey != self._hist_n:
            self._hist_n = hkey
            self.hist.clear()
            for h in reversed(hist[-25:]):
                it = QListWidgetItem()
                lb = QLabel(f"<span style='color:#eef0ff;font-weight:600'>{_esc(h['hu'])}</span><br>"
                            f"<span style='color:#7d86ad;font-size:8.5pt'>{h['t']} · {_esc(h['en'])}</span>")
                lb.setWordWrap(True)
                lb.setTextFormat(Qt.RichText)
                lb.setContentsMargins(2, 2, 2, 2)
                lb.setFixedWidth(320)
                lb.adjustSize()
                it.setSizeHint(lb.sizeHint())
                self.hist.addItem(it)
                self.hist.setItemWidget(it, lb)

    def _on_cue(self, hu, en, latency_ms):
        """Kész felirat: a felvétel mellé írt .srt-be (a felvétel idővonalán)."""
        rs, rec = self.rec_srt, self.rec
        if rs is None or not rec.active or rec.paused:
            return
        speech_end = rec.elapsed - latency_ms / 1000.0
        dur_speech = max(0.6, min(8.0, len(en) / 15.0))
        start = speech_end - dur_speech * 0.85
        end = max(start + display_seconds(hu, self.cfg), speech_end + 1.0)
        rs.add(start, end, hu)

    def _toggle_cc(self, on):
        self.cfg.update({"subs_on_video": bool(on)})
        self._save_cfg()

    def toggle_engine(self):
        if self.engine.state in ("running", "loading"):
            threading.Thread(target=self.engine.stop, daemon=True).start()
        else:
            ok, msg = self.engine.start("live")
            if not ok:
                self.statusBar().showMessage(msg, 6000)

    def engine_test(self):
        """A feliratozó kipróbálása a beépített angol mintafelvétellel (kártya és Xbox nélkül is)."""
        def run():
            if self.engine.state in ("running", "loading"):
                self.engine.stop("Próba indul.")
                time.sleep(0.3)
            ok, msg = self.engine.start("test", wav_path=sc.TEST_WAV)
            if not ok:
                self.bus.notify.emit(msg)
        threading.Thread(target=run, daemon=True).start()

    def _engine_log(self, msg):
        log.info("[engine] %s", msg)
        self.engine_last = msg

    # ------------------------------------------------------------------ recording
    def toggle_record(self):
        rec = self.rec
        if rec.active:
            self.btn_rec.setEnabled(False)

            def stop():
                rec.stop()
                rs, self.rec_srt = self.rec_srt, None
                if rs:
                    rs.close()
                self.bus.recordings_changed.emit()
                self.bus.notify.emit(f"Felvétel mentve: {rec.last_path}")
            threading.Thread(target=stop, name="rec-stop", daemon=True).start()
            QTimer.singleShot(400, lambda: self.btn_rec.setEnabled(True))
            return
        if not (self.cap and self.fmt and self.state == "run"):
            self.btn_rec.setChecked(False)
            self.statusBar().showMessage("Nincs jel – a felvétel akkor indul, ha van kép.", 6000)
            return
        f = self.fmt
        rec.start(f["w"], f["h"], f["fps"], self.cap.sample_rate, folder=rec_folder(self.cfg),
                  quality=self.cfg.rec_quality, codec=self.cfg.rec_codec, pix=f.get("pix", "nv12"))
        if self.cfg.rec_srt:
            try:
                self.rec_srt = RecSrt(os.path.splitext(rec.path)[0] + ".srt")
            except OSError:
                self.rec_srt = None
        self.lbl_size.setText("0 MB")
        self.bus.recordings_changed.emit()

    def toggle_pause(self):
        if self.rec.active:
            self.rec.toggle_pause()

    def snapshot(self):
        fr = self.last_frame
        if fr is None or not self.video.has_video():
            return
        d = os.path.join(rec_folder(self.cfg), "Képek")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("rksubby_%Y-%m-%d_%H-%M-%S.png"))
        try:
            fr.to_image().save(path)
            self.statusBar().showMessage(f"Pillanatkép: {path}", 6000)
        except Exception as e:
            self.statusBar().showMessage(f"Pillanatkép nem sikerült: {e}", 6000)

    def _refresh_recordings(self):
        d = rec_folder(self.cfg)
        files = sorted(glob.glob(os.path.join(d, "*.mp4")), key=lambda p: os.path.getmtime(p), reverse=True)[:30]
        cur = self.recs.currentItem().data(Qt.UserRole) if self.recs.currentItem() else None
        self.recs.clear()
        for p in files:
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            has_srt = os.path.exists(os.path.splitext(p)[0] + ".srt")
            name = os.path.basename(p)
            live = self.rec.active and p == self.rec.path
            it = QListWidgetItem(f"{name}\n{fmt_size(size)}{'  · felirat' if has_srt else ''}"
                                 f"{'  · FELVÉTEL FOLYIK' if live else ''}")
            it.setData(Qt.UserRole, p)
            it.setIcon(icon("video", "#ff7ad0" if live else "#9aa3d0", 18))
            self.recs.addItem(it)
            if p == cur:
                self.recs.setCurrentItem(it)
        if self.recs.currentItem() is None and self.recs.count():
            self.recs.setCurrentRow(0)

    def _selected(self):
        it = self.recs.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _play_selected(self):
        p = self._selected()
        if p and os.path.exists(p):
            os.startfile(p)

    def _delete_selected(self):
        p = self._selected()
        if not p:
            return
        if self.rec.active and p == self.rec.path:
            QMessageBox.information(self, "Törlés", "Ez a felvétel még folyik. Előbb állítsd le.")
            return
        srt = os.path.splitext(p)[0] + ".srt"
        if QMessageBox.question(self, "Lomtárba",
                                f"A felvétel a Lomtárba kerül (onnan visszaállítható):\n\n{os.path.basename(p)}"
                                + ("\n(a hozzá tartozó felirattal együtt)" if os.path.exists(srt) else "")
                                + "\n\nBiztosan?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        ok = recycle(p)
        if os.path.exists(srt):
            recycle(srt)
        self.statusBar().showMessage("Lomtárba helyezve." if ok else "Nem sikerült a Lomtárba helyezni.", 6000)
        self._refresh_recordings()

    def _export_selected(self):
        p = self._selected()
        if not p:
            return
        if self.rec.active and p == self.rec.path:
            QMessageBox.information(self, "Export", "Ez a felvétel még folyik. Előbb állítsd le.")
            return
        srt = os.path.splitext(p)[0] + ".srt"
        if not os.path.exists(srt):
            QMessageBox.information(self, "Export", "Ehhez a felvételhez nincs mentett felirat (.srt).\n"
                                                    "A feliratot a felvétel közben futó feliratozó menti.")
            return
        from . import export

        cues = export.parse_srt(srt)
        if not cues:
            QMessageBox.information(self, "Export", "A felirat üres.")
            return
        import av

        with av.open(p) as c:
            vw, vh = c.streams.video[0].width, c.streams.video[0].height
        style = dict(self.cfg.style)
        prepared = []
        for start, end, text in cues:
            img = render_subtitle([{"hu": text, "en": ""}], "", style, vh, vw)
            if img is None:
                continue
            img = img.convertToFormat(QImage.Format_RGBA8888)
            arr = np.frombuffer(bytes(img.constBits()), np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
            arr = arr[:, :img.width()].copy()
            x = (vw - img.width()) // 2
            y = vh - int(style.get("bottom", 40) * vh / 1080) - img.height()
            prepared.append((start, end, export.Overlay(arr, x, y)))
        dst = os.path.splitext(p)[0] + "_felirattal.mp4"
        dlg = QProgressDialog("Feliratos videó készítése (NVENC)…", "Mégse", 0, 1000, self)
        dlg.setWindowTitle("Export")
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModal)
        cancel = threading.Event()
        dlg.canceled.connect(cancel.set)
        bus = Bus(self)
        bus.export_progress.connect(lambda v: dlg.setValue(int(v * 1000)))

        def done(msg):
            dlg.close()
            self._refresh_recordings()
            self.statusBar().showMessage(msg, 10000)
        bus.export_done.connect(done)

        def work():
            try:
                export.burn(p, prepared, dst, progress=bus.export_progress.emit, cancel=cancel,
                            codec=self.cfg.rec_codec)
                bus.export_done.emit(f"Kész: {dst}")
            except InterruptedError:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                bus.export_done.emit("Export megszakítva.")
            except Exception as e:
                log.exception("export")
                bus.export_done.emit(f"Export hiba: {e}")
        self._export_bus = bus
        threading.Thread(target=work, name="export", daemon=True).start()

    # ------------------------------------------------------------------ misc UI actions
    def _toggle_mute(self):
        self.muted = not self.muted
        self.btn_mute.setIcon(icon("mute" if self.muted else "volume", "#dfe3ff", 20))
        self.cfg.update({"monitor_audio": not self.muted})
        self._save_cfg()

    def _volume_changed(self, v):
        self.volume = v / 100.0
        self.cfg.update({"monitor_volume": self.volume})
        QTimer.singleShot(800, self._save_cfg)

    def _toggle_panel(self, on):
        self.side.setVisible(on)
        self.cfg.update({"side_panel": bool(on)})
        self._save_cfg()

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        fs = self.fullscreen
        for w in (self.header, self.cbar, self.statusBar()):
            w.setVisible(not fs)
        self.side.setVisible(not fs and self.cfg.side_panel)
        self.centralWidget().layout().setContentsMargins(*((0, 0, 0, 0) if fs else (14, 10, 14, 8)))
        self.vframe.setStyleSheet("QFrame#VideoFrame { border: none; border-radius: 0; }" if fs else "")
        if fs:
            self._was_max = self.isMaximized()
            self.showFullScreen()
        else:
            self.showMaximized() if getattr(self, "_was_max", False) else self.showNormal()

    def _save_cfg(self):
        try:
            self.cfg.save()
        except OSError as e:
            log.warning("config save: %s", e)

    def _start_server(self):
        if self.cfg.obs_server and self.server is None:
            from subby.server import SubtitleServer

            self.server = SubtitleServer(self.sink, self.cfg.http_port)
            if not self.server.start():
                self.statusBar().showMessage(f"OBS-felirat: a {self.cfg.http_port}-es port foglalt.", 8000)
                self.server = None
        elif not self.cfg.obs_server and self.server is not None:
            self.server.stop()
            self.server = None

    def open_settings(self, page=0):
        from .settings import SettingsDialog

        try:
            devs = [d["name"] for d in capture.list_devices() if "video" in d["types"]]
        except Exception:
            devs = []
        self._refresh_outputs()
        dlg = SettingsDialog(self.cfg, devs, self, page, outputs=self.outputs)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        old_dev = self.cfg.dshow_device
        changed = set(self.cfg.update(dlg.changes()))
        key = dlg.new_key()
        if key is not None and key != sc.load_deepl_key():
            if key:
                sc.save_deepl_key(key)
            else:
                try:
                    os.remove(sc.DEEPL_KEY_FILE)
                except OSError:
                    pass
            changed.add("translator")
        self._save_cfg()
        log.info("settings changed: %s", sorted(changed))
        if changed & sc.TRANSLATOR_KEYS or "translator" in changed:
            self.engine.reload_translator()
        if changed & {"model", "asr_device", "profile", "end_silence_ms"} and self.engine.state == "running":
            if QMessageBox.question(self, "Feliratozó", "A változáshoz a feliratozó újraindul. Mehet?") == QMessageBox.Yes:
                def restart():
                    self.engine.stop("Újraindítás az új beállításokkal.")
                    time.sleep(0.5)
                    self.engine.start("live")
                threading.Thread(target=restart, daemon=True).start()
        if (("dshow_device" in changed and self.cfg.dshow_device != old_dev)
                or changed & {"source", "desktop_output", "desktop_fps"}) and not self.rec.active:
            self.dev = None
            self.restart_pipeline()
        if "desktop_subs_in_rec" in changed:
            self._apply_source()
        if "monitor_audio" in changed:
            self.muted = not self.cfg.monitor_audio
            self.btn_mute.setIcon(icon("mute" if self.muted else "volume", "#dfe3ff", 20))
        self.btn_cc.setChecked(self.cfg.subs_on_video)
        self._start_server()
        self._sub_key = None
        self._refresh_recordings()

    # ------------------------------------------------------------------ close
    def closeEvent(self, e):
        if self.rec.active:
            if QMessageBox.question(self, "Kilépés", "Felvétel folyamatban. Leállítod és kilépsz?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                e.ignore()
                return
        self.hide()
        QApplication.processEvents()
        try:
            if self.rec.active:
                self.rec.stop()
                if self.rec_srt:
                    self.rec_srt.close()
            self.engine.stop("Kilépés.")
            if self.cap:
                self.cap.stop()
            if self.server:
                self.server.stop()
            self.sink.close()
            self.overlay.close()
            if self._audio_dev:
                sdl2.SDL_CloseAudioDevice(self._audio_dev)
            self.video.close_sdl()
        except Exception:
            log.exception("shutdown")
        e.accept()


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ====================================================================== entry
def _selftest(w, app):
    """--selftest: a feliratozó a mintafelvétellel, az eredmény a naplóba, aztán kilépés (telepítés ellenőrzése)."""
    w.cfg.set_override("autostart", False)
    t0 = time.time()
    phase = {"started": False}

    def tick():
        if not phase["started"] and time.time() - t0 > 2:
            phase["started"] = True
            w.engine_test()
        st = w.engine.status()
        done = phase["started"] and time.time() - t0 > 6 and st["state"] in ("idle", "error")
        if done or time.time() - t0 > 240:
            hist = w.sink.history_list(20)
            log.info("SELFTEST state=%s error=%s model=%s translator=%s sentences=%d %s", st["state"], st.get("error"),
                     (st.get("info") or {}).get("model"), (st.get("translator") or {}).get("chain"), len(hist),
                     [(h["en"], h["hu"]) for h in hist][:6])
            timer.stop()
            w.close()
            app.quit()
    timer = QTimer(w, timeout=tick)
    timer.start(500)


def _record_test(w, app, seconds):
    st = {"t": None, "stop": None}

    def tick():
        now = time.time()
        if st["t"] is None and w.state == "run" and w.cap and w.cap.fps_in > 1:
            st["t"] = now
            w.toggle_record()
        elif st["t"] and st["stop"] is None and now - st["t"] > seconds:
            st["stop"] = now
            w.toggle_record()
        elif st["stop"] and not w.rec.active and now - st["stop"] > 2:
            log.info("RECORDTEST file=%s dropped=%s", w.rec.last_path, w.rec.dropped)
            timer.stop()
            w.close()
            app.quit()
    timer = QTimer(w, timeout=tick)
    timer.start(500)


def main(argv=None):
    log.info("start rksubby V%s", __version__)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("rknoise.rksubby.v3")
    except Exception:
        pass
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("rksubby V3")
    app.setWindowIcon(QIcon(os.path.join(ASSETS, "rksubby.ico")))
    app.setStyleSheet(QSS)
    # egy példány: a második indítás az elsőt hozza előre (a kártyát úgyis csak egy program használhatja)
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)
    w = MainWindow()
    args = argv or sys.argv
    if "--selftest" in args:
        _selftest(w, app)
    if "--record-seconds" in args:          # ellenőrzéshez: felvétel N mp-ig, amint van jel, aztán kilépés
        _record_test(w, app, float(args[args.index("--record-seconds") + 1]))

    def bring_front():
        c = server.nextPendingConnection()
        if c:
            c.close()
        w.showNormal() if w.isMinimized() else None
        w.raise_()
        w.activateWindow()
    server.newConnection.connect(bring_front)
    w.showMaximized()
    rc = app.exec()
    sdl2.SDL_Quit()
    log.info("exit %s", rc)
    return rc
