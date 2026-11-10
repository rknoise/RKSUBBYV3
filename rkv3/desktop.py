# -*- coding: utf-8 -*-
"""Windows asztal mint forrás (PC-s játék, program, videó).

Kép: DXGI Desktop Duplication (dxcam) – ugyanaz a dokumentált Windows API, amit az OBS „Kijelző rögzítése”
módja használ; a játékba nem nyúl bele (nincs injektálás), csalásvédelmet nem zavar.
Hang: WASAPI loopback (PyAudioWPatch) – amit a gép a hangszóróra/fejhallgatóra lejátszik.
A felület ugyanazt kapja, mint a capture kártyától: képkockákat (BGRA) és hangkereteket (s16)."""
from __future__ import annotations

import ctypes
import logging
import threading
import time

import av
import numpy as np

log = logging.getLogger("rksubby")
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
_dx_lock = threading.Lock()


def exclude_from_capture(hwnd: int, on: bool = True) -> bool:
    """Az ablak ne látsszon a képernyőfelvételen (Windows 10 2004+). A képernyőn ugyanúgy látszik."""
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(hwnd), WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
    except Exception:
        return False


def list_outputs(qt_screens=None) -> list[dict]:
    """A rögzíthető monitorok (az elsődleges videokártyán): [{idx, name, x, y, w, h, hz, primary}]."""
    import dxcam

    with _dx_lock:
        fac = vars(dxcam).get("__factory")
        if fac is None:
            fac = next(v for k, v in vars(dxcam).items() if "factory" in k.lower())
        outs = []
        for oi, o in enumerate(fac.outputs[0] if fac.outputs else []):
            try:
                o.update_desc()
            except Exception:
                pass
            r = o.desc.DesktopCoordinates
            outs.append({"idx": oi, "dev": o.desc.DeviceName, "x": r.left, "y": r.top,
                         "w": r.right - r.left, "h": r.bottom - r.top, "name": f"Monitor {oi + 1}", "hz": 60,
                         "primary": r.left == 0 and r.top == 0})
    for o in outs:
        for s in qt_screens or []:
            g, dpr = s.geometry(), s.devicePixelRatio()
            if abs(g.x() * dpr - o["x"]) < 8 and abs(g.y() * dpr - o["y"]) < 8:
                o["name"] = s.name() or o["name"]
                o["hz"] = int(round(s.refreshRate() or 60))
                o["qt"] = s
    return outs


def default_loopback():
    import pyaudiowpatch as pa

    p = pa.PyAudio()
    try:
        dev = p.get_default_wasapi_loopback()
        return p, dev
    except Exception:
        p.terminate()
        raise


class DesktopCapture:
    """Ugyanaz a felület, mint capture.Capture (a főablak ugyanúgy kezeli)."""

    kind = "desktop"

    def __init__(self, output: dict, fmt: dict, on_video, on_audio, recorder):
        self.output, self.fmt = output, fmt
        self.on_video, self.on_audio, self.rec = on_video, on_audio, recorder
        self.stop_flag = threading.Event()
        self.signal_event = threading.Event()
        self.nosignal = False
        self.fps_in = 0.0
        self.last_frame_wall = time.perf_counter()
        self.error: str | None = None
        self.audio_error = ""
        self.sample_rate = 48000
        self.audio_desc = ""
        self._cam = None
        self._pa = self._stream = None
        self._vt = threading.Thread(target=self._run_video, name="desktop-video", daemon=True)

    def start(self):
        self._start_audio()
        self._vt.start()

    def stop(self):
        self.stop_flag.set()
        self._vt.join(timeout=3)
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
            if self._pa:
                self._pa.terminate()
        except Exception:
            pass
        self._stream = self._pa = None

    # ------------------------------------------------------------------ video
    def _run_video(self):
        import dxcam

        fps = int(self.fmt["fps"])
        try:
            with _dx_lock:
                cam = dxcam.create(device_idx=0, output_idx=self.output["idx"], output_color="BGRA")
                cam.start(target_fps=fps, video_mode=True)
            self._cam = cam
        except Exception as e:
            self.error = f"asztal rögzítése nem indul: {e}"
            log.exception("dxcam start")
            return
        log.info("desktop capture: %s (%dx%d) @ %d fps", self.output.get("name"), self.output["w"], self.output["h"], fps)
        n_win, t_win = 0, time.perf_counter()
        try:
            while not self.stop_flag.is_set():
                arr = cam.get_latest_frame()
                if arr is None:
                    continue
                frame = av.VideoFrame.from_ndarray(arr, format="bgra")
                t = time.monotonic()
                now = time.perf_counter()
                self.last_frame_wall = now
                n_win += 1
                if now - t_win >= 1.0:
                    self.fps_in = n_win / (now - t_win)
                    n_win, t_win = 0, now
                self.on_video(frame)
                self.rec.push("v", frame, t)
        except Exception as e:
            if not self.stop_flag.is_set():
                self.error = str(e)
                log.exception("desktop capture loop")
        finally:
            try:
                with _dx_lock:
                    cam.stop()
                    cam.release()
            except Exception:
                pass

    # ------------------------------------------------------------------ audio
    def _start_audio(self):
        try:
            import pyaudiowpatch as pa

            self._pa, dev = default_loopback()
            ch = int(dev["maxInputChannels"]) or 2
            rate = int(dev["defaultSampleRate"]) or 48000
            self.sample_rate = rate
            self.audio_desc = f"{dev['name']} ({rate} Hz, {ch} csatorna)"
            out_ch = 2 if ch >= 2 else 1
            layout = "stereo" if out_ch == 2 else "mono"

            def cb(data, n, info, status):
                try:
                    a = np.frombuffer(data, np.int16).reshape(-1, ch)
                    if ch != out_ch:
                        a = a[:, :out_ch]
                    a = np.ascontiguousarray(a)
                    fr = av.AudioFrame.from_ndarray(a.reshape(1, -1), format="s16", layout=layout)
                    fr.sample_rate = rate
                    t = time.monotonic() - n / rate
                    self.on_audio(fr)
                    self.rec.push("a", fr, t)
                except Exception:
                    log.exception("loopback cb")
                return (None, pa.paContinue)

            self._stream = self._pa.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                                         input_device_index=dev["index"], frames_per_buffer=rate // 100,
                                         stream_callback=cb)
            self._stream.start_stream()
            log.info("desktop audio (loopback): %s", self.audio_desc)
        except Exception as e:
            self.audio_error = f"rendszerhang nem rögzíthető: {e}"
            log.warning(self.audio_error)
