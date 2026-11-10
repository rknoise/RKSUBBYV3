# -*- coding: utf-8 -*-
"""A kártya képe a Qt-ablakban: SDL2 + Direct3D 11 egy natív Qt-widgetbe ágyazva.

A képkocka (NV12) közvetlenül a GPU-ra megy (SDL_UpdateNVTexture), a színátalakítás és a skálázás a
GPU-n történik, így 1080p120 mellett is alig terheli a processzort. A felirat egy előre megrajzolt
textúra: csak akkor rajzoljuk újra, ha a szöveg változik, képkockánként csak rámásoljuk."""

from __future__ import annotations

import ctypes
import threading
import time

import sdl2
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class ScreenOverlay(QWidget):
    """Asztali módban a felirat a képernyő alján, minden ablak fölött; az egér átmegy rajta."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.img = None
        self.bottom = 40

    def place(self, screen, bottom_px):
        g = screen.geometry()
        h = int(g.height() * 0.45)
        self.setGeometry(g.x(), g.y() + g.height() - h, g.width(), h)
        self.bottom = bottom_px

    def set_image(self, img):
        self.img = img
        self.update()

    def paintEvent(self, e):
        if self.img is None or self.img.isNull():
            return
        p = QPainter(self)
        dpr = self.devicePixelRatioF() or 1.0
        iw, ih = self.img.width() / dpr, self.img.height() / dpr
        p.drawImage(int((self.width() - iw) / 2), int(self.height() - self.bottom / dpr - ih),
                    self.img.scaled(int(iw), int(ih), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    if dpr != 1.0 else self.img)
        p.end()

U8 = ctypes.POINTER(ctypes.c_uint8)


class _FrameBus(QObject):
    frame = Signal()


class VideoWidget(QWidget):
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        for attr in (Qt.WA_NativeWindow, Qt.WA_PaintOnScreen, Qt.WA_NoSystemBackground, Qt.WA_OpaquePaintEvent):
            self.setAttribute(attr, True)
        self.setMinimumSize(320, 180)
        self.setFocusPolicy(Qt.StrongFocus)
        self._lock = threading.Lock()
        self._latest = None
        self._pending = False
        self._bus = _FrameBus()
        self._bus.frame.connect(self._on_frame, Qt.QueuedConnection)
        self.win = self.ren = None
        self.tex = None
        self.tex_size = None
        self.tex_fmt = None
        self.last_frame_t = 0.0
        self.presented = 0
        self.fps = 0.0
        self._fps_n, self._fps_t = 0, time.perf_counter()
        self._sub_tex = None
        self._sub_rect = None
        self._msg_tex = None
        self._msg_size = (1920, 1080)
        self.message = ""
        self._idle = QTimer(self)
        self._idle.timeout.connect(self._idle_tick)
        self._idle.start(100)

    # ------------------------------------------------------------------ Qt
    def paintEngine(self):  # Qt ne fessen rá: az SDL rajzol
        return None

    def paintEvent(self, e):
        self._present()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._present()

    def mouseDoubleClickEvent(self, e):
        self.double_clicked.emit()

    def showEvent(self, e):
        super().showEvent(e)
        if self.win is None:
            self._init_sdl()

    # ------------------------------------------------------------------ SDL
    def _init_sdl(self):
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_DRIVER, b"direct3d11")
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"1")
        sdl2.SDL_SetHint(b"SDL_WINDOWS_DPI_AWARENESS", b"unaware")  # a Qt már beállította
        if sdl2.SDL_WasInit(sdl2.SDL_INIT_VIDEO) == 0:
            sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_EVENTS)
        self.win = sdl2.SDL_CreateWindowFrom(ctypes.c_void_p(int(self.winId())))
        if not self.win:
            raise RuntimeError("SDL_CreateWindowFrom: " + sdl2.SDL_GetError().decode(errors="replace"))
        # vsync nélkül: a Qt felülete nem várakozik; ablakos módban a DWM úgyis szakadásmentesen komponál
        self.ren = sdl2.SDL_CreateRenderer(self.win, -1, sdl2.SDL_RENDERER_ACCELERATED)
        info = sdl2.SDL_RendererInfo()
        sdl2.SDL_GetRendererInfo(self.ren, ctypes.byref(info))
        self.renderer_name = info.name.decode()
        sdl2.SDL_SetRenderDrawColor(self.ren, 8, 8, 11, 255)
        self._present()

    def renderer_info(self):
        return getattr(self, "renderer_name", "?")

    # ------------------------------------------------------------------ frames (any thread)
    def push_frame(self, frame):
        with self._lock:
            self._latest = frame
            if self._pending:
                return
            self._pending = True
        self._bus.frame.emit()

    def _on_frame(self):
        with self._lock:
            frame, self._latest = self._latest, None
            self._pending = False
        if frame is None or self.ren is None:
            return
        self._upload(frame)
        self.last_frame_t = time.monotonic()
        self._present()
        self._fps_n += 1
        now = time.perf_counter()
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_n, self._fps_t = 0, now

    def _upload(self, frame):
        w, h = frame.width, frame.height
        fmt = frame.format.name
        if self.tex_size != (w, h) or self.tex_fmt != fmt:
            if self.tex:
                sdl2.SDL_DestroyTexture(self.tex)
            sfmt = {"nv12": sdl2.SDL_PIXELFORMAT_NV12, "bgra": sdl2.SDL_PIXELFORMAT_ARGB8888}.get(
                fmt, sdl2.SDL_PIXELFORMAT_YUY2)
            self.tex = sdl2.SDL_CreateTexture(self.ren, sfmt, sdl2.SDL_TEXTUREACCESS_STREAMING, w, h)
            self.tex_size, self.tex_fmt = (w, h), fmt
            sdl2.SDL_RenderSetLogicalSize(self.ren, w, h)
        p = frame.planes
        if fmt == "nv12":
            sdl2.SDL_UpdateNVTexture(self.tex, None, ctypes.cast(p[0].buffer_ptr, U8), p[0].line_size,
                                     ctypes.cast(p[1].buffer_ptr, U8), p[1].line_size)
        else:
            sdl2.SDL_UpdateTexture(self.tex, None, ctypes.c_void_p(p[0].buffer_ptr), p[0].line_size)

    def has_video(self):
        return self.tex is not None and time.monotonic() - self.last_frame_t < 1.0

    def _present(self):
        if self.ren is None:
            return
        sdl2.SDL_RenderClear(self.ren)
        if self.has_video():
            sdl2.SDL_RenderCopy(self.ren, self.tex, None, None)
            if self._sub_tex is not None:
                sdl2.SDL_RenderCopy(self.ren, self._sub_tex, None, self._sub_rect)
        elif self._msg_tex is not None:
            if self.tex_size != self._msg_size:
                sdl2.SDL_RenderSetLogicalSize(self.ren, *self._msg_size)
                self.tex_size, self.tex_fmt = self._msg_size, None
                if self.tex:
                    sdl2.SDL_DestroyTexture(self.tex)
                    self.tex = None
            sdl2.SDL_RenderCopy(self.ren, self._msg_tex, None, None)
        sdl2.SDL_RenderPresent(self.ren)
        self.presented += 1

    def _idle_tick(self):
        if self.ren is None:
            return
        ev = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ctypes.byref(ev)):
            pass
        if not self.has_video():
            self._present()

    # ------------------------------------------------------------------ overlays (GUI thread)
    def _texture_from_qimage(self, img: QImage):
        img = img.convertToFormat(QImage.Format_ARGB32)
        tex = sdl2.SDL_CreateTexture(self.ren, sdl2.SDL_PIXELFORMAT_ARGB8888, sdl2.SDL_TEXTUREACCESS_STATIC,
                                     img.width(), img.height())
        data = bytes(img.constBits())
        buf = ctypes.create_string_buffer(data, len(data))
        sdl2.SDL_UpdateTexture(tex, None, buf, img.bytesPerLine())
        sdl2.SDL_SetTextureBlendMode(tex, sdl2.SDL_BLENDMODE_BLEND)
        return tex

    def set_subtitle_image(self, img: QImage | None, video_size, bottom_px):
        if self.ren is None:
            return
        if self._sub_tex is not None:
            sdl2.SDL_DestroyTexture(self._sub_tex)
            self._sub_tex = None
        if img is None or img.isNull():
            return
        vw, vh = video_size
        x = (vw - img.width()) // 2
        y = vh - bottom_px - img.height()
        self._sub_tex = self._texture_from_qimage(img)
        self._sub_rect = sdl2.SDL_Rect(x, y, img.width(), img.height())

    def set_message(self, text: str, sub: str = "", logo: QImage | None = None):
        """A 'nincs kép' állapot képe (pl. Nincs jel)."""
        if self.ren is None or (text, sub) == getattr(self, "_msg_key", None):
            return
        self._msg_key = (text, sub)
        w, h = self._msg_size
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor(10, 10, 13))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        if logo is not None and not logo.isNull():
            lw = 360
            lg = logo.scaledToWidth(lw, Qt.SmoothTransformation)
            p.setOpacity(0.9)
            p.drawImage((w - lg.width()) // 2, h // 2 - lg.height() - 40, lg)
            p.setOpacity(1.0)
        f = QFont("Segoe UI Semibold", 44)
        p.setFont(f)
        p.setPen(QColor(240, 240, 245))
        p.drawText(0, h // 2, w, 80, Qt.AlignHCenter | Qt.AlignTop, text)
        if sub:
            p.setFont(QFont("Segoe UI", 26))
            p.setPen(QColor(160, 160, 175))
            p.drawText(160, h // 2 + 90, w - 320, 200, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, sub)
        p.end()
        if self._msg_tex is not None:
            sdl2.SDL_DestroyTexture(self._msg_tex)
        self._msg_tex = self._texture_from_qimage(img)
        self._present()

    def close_sdl(self):
        for t in (self.tex, self._sub_tex, self._msg_tex):
            if t:
                sdl2.SDL_DestroyTexture(t)
        self.tex = self._sub_tex = self._msg_tex = None
        if self.ren:
            sdl2.SDL_DestroyRenderer(self.ren)
            self.ren = None


# ---------------------------------------------------------------------- subtitle drawing
def render_subtitle(lines, partial, style, video_h, video_w):
    """A felirat képe (QImage, átlátszó háttérrel), a videó saját felbontásában.
    lines: [{"hu", "en"}], partial: szürke angol előnézet. None, ha nincs mit mutatni."""
    if not lines and not partial:
        return None
    scale = video_h / 1080.0
    fs = max(14, int(style.get("font_size", 40) * scale))
    font = QFont(style.get("font", "Segoe UI"), 1)
    font.setPixelSize(fs)
    font.setBold(True)
    en_font = QFont(style.get("font", "Segoe UI"), 1)
    en_font.setPixelSize(max(12, int(fs * 0.62)))
    fm, efm = QFontMetrics(font), QFontMetrics(en_font)
    max_w = min(video_w - 40, int(style.get("max_width", 1500) * scale))
    color = QColor(style.get("color", "#ffffff"))
    en_color = QColor(style.get("en_color", "#d0d0d0"))
    items = []  # (text, font, fm, color)
    for ln in lines:
        items.append((ln["hu"], font, fm, color))
        if ln.get("en"):
            items.append((ln["en"], en_font, efm, en_color))
    if partial:
        items.append((partial, en_font, efm, QColor(170, 170, 180)))
    # sortördelés
    rows = []
    for text, f, m, c in items:
        words, cur = text.split(), ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if m.horizontalAdvance(t) > max_w and cur:
                rows.append((cur, f, m, c))
                cur = wd
            else:
                cur = t
        if cur:
            rows.append((cur, f, m, c))
    if not rows:
        return None
    pad = int(14 * scale)
    gap = int(4 * scale)
    width = max(m.horizontalAdvance(t) for t, f, m, c in rows) + 2 * pad
    height = sum(m.height() for t, f, m, c in rows) + gap * (len(rows) - 1) + 2 * pad
    img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    if style.get("box"):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, int(255 * float(style.get("box_opacity", 0.5)))))
        p.drawRoundedRect(0, 0, width, height, 12 * scale, 12 * scale)
    y = pad
    outline = max(2.0, fs * 0.09)
    for t, f, m, c in rows:
        x = (width - m.horizontalAdvance(t)) / 2
        path = QPainterPath()
        path.addText(x, y + m.ascent(), f, t)
        if not style.get("box"):
            p.setPen(QPen(QColor(0, 0, 0, 230), outline * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPath(path)
        y += m.height() + gap
    p.end()
    return img
