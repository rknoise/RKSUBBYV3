# -*- coding: utf-8 -*-
"""Cutscene-figyelő: ha a megjelölt HUD-elemek (minitérkép, életerő, óra…) eltűnnek a képről, magától
felveszi az átvezető jelenetet, és leállítja, amikor a HUD visszajön.

Három rész:
  HudModel         a megjelölt HUD-elemek mintája (élek iránya) játékprofilonként és felbontásonként;
                   magától megtanulja, mely képpontok állandók (a keret) és melyek változnak (a térkép tartalma)
  ReplayRing       folyamatos NVENC-kódolás a memóriába (az utolsó pár másodperc mindig megvan), ebből vágja ki
                   a jelenet fájlját – így a jelenet eleje sem marad le
  CutsceneWatcher  másodpercenként ~8-szor megnézi a képet, és vezérli a felvételt
"""
from __future__ import annotations

import collections
import io
import json
import logging
import os
import queue
import threading
import time
from fractions import Fraction

import av
import numpy as np

from subby import config as sc

log = logging.getLogger("rksubby")
HUD_DIR = os.path.join(sc.HOME_DIR, "hud")
ANALYSIS_H = 540          # az elemzés kb. ekkora képmagasságon fut (1080p: 2x2-es átlagolás)
EDGE_T = 20.0             # a mintában ennél erősebb él számít
PRESENT, ABSENT = 0.55, 0.30
LEARN_S = 8.0             # megjelölés után ennyi ideig tanulja, mi állandó
MIN_MASK = 30


# ====================================================================== képelemzés
def _luma(frame, x0, y0, x1, y1) -> np.ndarray:
    """A képkocka egy téglalapjának fényessége (uint8), a forrás formátumától függetlenül."""
    fmt = frame.format.name
    h = frame.height
    p = frame.planes[0]
    if fmt in ("nv12", "yuv420p", "gray"):
        a = np.frombuffer(p, np.uint8)[: p.line_size * h].reshape(h, p.line_size)
        return a[y0:y1, x0:x1]
    if fmt in ("bgra", "bgr0"):
        a = np.frombuffer(p, np.uint8)[: p.line_size * h].reshape(h, p.line_size)[y0:y1, x0 * 4:x1 * 4]
        a = a.reshape(y1 - y0, x1 - x0, 4).astype(np.uint16)
        return ((a[..., 0] * 29 + a[..., 1] * 150 + a[..., 2] * 77) >> 8).astype(np.uint8)
    g = frame.reformat(format="gray")
    return _luma(g, x0, y0, x1, y1)


def analysis_step(h: int) -> int:
    return max(1, int(round(h / ANALYSIS_H)))


