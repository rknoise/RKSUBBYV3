# -*- coding: utf-8 -*-
"""rksubby V3 – a capture kártya kezelése: eszközkeresés, jelformátum-felismerés, élő kép+hang, felvétel.

Csak a szabványos DirectShow-utat használja (PyAV/FFmpeg dshow), ahogy az OBS vagy a RECentral.
Gyártói, nem dokumentált driver-hívás NINCS (egy ilyen próbálgatás kékhalált okozott a GC573-mal)."""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import re
import threading
import time
from fractions import Fraction

import av
import av.logging
import numpy as np

log = logging.getLogger("rksubby")

DEVICE_HINT = "GC573"
STD_RATES = [24, 25, 30, 50, 60, 100, 120, 144]
MAX_SOURCE_FPS = 120  # Xbox Series max output
PIX = "nv12"  # a kártya NV12-t ad (GC573); ha nem tud, YUY2


# ---------------------------------------------------------------- device info
def _ffmpeg_log_text(url: str, options: dict) -> str:
    av.logging.set_level(av.logging.INFO)
    try:
        with av.logging.Capture() as logs:
            try:
                av.open(url, format="dshow", options=options).close()
            except Exception:
                pass
    finally:
        av.logging.set_level(None)
    return "".join(m for _lvl, _name, m in logs)


CARD_HINTS = ("gc573", "live gamer", "avermedia", "elgato", "game capture", "capture")


def list_devices() -> list[dict]:
    """DirectShow-eszközök: [{"name", "types": {"audio","video"}}]."""
    text = _ffmpeg_log_text("dummy", {"list_devices": "true"})
    out = []
    for name, types in re.findall(r'"([^"\r\n]+)"\s*\(([^)]*)\)', text):
        out.append({"name": name, "types": {t.strip() for t in types.split(",")} - {"none", ""}})
    return out


def find_device(preferred: str = "") -> str | None:
    """A beállított kártya, különben az első kép+hang eszköz, ami capture kártyának látszik."""
    devs = [d for d in list_devices() if "video" in d["types"]]
    names = [d["name"] for d in devs]
    if preferred and preferred in names:
        return preferred
    both = [d["name"] for d in devs if "audio" in d["types"]]
    for hint in CARD_HINTS:
        for n in both + names:
            if hint in n.lower():
                return n
    return both[0] if both else None


def list_caps(dev: str) -> dict[tuple[int, int], float]:
    """{(w,h): max fps} for NV12 from IAMStreamConfig::GetStreamCaps (ffmpeg list_options)."""
    text = _ffmpeg_log_text(f"video={dev}", {"list_options": "true"})
    caps: dict[tuple[int, int], float] = {}
    pix = "nv12" if "pixel_format=nv12" in text else "yuyv422"
    for m in re.finditer(r"pixel_format=" + pix + r"\s+min s=(\d+)x(\d+) fps=([\d.]+) max s=(\d+)x(\d+) fps=([\d.]+)", text):
        key = (int(m.group(4)), int(m.group(5)))
        caps[key] = max(caps.get(key, 0.0), float(m.group(6)))
    global PIX
    PIX = pix
    return caps


def snap_rate(f: float) -> int:
    return min(STD_RATES, key=lambda r: abs(r - f))


# ---------------------------------------------------------------- frame helpers
def y_plane(frame) -> np.ndarray:
    p = frame.planes[0]
    a = np.frombuffer(p, np.uint8)
    return a[: p.line_size * frame.height].reshape(frame.height, p.line_size)[:, : frame.width]


