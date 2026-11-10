# -*- coding: utf-8 -*-
"""Feliratos (beégetett) export: a felvétel + a mellé mentett .srt -> új videó, a felirat a képen.
A kép NVENC-cel kódolódik újra (ugyanaz a felbontás és képkockasebesség), a hang változatlanul átmásolódik."""
from __future__ import annotations

import logging
import re
import threading
from fractions import Fraction

import av
import numpy as np

log = logging.getLogger("rksubby")
_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(path):
    cues = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        blocks = re.split(r"\n\s*\n", f.read().replace("\r", ""))
    for b in blocks:
        lines = [x for x in b.strip().split("\n") if x.strip()]
        for i, ln in enumerate(lines):
            m = _TS.search(ln)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                text = "\n".join(lines[i + 1:]).strip()
                if text and end > start:
                    cues.append((start, end, text))
                break
    return cues


def rgba_to_yuva(rgba: np.ndarray):
    """RGBA (h, w, 4) uint8 -> Y, U, V (4:2:0) és alfa (teljes + fél felbontás), BT.709, limited range."""
    h, w = rgba.shape[:2]
    h2, w2 = h - h % 2, w - w % 2
    rgba = rgba[:h2, :w2].astype(np.float32)
    r, g, b, a = rgba[..., 0], rgba[..., 1], rgba[..., 2], rgba[..., 3] / 255.0
    y = 16 + 0.1826 * r + 0.6142 * g + 0.0620 * b
    u = 128 - 0.1006 * r - 0.3386 * g + 0.4392 * b
    v = 128 + 0.4392 * r - 0.3989 * g - 0.0403 * b

    def half(x):
        return x.reshape(h2 // 2, 2, w2 // 2, 2).mean(axis=(1, 3))

    return y, half(u), half(v), a, half(a)


class Overlay:
    def __init__(self, rgba, x, y):
        self.y, self.u, self.v, self.a, self.a2 = rgba_to_yuva(rgba)
        self.x, self.top = x - x % 2, y - y % 2


def burn(src, cues_imgs, dst, progress=None, cancel: threading.Event | None = None, codec="hevc", cq=20):
    """cues_imgs: [(start, end, Overlay)] (az Overlay-t a hívó készíti a felirat képéből)."""
    inp = av.open(src)
    vin = inp.streams.video[0]
    vin.thread_type = "AUTO"
    ain = inp.streams.audio[0] if inp.streams.audio else None
    duration = float(inp.duration / av.time_base) if inp.duration else 0.0
    rate = vin.average_rate or vin.guessed_rate or Fraction(60)
    fps = int(round(float(rate)))
    out = av.open(dst, "w", format="mp4", options={"movflags": "+faststart"})
    try:
        vout = out.add_stream(f"{codec}_nvenc", rate=fps, options={"preset": "p5", "tune": "hq", "rc": "vbr",
                                                                   "cq": str(cq), "bf": "2", "g": str(fps * 2)})
        vout.width, vout.height, vout.pix_fmt = vin.width, vin.height, "yuv420p"
        vout.codec_context.time_base = Fraction(1, fps)
        vout.codec_context.open()
    except Exception as e:
        log.warning("export: NVENC unavailable (%s) -> libx264", e)
        out.close()
        out = av.open(dst, "w", format="mp4", options={"movflags": "+faststart"})
        vout = out.add_stream("libx264", rate=fps, options={"preset": "faster", "crf": str(cq)})
        vout.width, vout.height, vout.pix_fmt = vin.width, vin.height, "yuv420p"
        vout.codec_context.time_base = Fraction(1, fps)
    aout = out.add_stream_from_template(ain) if ain else None
    streams = [vin] + ([ain] if ain else [])
    t0 = None
    last_pts = -1
    ci = 0
    n = 0
    try:
        for packet in inp.demux(*streams):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("megszakítva")
            if packet.stream is ain:
                if packet.dts is None:
                    continue
                packet.stream = aout
                out.mux(packet)
                continue
            for frame in packet.decode():
                t = frame.time if frame.time is not None else 0.0
                if t0 is None:
                    t0 = t
                rel = t - t0
                while ci < len(cues_imgs) and cues_imgs[ci][1] <= rel:
                    ci += 1
                cue = cues_imgs[ci] if ci < len(cues_imgs) and cues_imgs[ci][0] <= rel else None
                if frame.format.name != "yuv420p":
                    frame = frame.reformat(format="yuv420p")
                if cue is not None:
                    frame = _blend(frame, cue[2])
                pts = max(round(rel * fps), last_pts + 1)
                last_pts = pts
                frame.pts = pts
                frame.time_base = Fraction(1, fps)
                for p in vout.encode(frame):
                    out.mux(p)
                n += 1
                if progress and n % 30 == 0 and duration:
                    progress(min(0.999, rel / duration))
        for p in vout.encode(None):
            out.mux(p)
    finally:
        out.close()
        inp.close()
    if progress:
        progress(1.0)


def _blend(frame, ov: Overlay):
    arr = frame.to_ndarray()  # yuv420p: (h*3/2, w)
    h, w = frame.height, frame.width
    Y = arr[:h]
    U = arr[h:h + h // 4].reshape(h // 2, w // 2)
    V = arr[h + h // 4:].reshape(h // 2, w // 2)
    oh, ow = ov.y.shape
    x0, y0 = max(0, ov.x), max(0, ov.top)
    x1, y1 = min(w, x0 + ow), min(h, y0 + oh)
    if x1 <= x0 or y1 <= y0:
        return frame
    sh, sw = y1 - y0, x1 - x0
    a = ov.a[:sh, :sw]
    reg = Y[y0:y1, x0:x1].astype(np.float32)
    Y[y0:y1, x0:x1] = (reg * (1 - a) + ov.y[:sh, :sw] * a).astype(np.uint8)
    cy0, cx0, ch, cw = y0 // 2, x0 // 2, sh // 2, sw // 2
    a2 = ov.a2[:ch, :cw]
    for P, S in ((U, ov.u), (V, ov.v)):
        reg = P[cy0:cy0 + ch, cx0:cx0 + cw].astype(np.float32)
        P[cy0:cy0 + ch, cx0:cx0 + cw] = (reg * (1 - a2) + S[:ch, :cw] * a2).astype(np.uint8)
    nf = av.VideoFrame.from_ndarray(arr, format="yuv420p")
    return nf