def region_luma(frame, rect) -> np.ndarray:
    """rect = (x, y, w, h) 0..1 arányban -> kicsinyített (k×k átlagolt) float32 fényességkép."""
    W, H = frame.width, frame.height
    k = analysis_step(H)
    x0 = max(0, min(W - 2 * k, int(rect[0] * W)))
    y0 = max(0, min(H - 2 * k, int(rect[1] * H)))
    x1 = min(W, x0 + max(8 * k, int(rect[2] * W)))
    y1 = min(H, y0 + max(8 * k, int(rect[3] * H)))
    x1 -= (x1 - x0) % k
    y1 -= (y1 - y0) % k
    y = _luma(frame, x0, y0, x1, y1).astype(np.float32)
    if k > 1:
        y = y.reshape((y1 - y0) // k, k, (x1 - x0) // k, k).mean(axis=(1, 3))
    return y


def gradients(y: np.ndarray):
    gx = y[1:-1, 2:] - y[1:-1, :-2]
    gy = y[2:, 1:-1] - y[:-2, 1:-1]
    return gx, gy


def frame_stats(frame):
    """(átlagfényesség, kis ujjlenyomat) a teljes képről: fekete kép és állókép felismeréséhez."""
    W, H = frame.width, frame.height
    y = _luma(frame, 0, 0, W, H)[:: max(1, H // 36), :: max(1, W // 64)].astype(np.int16)
    return float(y.mean()), y


class HudRegion:
    def __init__(self, rect, gx, gy, s=None):
        self.rect = tuple(float(v) for v in rect)
        self.gx, self.gy = gx.astype(np.float32), gy.astype(np.float32)
        self.mag = np.hypot(self.gx, self.gy)
        self.edge = self.mag > EDGE_T
        self.s = np.ones(self.gx.shape, np.float32) if s is None else s.astype(np.float32)
        self.score = 1.0
        self.present = True
        self._remask()

    def _remask(self):
        self.mask = self.edge & (self.s > 0.5)
        self.n = int(self.mask.sum())

    @property
    def usable(self):
        return self.n >= MIN_MASK

    def match(self, frame):
        """Képpontonkénti egyezés (él ugyanott, ugyanolyan irányban) és az összpontszám a maszkon."""
        y = region_luma(frame, self.rect)
        if y.shape[0] - 2 != self.gx.shape[0] or y.shape[1] - 2 != self.gx.shape[1]:
            return None, 0.0
        gx, gy = gradients(y)
        mag = np.hypot(gx, gy)
        cos = np.abs(gx * self.gx + gy * self.gy) / (mag * self.mag + 1e-6)
        c = (cos > 0.94) & (mag > 0.4 * self.mag) & (mag < 3.0 * self.mag)
        score = float(c[self.mask].mean()) if self.n else 0.0
        return c, score

    def update(self, frame, learning: bool):
        c, score = self.match(frame)
        if c is None:
            self.score = 0.0
            return 0.0
        self.score = score
        if score >= PRESENT:
            self.present = True
        elif score <= ABSENT:
            self.present = False
        if learning:
            self.s = 0.88 * self.s + 0.12 * c
            self._remask()
        elif score >= PRESENT:           # lassú finomítás, csak amikor biztosan látszik
            self.s = 0.985 * self.s + 0.015 * c
            self._remask()
        return score


class HudModel:
    def __init__(self, profile: str, size, regions: list[HudRegion], learned_s: float = 0.0):
        self.profile, self.size, self.regions = profile, tuple(size), regions
        self.learned_s = learned_s        # eddig ennyi tanulási idő telt el
        self._t = None

    # ---- létrehozás / mentés
    @staticmethod
    def path(profile, size):
        return os.path.join(HUD_DIR, f"{profile}_{size[0]}x{size[1]}.npz")

    @classmethod
    def from_frame(cls, profile, frame, rects):
        """A megjelölt téglalapokból. Visszaadja a modellt és az elutasított (túl sima) téglalapok sorszámát."""
        regions, rejected = [], []
        for i, r in enumerate(rects):
            gx, gy = gradients(region_luma(frame, r))
            reg = HudRegion(r, gx, gy)
            (regions if reg.usable else rejected).append(reg if reg.usable else i)
        return cls(profile, (frame.width, frame.height), regions), rejected

    @classmethod
    def load(cls, profile, size):
        try:
            with np.load(cls.path(profile, size), allow_pickle=False) as d:
                meta = json.loads(str(d["meta"]))
                regions = [HudRegion(meta["rects"][i], d[f"gx{i}"], d[f"gy{i}"], d[f"s{i}"])
                           for i in range(len(meta["rects"]))]
            return cls(profile, size, regions, float(meta.get("learned_s", LEARN_S)))
        except (OSError, KeyError, ValueError):
            return None

    def save(self):
        try:
            os.makedirs(HUD_DIR, exist_ok=True)
            data = {"meta": json.dumps({"rects": [r.rect for r in self.regions], "learned_s": self.learned_s})}
            for i, r in enumerate(self.regions):
                data[f"gx{i}"], data[f"gy{i}"] = r.gx.astype(np.float16), r.gy.astype(np.float16)
                data[f"s{i}"] = r.s.astype(np.float16)
            tmp = self.path(self.profile, self.size) + ".tmp.npz"
            np.savez(tmp, **data)
            os.replace(tmp, self.path(self.profile, self.size))
        except OSError as e:
            log.warning("hud save: %s", e)

    # ---- futás
    @property
    def learning(self):
        return self.learned_s < LEARN_S

    def update(self, frame, now):
        """Frissíti az elemek pontszámát. Vissza: 'present' | 'absent' | 'mixed' | 'weak' (nincs használható elem)."""
        dt = 0.0 if self._t is None else min(0.5, now - self._t)
        self._t = now
        learning = self.learning
        for r in self.regions:
            r.update(frame, learning)
        if learning:
            self.learned_s += dt
            return "present"
        usable = [r for r in self.regions if r.usable]
        if not usable:
            return "weak"
        n_present = sum(1 for r in usable if r.present)
        if n_present == 0:
            return "absent"
        return "present" if n_present * 2 >= len(usable) else "mixed"

    def scores(self):
        return [round(r.score, 2) for r in self.regions]


# ====================================================================== előpufferes kódoló
class ReplayRing:
    """Folyamatosan kódol (NVENC + AAC) a memóriába; a fájl a pufferből indul, egy kulcsképkockától.

    A csomagok a memóriában maradnak `preroll` + néhány másodpercig. begin_file(): megnyitja a fájlt, és
    beleírja a puffert; utána minden új csomag egyenesen a fájlba megy. hold()/release(): a HUD visszatérésekor
    visszatartja az új csomagokat, így a fájl vége nem lóg bele a játékmenetbe."""

    MOV = {"movflags": "+frag_keyframe+empty_moov+default_base_moof"}

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.active = False
        self.writing = False
        self.path = None
        self.error = None
        self.encoder_name = ""
        self.dropped = 0
        self.file_seconds = 0.0
        self._thread = None
        self._file_wall = 0.0
        self._preroll_used = 0.0

    def start(self, w, h, fps, sample_rate=48000, pix="nv12", codec="hevc", cq=21, preroll=5.0):
        if self.active:
            self.stop()
        self.error = None
        self.dropped = 0
        self.q = queue.Queue(maxsize=max(240, fps * 3))
        self.active = True
        self._thread = threading.Thread(target=self._run, name="replay-ring", daemon=True,
                                        args=(self.q, w, h, fps, sample_rate, pix, codec, cq, float(preroll)))
        self._thread.start()

    def stop(self):
        if not self.active:
            return
        self.active = False
        self.q.put(("stop",))
        if self._thread:
            self._thread.join(timeout=10)
        self.writing = False

    def push(self, kind, frame, t):
        if not self.active:
            return
        try:
            self.q.put_nowait((kind, frame, t))
        except queue.Full:
            self.dropped += 1

    def begin_file(self, path):
        self.q.put(("begin", path))

    def hold(self):
        self.q.put(("hold",))

    def release(self):
        self.q.put(("release",))

    def end_file(self, on_done=None):
        self.q.put(("end", on_done))

    def file_elapsed(self):
        """A most íródó fájl idővonalán hol tartunk (mp) – a felirat időzítéséhez."""
        return (time.monotonic() - self._file_wall) + self._preroll_used if self.writing else 0.0

    # ------------------------------------------------------------------ worker
    def _open_encoder(self, w, h, fps, sr, pix, codec, cq):
        last = None
        for enc, opts, epix in ((f"{codec}_nvenc", {"preset": "p5", "tune": "hq", "rc": "vbr", "cq": str(cq),
                                                    "g": str(fps), "bf": "0", "forced-idr": "1"}, pix),
                                ("libx264", {"preset": "veryfast", "crf": str(cq), "g": str(fps), "bf": "0"},
                                 "yuv420p")):
            try:
                buf = io.BytesIO()
                out0 = av.open(buf, "w", format="mp4", options=self.MOV)
                vs = out0.add_stream(enc, rate=fps, options=opts)
                vs.width, vs.height, vs.pix_fmt = w, h, epix
                vs.codec_context.time_base = Fraction(1, fps)
                if enc.endswith("nvenc"):
                    br = int(min(130e6, max(12e6, w * h * fps * 0.12)))
                    vs.codec_context.options.update({"b": str(br), "maxrate": str(int(br * 1.5))})
                as_ = out0.add_stream("aac", rate=sr)
                as_.layout = "stereo"
                as_.bit_rate = 192000
                out0.start_encoding()            # megnyitja a kódolókat, és megírja a fejlécet (ebből lesz a sablon)
                tmpl = av.open(io.BytesIO(buf.getvalue()))
                return out0, vs, as_, tmpl, enc, epix
            except Exception as e:
                last = e
                log.warning("ring encoder %s unavailable: %s", enc, e)
        raise last

    def _run(self, q, w, h, fps, sr, pix, codec, cq, preroll):
        try:
            out0, vs, as_, tmpl, enc, epix = self._open_encoder(w, h, fps, sr, pix, codec, cq)
        except Exception as e:
            self.error = f"A cutscene-kódoló nem indul: {e}"
            self.active = False
            return
        self.encoder_name = enc
        tv, ta = tmpl.streams.video[0], tmpl.streams.audio[0]
        v_extradata = vs.codec_context.extradata
        res = av.AudioResampler(format="fltp", layout="stereo", rate=sr, frame_size=1024)
        ring = collections.deque()         # (kind, data, pts, dts, key, dur, t_sec)
        held = []
        out = sv = sa = None
        base_t = 0.0
        holding = False
        seen_key = False                   # a fájl első videócsomagja kulcsképkocka legyen
        t0 = None
        last_vpts = -1
        last_t = None
        a_next = None
        tbv, tba = Fraction(1, fps), Fraction(1, sr)

        def mux(entry):
            nonlocal seen_key
            kind, data, pts, dts, key, dur, t = entry
            if kind == "v" and not seen_key:
                if not key:
                    return
                seen_key = True
            p = av.Packet(data)
            if kind == "v":
                off = round(base_t * fps)
                p.time_base, p.stream = tbv, sv
            else:
                off = round(base_t * sr)
                p.time_base, p.stream = tba, sa
            p.pts, p.dts, p.duration, p.is_keyframe = pts - off, dts - off, dur, key
            if p.pts < 0:
                return
            out.mux(p)
            if kind == "v":
                self.file_seconds = (pts - off) / fps

        def close_file(on_done):
            nonlocal out, holding
            path, secs = self.path, self.file_seconds
            try:
                out.close()
            except Exception:
                log.exception("ring close")
            out = None
            holding = False
            held.clear()
            self.writing = False
            if on_done:
                try:
                    on_done(path, secs)
                except Exception:
                    log.exception("ring on_done")

        def emit(entry):
            ring.append(entry)
            while ring and entry[6] - ring[0][6] > preroll + 2.5:
                ring.popleft()
            if out is not None:
                (held.append(entry) if holding else mux(entry))

        try:
            while True:
                item = q.get()
                cmd = item[0]
                if cmd == "stop":
                    break
                if cmd == "begin":
                    if out is not None:
                        continue
                    now_t = ring[-1][6] if ring else 0.0
                    keys = [e for e in ring if e[0] == "v" and e[4]]
                    start = next((e for e in reversed(keys) if e[6] <= now_t - preroll), keys[0] if keys else None)
                    base_t = start[6] if start else now_t
                    self.path = item[1]
                    out = av.open(self.path, "w", format="mp4", options=self.MOV)
                    sv = out.add_stream_from_template(tv, opaque=True)
                    sv.codec_context.extradata = v_extradata     # Annex B: a muxer maga alakítja át
                    sa = out.add_stream_from_template(ta)
                    self.file_seconds = 0.0
                    seen_key = False
                    self._preroll_used = now_t - base_t
                    self._file_wall = time.monotonic()
                    self.writing = True
                    started = start is None
                    for e in list(ring):
                        if not started:
                            started = e is start
                            if not started:
                                continue
                        if e[6] >= base_t:
                            mux(e)
                    continue
                if cmd == "hold":
                    holding = out is not None
                    continue
                if cmd == "release":
                    for e in held:
                        mux(e)
                    held.clear()
                    holding = False
                    continue
                if cmd == "end":
                    if out is not None:
                        close_file(item[1])
                    continue
                kind, frame, t = item
                if t0 is None:
                    if kind != "v":
                        continue
                    t0 = t
                if last_t is not None and kind == "v" and t - last_t > 1.0:   # szünet a forrásban: a puffer érvénytelen
                    if out is None:
                        ring.clear()
                last_t = t if kind == "v" else last_t
                rel = t - t0
                if kind == "v":
                    if frame.format.name != epix:
                        frame = frame.reformat(format=epix)
                    pts = max(round(rel * fps), last_vpts + 1)
                    last_vpts = pts
                    frame.pts, frame.time_base = pts, tbv
                    for p in vs.encode(frame):
                        emit(("v", bytes(p), p.pts, p.dts if p.dts is not None else p.pts, p.is_keyframe,
                              p.duration or 1, p.pts / fps))
                else:
                    if rel < 0:
                        continue
                    frame.pts = None
                    exp = max(0, round(rel * sr))
                    if a_next is None or exp - a_next > sr // 4:
                        a_next = exp if a_next is None else max(exp, a_next)
                    for f in res.resample(frame):
                        f.pts, f.time_base = a_next, tba
                        a_next += f.samples
                        for p in as_.encode(f):
                            emit(("a", bytes(p), p.pts, p.dts if p.dts is not None else p.pts, True,
                                  p.duration or 1024, p.pts / sr))
        except Exception as e:
            self.error = f"Cutscene-felvételi hiba: {e}"
            log.exception("replay ring")
        finally:
            if out is not None:
                close_file(None)
            self.active = False
            for c in (tmpl, out0):
                try:
                    c.close()
                except Exception:
                    pass


# ====================================================================== figyelő
class CutsceneWatcher:
    """A képet mintavételezi, és a HUD eltűnése / visszatérése alapján vezérli a ReplayRing fájljait."""

    PERIOD = 0.125

    def __init__(self, ring: ReplayRing, make_path, on_start, on_done, is_blocked=lambda: False):
        self.ring, self.make_path, self.on_start, self.on_done = ring, make_path, on_start, on_done
        self.is_blocked = is_blocked            # pl. kézi felvétel folyik: ne indítson
        self.model: HudModel | None = None
        self.armed = False
        self.state = "off"                      # off | learn | watch | rec | weak
        self.stop_delay = 2.0
        self.start_delay = 0.6
        self.count = 0                          # ebben a menetben felvett jelenetek
        self._lock = threading.Lock()
        self._frame = None
        self._ev = threading.Event()
        self._last_submit = 0.0
        self._thread = None
        self._quit = False
        self._reset()

    def _reset(self):
        self._absent_since = None
        self._present_since = None
        self._motion = 0
        self._prev_sig = None
        self._dull_since = None
        self._rec_since = None
        self._holding = False

    def arm(self, model: HudModel, stop_delay=2.0):
        self.disarm()
        self.model, self.stop_delay = model, float(stop_delay)
        self._reset()
        self.state = "learn" if model.learning else "watch"
        self.armed = True
        self._quit = False
        self._thread = threading.Thread(target=self._run, name="cutscene", daemon=True)
        self._thread.start()

    def disarm(self):
        if not self.armed:
            return
        self.armed = False
        self._quit = True
        self._ev.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self.ring.writing:
            self.ring.end_file(self.on_done)
        if self.model:
            self.model.save()
        self.state = "off"

    def submit(self, frame):
        """Bármelyik szálból, minden képkockára hívható: magától ritkít."""
        now = time.monotonic()
        if now - self._last_submit < self.PERIOD:
            return
        self._last_submit = now
        self._frame = frame
        self._ev.set()

    def status(self):
        m = self.model
        return {"state": self.state, "scores": m.scores() if m else [], "count": self.count,
                "learn_left": max(0.0, LEARN_S - m.learned_s) if m else 0.0,
                "rec_s": (time.monotonic() - self._rec_since) if self._rec_since else 0.0}

    # ------------------------------------------------------------------ worker
    def _run(self):
        saved_learn = False
        while not self._quit:
            self._ev.wait(0.5)
            self._ev.clear()
            frame, self._frame = self._frame, None
            if frame is None or self._quit:
                continue
            try:
                self._step(frame, time.monotonic())
                if not saved_learn and self.model and not self.model.learning:
                    saved_learn = True
                    self.model.save()
            except Exception:
                log.exception("cutscene step")

    def _step(self, frame, now):
        m = self.model
        if (frame.width, frame.height) != m.size:
            return
        hud = m.update(frame, now)
        if m.learning:
            self.state = "learn"
            return
        if hud == "weak":
            self.state = "weak"
            return
        mean, sig = frame_stats(frame)
        moved = self._prev_sig is not None and sig.shape == self._prev_sig.shape and \
            float(np.abs(sig - self._prev_sig).mean()) > 0.4
        self._prev_sig = sig
        dull = mean < 10 or not moved                # fekete kép vagy állókép (töltés, szünetmenü)
        recording = self.ring.writing
        if not recording:
            self.state = "watch"
            self._rec_since = None
            if hud != "absent" or self.is_blocked():
                self._absent_since, self._motion = None, 0
                return
            if self._absent_since is None:
                self._absent_since, self._motion = now, 0
            if not dull:
                self._motion += 1
            if now - self._absent_since >= self.start_delay and self._motion >= 2:
                path = self.make_path()
                self.ring.begin_file(path)
                self._rec_since, self._present_since, self._dull_since, self._holding = now, None, None, False
                self.count += 1
                self.state = "rec"
                log.info("cutscene start -> %s (scores %s)", path, m.scores())
                self.on_start(path)
            return
        # ---- felvétel közben
        self.state = "rec"
        if self._rec_since is None:
            self._rec_since = now
        self._dull_since = (self._dull_since or now) if dull else None
        if hud == "present":
            if self._present_since is None:
                self._present_since = now
                self.ring.hold()                     # innentől visszatartjuk: ha tényleg vége, ez már nem kell
                self._holding = True
        elif hud == "absent" and self._present_since is not None:
            self._present_since = None
            self.ring.release()
            self._holding = False
        ended = self._present_since is not None and now - self._present_since >= self.stop_delay
        stuck = self._dull_since is not None and now - self._dull_since >= 15.0
        if ended or stuck or self.is_blocked():
            log.info("cutscene end (%s) after %.1f s", "hud" if ended else "static" if stuck else "blocked",
                     now - self._rec_since)
            self.ring.end_file(self.on_done)
            self._absent_since, self._present_since, self._dull_since, self._rec_since = None, None, None, None
            self._motion, self._holding = 0, False
            self.state = "watch"