def frame_sig(frame) -> np.ndarray:
    y = y_plane(frame)
    return y[:: max(1, frame.height // 48), :: max(1, frame.width // 64)].astype(np.int16)


class NoSignalMatcher:
    """Recognises the driver's own 'No Signal' slate (static image) via correlation."""

    def __init__(self, png_path: str):
        self.ref = None
        try:
            if png_path.endswith(".npy"):      # előre kiszámolt 32x18-as normalizált minta
                self.ref = np.load(png_path).astype(np.float32).ravel()
                return
            from PIL import Image
            im = Image.open(png_path).convert("L").resize((32, 18), Image.BILINEAR)
            r = np.asarray(im, np.float32).ravel()
            self.ref = (r - r.mean()) / (r.std() + 1e-6)
        except Exception as e:  # pragma: no cover
            log.warning("nosignal ref load failed: %s", e)

    def is_nosignal(self, frame) -> bool:
        if self.ref is None:
            return False
        y = y_plane(frame)
        rows = np.linspace(0, frame.height - 1, 18 * 4).astype(int)
        cols = np.linspace(0, frame.width - 1, 32 * 4).astype(int)
        s = y[np.ix_(rows, cols)].astype(np.float32).reshape(18, 4, 32, 4).mean(axis=(1, 3)).ravel()
        if s.std() < 1:
            return False
        s = (s - s.mean()) / s.std()
        return float((s * self.ref).mean()) > 0.95


# ---------------------------------------------------------------- detection
XBOX_MODES = [(3840, 2160), (2560, 1440), (1920, 1080)]
DETAIL_THRESHOLD = 0.0025  # share of spectral energy in the top quarter band (native ~0.005+, upscaled <0.001)


def detail_ratio(y: np.ndarray) -> float | None:
    """Share of energy in the top 25 % of spatial frequencies (rows + columns).
    An upscaled picture has (almost) nothing there. None = picture too flat to judge."""
    y = y.astype(np.float32)
    y = y[:: max(1, y.shape[0] // 540)]  # rows subsample for speed (column FFT still full res)
    yr = y - y.mean(axis=1, keepdims=True)
    P = (np.abs(np.fft.rfft(yr, axis=1)) ** 2).mean(axis=0)
    tot = P[1:].sum()
    if tot < 1e3 * len(P):
        return None
    n = len(P)
    return float(P[int(n * 0.75):].sum() / tot)


def grab_y(dev, w, h, fps, skip=6):
    c = av.open(f"video={dev}", format="dshow", options={
        "video_size": f"{w}x{h}", "framerate": str(fps), "pixel_format": PIX, "rtbufsize": "512M"})
    try:
        for i, f in enumerate(c.decode(video=0)):
            if i >= skip:
                if _matcher and _matcher.is_nosignal(f):
                    return None, True
                return y_plane(f).copy(), False
    finally:
        c.close()
    return None, False


def detect_format(dev: str, caps: dict) -> dict:
    """Detect the incoming signal using only the standard capture path:
    1) the pin's current/default media type (IAMStreamConfig::GetFormat – what ffmpeg uses
       when no size/fps is requested) as fallback,
    2) resolution: the card scales to whatever is requested, so capture at each Xbox mode
       (4K, 1440p, 1080p) and keep the largest one that still carries real fine detail,
    3) frame rate: the highest rate the card offers for that size, capped at 120
       (1080p/1440p -> 120, 4K -> 60). Duplicate frames of slower content cost ~nothing in HEVC."""
    w, h, dfps = 1920, 1080, 60.0
    c = av.open(f"video={dev}", format="dshow", options={"rtbufsize": "256M"})
    try:
        s = c.streams.video[0]
        w, h = s.codec_context.width, s.codec_context.height
        dfps = float(s.average_rate or s.guessed_rate or 60)
    finally:
        c.close()
    log.info("default media type: %dx%d @ %.3f", w, h, dfps)
    result = {"w": w, "h": h, "fps": snap_rate(dfps), "default_fps": dfps, "measured": None,
              "nosignal": False, "detail": {}}
    # --- resolution
    chosen = None
    for mw, mh in XBOX_MODES:
        if (mw, mh) not in caps:
            continue
        rate = snap_rate(min(30 if mw > 2560 else 60, caps[(mw, mh)]))
        try:
            y, nos = grab_y(dev, mw, mh, rate)
        except Exception as e:
            log.warning("grab %dx%d failed: %s", mw, mh, e)
            continue
        if nos:
            result["nosignal"] = True
            break
        r = detail_ratio(y)
        result["detail"][f"{mw}x{mh}"] = r
        log.info("detail %dx%d = %s", mw, mh, r)
        if r is None:
            break  # flat picture: can't judge, keep default
        if r > DETAIL_THRESHOLD or (mw, mh) == XBOX_MODES[-1]:
            chosen = (mw, mh)
            break
    if chosen:
        w, h = chosen
        result.update(w=w, h=h)
    # --- frame rate
    maxfps = caps.get((w, h), dfps)
    probe = snap_rate(min(MAX_SOURCE_FPS, maxfps))
    result["fps"] = probe
    if result["nosignal"]:
        log.info("detect result (no signal): %s", result)
        return result
    # Capture rate = the highest Xbox rate the card offers for this size (never derived from
    # content cadence: a 60 fps game inside a 120 Hz signal looks identical to a 60 Hz signal).
    # The card never invents frames: opened at 120 with a 60 Hz HDMI signal it delivers ~60.
    # So the ARRIVAL rate (timestamps) is the signal rate. Content cadence is never used.
    try:
        stats = measure(dev, w, h, probe, seconds=1.0)
        result["measured"] = stats
        result["nosignal"] = stats["nosignal"]
        arr = stats["arrival_fps"]
        if not stats["nosignal"] and 0 < arr < probe * 0.92:
            slower = snap_rate(arr)
            if slower < probe:
                chk = measure(dev, w, h, slower, seconds=0.8)
                log.info("signal slower than %d: arrival %.2f -> check at %d gives %.2f", probe, arr, slower, chk["arrival_fps"])
                if chk["arrival_fps"] >= slower * 0.95:
                    result["fps"] = slower
    except Exception as e:
        log.warning("measure failed: %s", e)
    log.info("detect result: %s", result)
    return result


_matcher: NoSignalMatcher | None = None


def set_nosignal_ref(path: str):
    global _matcher
    _matcher = NoSignalMatcher(path)


def measure(dev, w, h, fps, seconds=1.5) -> dict:
    c = av.open(f"video={dev}", format="dshow", options={
        "video_size": f"{w}x{h}", "framerate": str(fps), "pixel_format": PIX, "rtbufsize": "512M"})
    n = uniq = 0
    prev = None
    t_first = t_last = None
    nos = 0
    start = time.perf_counter()
    try:
        for frame in c.decode(video=0):
            n += 1
            if n <= 8:  # warm-up
                continue
            t = frame.time
            t_first = t if t_first is None else t_first
            t_last = t
            sig = frame_sig(frame)
            if prev is None or np.abs(sig - prev).mean() > 0.25:
                uniq += 1
            prev = sig
            if _matcher and n % 10 == 0 and _matcher.is_nosignal(frame):
                nos += 1
            if time.perf_counter() - start > seconds + 0.5:
                break
    finally:
        c.close()
    cnt = n - 8
    span = (t_last - t_first) if (t_first is not None and t_last and t_last > t_first) else seconds
    arrival = (cnt - 1) / span if cnt > 1 else 0
    unique_fps = uniq / span if span else 0
    motion = uniq > 0.15 * cnt
    return {"frames": cnt, "arrival_fps": arrival, "unique_fps": min(unique_fps, arrival),
            "motion": motion, "nosignal": nos > 0 and nos >= (cnt // 10) // 2, "requested": fps}


# ---------------------------------------------------------------- recorder
def videos_dir() -> str:
    try:
        from ctypes import wintypes
        guid = (ctypes.c_byte * 16).from_buffer_copy(
            bytes.fromhex("1d9b98189b5c5b45841cab7c74e4ddfc"))  # FOLDERID_Videos
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(p)) == 0:
            d = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return os.path.join(d, "RKREBOX")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Videos", "RKREBOX")


class Recorder:
    """Encodes NV12 frames with NVENC (HEVC) + AAC into a crash-safe fragmented MP4."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue(maxsize=600)
        self.active = False
        self.paused = False
        self.path: str | None = None
        self.last_path: str | None = None
        self.elapsed = 0.0
        self.dropped = 0
        self.error: str | None = None
        self._thread: threading.Thread | None = None

    # control (any thread) -------------------------------------------------
    QUALITY_CQ = {"normal": 25, "high": 21, "max": 18}

    def start(self, w, h, fps, sample_rate=48000, folder="", quality="high", codec="hevc"):
        if self.active:
            return
        d = folder or videos_dir()
        os.makedirs(d, exist_ok=True)
        tag = f"{h}p{fps}" if w * 9 == h * 16 else f"{w}x{h}_{fps}"
        self.path = os.path.join(d, time.strftime(f"rksubby_%Y-%m-%d_%H-%M-%S_{tag}.mp4"))
        self.cq = self.QUALITY_CQ.get(quality, 21)
        self.codec = codec if codec in ("hevc", "h264") else "hevc"
        self.encoder_name = ""
        self.elapsed = 0.0
        self.dropped = 0
        self.error = None
        self.paused = False
        self.q = queue.Queue(maxsize=max(240, fps * 4))
        self.active = True
        self._thread = threading.Thread(target=self._run, args=(self.path, w, h, fps, sample_rate, self.q),
                                        name="encoder", daemon=True)
        self._thread.start()
        log.info("recording start %s", self.path)

    def stop(self, wait=True):
        if not self.active:
            return
        self.active = False
        self.q.put(None)
        if wait and self._thread:
            self._thread.join(timeout=30)
        self.last_path = self.path
        log.info("recording stopped %s (dropped %d)", self.path, self.dropped)

    def toggle_pause(self):
        if self.active:
            self.paused = not self.paused

    def push(self, kind, frame, t):
        if not self.active:
            return
        try:
            self.q.put_nowait((kind, frame, t))
        except queue.Full:
            if kind == "v":
                self.dropped += 1

    # worker --------------------------------------------------------------
    def _run(self, path, w, h, fps, sr, q):
        try:
            out = av.open(path, "w", format="mp4",
                          options={"movflags": "+frag_keyframe+empty_moov+default_base_moof"})
            bitrate = int(min(130e6, max(12e6, w * h * fps * 0.12 * (1.4 if self.codec == "h264" else 1.0))))
            enc = f"{self.codec}_nvenc"
            vs = None
            try:
                vs = out.add_stream(enc, rate=fps, options={
                    "preset": "p5", "tune": "hq", "rc": "vbr", "cq": str(self.cq),
                    "b": str(bitrate), "maxrate": str(int(bitrate * 1.5)), "bufsize": str(bitrate * 2),
                    "g": str(fps * 2), "bf": "2", "spatial-aq": "1"})
                vs.width, vs.height, vs.pix_fmt = w, h, "nv12"
                vs.codec_context.time_base = Fraction(1, fps)
                vs.codec_context.open()
            except Exception as e:  # nincs NVIDIA / NVENC: processzoros kódolás
                log.warning("NVENC unavailable (%s) -> libx264", e)
                out.close()
                out = av.open(path, "w", format="mp4",
                              options={"movflags": "+frag_keyframe+empty_moov+default_base_moof"})
                enc = "libx264"
                vs = out.add_stream(enc, rate=fps, options={"preset": "veryfast", "crf": str(self.cq),
                                                            "g": str(fps * 2)})
                vs.width, vs.height, vs.pix_fmt = w, h, "nv12"
                vs.codec_context.time_base = Fraction(1, fps)
            self.encoder_name = enc
            vs.time_base = Fraction(1, fps * 1000)
            as_ = out.add_stream("aac", rate=sr)
            as_.layout = "stereo"
            as_.bit_rate = 192000
            res = av.AudioResampler(format="fltp", layout="stereo", rate=sr, frame_size=1024)
        except Exception as e:
            self.error = f"Kódoló hiba: {e}"
            log.exception("encoder init")
            self.active = False
            return
        t0 = None
        offset = 0.0
        pause_t = None
        last_vpts = -1
        a_next = None
        tb = Fraction(1, fps)
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                kind, frame, t = item
                if t0 is None:
                    if kind != "v":
                        continue
                    t0 = t
                if self.paused:
                    if pause_t is None:
                        pause_t = t
                    continue
                if pause_t is not None:
                    offset += max(0.0, t - pause_t)
                    pause_t = None
                    a_next = None  # resync audio after pause
                rel = t - t0 - offset
                if kind == "v":
                    if frame.format.name != "nv12":
                        frame = frame.reformat(format="nv12")
                    pts = max(round(rel * fps), last_vpts + 1)
                    last_vpts = pts
                    frame.pts = pts
                    frame.time_base = tb
                    for p in vs.encode(frame):
                        out.mux(p)
                    self.elapsed = pts / fps
                else:
                    if rel < 0:
                        continue
                    frame.pts = None
                    if a_next is None:
                        a_next = max(0, round(rel * sr))
                    for f in res.resample(frame):
                        f.pts = a_next
                        f.time_base = Fraction(1, sr)
                        a_next += f.samples
                        for p in as_.encode(f):
                            out.mux(p)
            for p in vs.encode(None):
                out.mux(p)
            for f in res.resample(None):
                if a_next is None:
                    break
                f.pts = a_next
                a_next += f.samples
                for p in as_.encode(f):
                    out.mux(p)
            for p in as_.encode(None):
                out.mux(p)
        except Exception as e:
            self.error = f"Felvételi hiba: {e}"
            log.exception("encoder loop")
            self.active = False
        finally:
            try:
                out.close()
            except Exception:
                log.exception("close")


# ---------------------------------------------------------------- live capture
class Capture:
    """Single DirectShow session (video+audio of the card). Delivers frames to the preview
    slot, the audio sink and the recorder. Watches for signal loss / no-signal slate."""

    def __init__(self, dev, fmt, on_video, on_audio, recorder: Recorder):
        self.dev, self.fmt = dev, fmt
        self.on_video, self.on_audio, self.rec = on_video, on_audio, recorder
        self.stop_flag = threading.Event()
        self.nosignal = fmt.get("nosignal", False)
        self.signal_event = threading.Event()  # set when re-detection is needed
        self.fps_in = 0.0
        self.last_frame_wall = time.perf_counter()
        self.error: str | None = None
        self.sample_rate = 48000
        self.thread = threading.Thread(target=self._run, name="capture", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.thread.join(timeout=5)

    def _run(self):
        f = self.fmt
        try:
            c = av.open(f"video={self.dev}:audio={self.dev}", format="dshow", options={
                "video_size": f"{f['w']}x{f['h']}", "framerate": str(f["fps"]), "pixel_format": PIX,
                "rtbufsize": "512M", "audio_buffer_size": "20"})
        except Exception as e:
            self.error = str(e)
            log.exception("capture open")
            return
        try:
            vs = c.streams.video[0]
            if c.streams.audio:
                self.sample_rate = c.streams.audio[0].codec_context.sample_rate or 48000
            log.info("capture open: %s / %s", vs.codec_context.format.name if vs.codec_context.format else "?",
                     c.streams.audio[0].codec_context if c.streams.audio else "no audio")
            n = 0
            t_win = time.perf_counter()
            n_win = 0
            for packet in c.demux():
                if self.stop_flag.is_set():
                    break
                if packet.size == 0:
                    continue
                st = packet.stream.type
                for frame in packet.decode():
                    if st == "video":
                        n += 1
                        n_win += 1
                        now = time.perf_counter()
                        self.last_frame_wall = now
                        if now - t_win >= 1.0:
                            self.fps_in = n_win / (now - t_win)
                            t_win, n_win = now, 0
                        t = frame.time
                        self.on_video(frame)
                        self.rec.push("v", frame, t)
                        if _matcher and n % 30 == 0:
                            ns = _matcher.is_nosignal(frame)
                            if self.nosignal and not ns:
                                log.info("signal appeared -> re-detect")
                                self.signal_event.set()
                            self.nosignal = ns
                    elif st == "audio":
                        self.on_audio(frame)
                        self.rec.push("a", frame, frame.time)
        except Exception as e:
            if not self.stop_flag.is_set():
                self.error = str(e)
                log.exception("capture loop")
        finally:
            c.close()
