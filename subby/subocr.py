# -*- coding: utf-8 -*-
"""Játékfelirat-olvasó (OCR): a képernyőn látszó angol feliratot olvassa, és a hallás utáni felismeréssel (ASR)
együttműködve ad magyar feliratot.

Munkamegosztás
  * Ha a képen van angol felirat, az az elsődleges: a szöveg a felirat megjelenésekor már megvan (nem kell megvárni
  a mondat végét), pontosabb, mint a hallott, és nem terheli a GPU-t. Ilyenkor a hang utáni felismerés másodlagos:
  a felirattal egy időben elhangzott beszédet kihagyja (Fusion.asr_blocked).
  * Ha nincs felirat a képen (a játék nem ír ki, kikapcsolták, vagy még nincs kijelölve a hely), minden a régi
  módon, a hangból megy.
  * Ha az OCR nem fut (hiba, nincs kép), a hangfelismerés magától átveszi a teljes munkát.
  * Ha mégis mindkettő ugyanazt hallja és látja, a másodikként érkező kimarad (duplikátum-szűrés), és az OCR-ből
  kapott szöveg a hangfelismerés előző-mondat kontextusába is bekerül.

A számoló részek (szövegtisztítás, sorkövetés, összehangolás) csak stdlib-et és numpy-t használnak, az OCR-motor
(rapidocr-onnxruntime) lustán töltődik be – a program enélkül is elindul.
"""

from __future__ import annotations

import difflib
import itertools
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .config import HOME_DIR, valid_profile_id

ROI_DIR = os.path.join(HOME_DIR, "subroi")
DEFAULT_RECT = (0.12, 0.76, 0.76, 0.19)     # (x, y, szélesség, magasság) 0..1 arányban: a kép alsó közepe

MODES = ("auto", "ocr_only", "off")


# ======================================================================= szövegtisztítás
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'",
                         "“": '"', "”": '"', "„": '"', "–": "-", "—": "-",
                         "…": "...", " ": " "})
# az OCR gyakran elhagyja a kisbetűs szavak aposztrófját; csak olyan szavak, amelyek aposztróf nélkül nem szavak
_CONTRACTIONS = {
    "im": "I'm", "ive": "I've", "dont": "don't", "cant": "can't", "didnt": "didn't",
    "doesnt": "doesn't", "isnt": "isn't", "wasnt": "wasn't", "werent": "weren't", "arent": "aren't",
    "couldnt": "couldn't", "wouldnt": "wouldn't", "shouldnt": "shouldn't", "havent": "haven't",
    "hasnt": "hasn't", "hadnt": "hadn't", "youre": "you're", "theyre": "they're", "weve": "we've",
    "youve": "you've", "theyve": "they've", "thats": "that's", "whats": "what's", "theres": "there's",
    "heres": "here's", "wheres": "where's", "youll": "you'll", "theyll": "they'll", "itll": "it'll",
    "aint": "ain't",
}
_RE_WORD = re.compile(r"[A-Za-z]+")
_RE_SPACE = re.compile(r"\s+")
_RE_BAR_ALONE = re.compile(r"(?<![A-Za-z0-9])\|(?![A-Za-z0-9])")
_RE_BAR_APOS = re.compile(r"\|(?=')")
_RE_L_APOS = re.compile(r"(?<![A-Za-z0-9])l(?='(?:m|ll|ve|d)\b)")
_RE_L_ALONE = re.compile(r"(?<![A-Za-z0-9'])l(?![A-Za-z0-9'])")
_RE_FM = re.compile(r"(?<![A-Za-z0-9])[FPl]m(?= [a-z])")
_RE_SPACE_PUNCT = re.compile(r"\s+([,.!?;:])")
_RE_NO_SPACE_AFTER = re.compile(r"(?<=[a-z\)\"'])([.!?])(?=[A-Z])")
_RE_COMMA_LETTER = re.compile(r",(?=[A-Za-z])")
_RE_LEAD = re.compile(r"^[^\w\"'\[(¿¡]+")
_RE_TRAIL = re.compile(r"[^\w.!?,\"')\]]+$")
_RE_KEY = re.compile(r"[^a-z0-9]+")


def _fix_word(m):
    w = m.group(0)
    rep = _CONTRACTIONS.get(w.lower())
    if rep is None or rep == w:
        return w
    if w.isupper() and len(w) > 2:
        return rep.upper()
    if w[0].isupper() and rep[0].islower():
        return rep[0].upper() + rep[1:]
    return rep


def normalize_text(s: str) -> str:
    """Az OCR nyers szövegét olvasható mondattá tisztítja (szóközök, idézőjelek, gyakori betűtévesztések)."""
    s = (s or "").translate(_QUOTES)
    s = "".join(ch for ch in s if ch >= " " or ch in "\t\n")
    s = _RE_SPACE.sub(" ", s).strip()
    s = _RE_BAR_APOS.sub("I", s)
    s = _RE_BAR_ALONE.sub("I", s)
    s = _RE_L_APOS.sub("I", s)
    s = _RE_L_ALONE.sub("I", s)
    s = _RE_FM.sub("I'm", s)
    s = _RE_WORD.sub(_fix_word, s)
    s = _RE_SPACE_PUNCT.sub(r"\1", s)
    s = _RE_NO_SPACE_AFTER.sub(r"\1 ", s)
    s = _RE_COMMA_LETTER.sub(", ", s)
    s = _RE_LEAD.sub("", s)
    s = _RE_TRAIL.sub("", s)
    return _RE_SPACE.sub(" ", s).strip()


def key_text(s: str) -> str:
    """Összehasonlító kulcs: kisbetű, csak betű és szám (a hiányzó szóközök és írásjelek nem számítanak)."""
    return _RE_KEY.sub("", (s or "").lower())


def similarity(a: str, b: str) -> float:
    ka, kb = key_text(a), key_text(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    if sm.real_quick_ratio() < 0.3 or sm.quick_ratio() < 0.3:
        return 0.0
    return sm.ratio()


def is_extension(old: str, new: str) -> bool:
    """Az új szöveg az előzőnek hosszabb folytatása (pl. megjelent a felirat második sora is)."""
    ko, kn = key_text(old), key_text(new)
    return len(kn) >= len(ko) + 6 and len(ko) >= 4 and (kn.startswith(ko) or ko in kn)


def subtitle_like(text: str, conf: float, min_conf: float = 0.6):
    """(rendben?, ok) – alkalmas-e a leolvasott szöveg élő feliratnak (nem zaj, nem HUD-jelölő)."""
    if not text:
        return False, "nincs szöveg"
    if conf < min_conf:
        return False, f"bizonytalan ({conf:.0%})"
    letters = sum(ch.isalpha() for ch in text)
    visible = sum(not ch.isspace() for ch in text)
    if letters < 2:
        return False, "túl rövid"
    if letters < 3 and conf < 0.85:
        return False, "túl rövid és bizonytalan"
    if letters / max(1, visible) < 0.55:
        return False, "kevés betű (nem beszélt szöveg?)"
    if not re.search(r"[A-Za-z]{2}", text):
        return False, "nincs benne szó"
    if len(text) > 400:
        return False, "túl hosszú (nem felirat?)"
    if text[0] in "[(" and text[-1] in "])":
        return False, "hangleírás ([…]) – nem beszéd"
    return True, ""


# ======================================================================= a felirat-sor élete
class LineRef:
    """Egy képernyőfelirat-sor élete: mikor jelent meg, mikor tűnt el, mi a magyar fordítása.
    Az idők time.monotonic() szerintiek. A sor „kész” (on_complete), ha az angol már eltűnt ÉS megvan a magyar –
    ekkor írható a pontos időzítésű felirat (SRT) a felvételhez."""

    _seq = itertools.count(1)

    def __init__(self, en: str, conf: float, start_t: float):
        self.id = next(self._seq)
        self.en, self.conf, self.start_t = en, conf, start_t
        self.end_t: float | None = None
        self.hu: str | None = None
        self.static = False          # állandó szöveg (HUD-felirat): nem beszéd, a hang kihagyásában nem számít
        self.suppressed = False      # a hangból már megjelent ugyanez: nem mutatjuk újra
        self.asr_matched = False     # a hangfelismerés egy mondatát már ennek társítottuk
        self.replaces: LineRef | None = None
        self._cbs = []
        self._lock = threading.Lock()
        self._fired = False

    def on_complete(self, cb):
        """A visszahívás egyszer fut le, amint az angol eltűnt és megvan a magyar (ha már megvan: azonnal)."""
        with self._lock:
            late = self._fired
            if not late:
                self._cbs.append(cb)
        if late:
            try:
                cb(self)
            except Exception:
                pass
        else:
            self._check()

    def set_end(self, t: float):
        with self._lock:
            if self.end_t is None:
                self.end_t = t
        self._check()

    def set_hu(self, hu: str):
        with self._lock:
            if self.hu is None:
                self.hu = hu
        self._check()

    def _check(self):
        with self._lock:
            if self._fired or self.end_t is None or self.hu is None or not self._cbs:
                return
            self._fired = True
            cbs = list(self._cbs)
        for cb in cbs:
            try:
                cb(self)
            except Exception:
                pass

    @property
    def visible(self) -> bool:
        return self.end_t is None

    def __repr__(self):
        return f"<LineRef {self.id} {self.en[:30]!r} {self.start_t:.1f}..{self.end_t}>"


# ======================================================================= a felirat helye (ROI)
def roi_path(profile: str) -> str:
    pid = profile if valid_profile_id(profile) else "altalanos"
    return os.path.join(ROI_DIR, pid + ".json")


def clean_rect(rect):
    """(x, y, w, h) 0..1 arányban -> érvényes, a képen belüli téglalap, vagy None."""
    try:
        x, y, w, h = (float(v) for v in rect)
    except (TypeError, ValueError):
        return None
    if not all(np.isfinite([x, y, w, h])):
        return None
    x, y = min(max(x, 0.0), 0.98), min(max(y, 0.0), 0.98)
    w, h = min(max(w, 0.0), 1.0 - x), min(max(h, 0.0), 1.0 - y)
    if w < 0.05 or h < 0.02:
        return None
    return (x, y, w, h)


def load_roi(profile: str):
    try:
        with open(roi_path(profile), encoding="utf-8") as f:
            data = json.load(f)
        return clean_rect(data.get("rect"))
    except (OSError, ValueError, AttributeError):
        return None


def save_roi(profile: str, rect) -> bool:
    rect = clean_rect(rect)
    if rect is None:
        return False
    os.makedirs(ROI_DIR, exist_ok=True)
    path = roi_path(profile)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rect": [round(v, 5) for v in rect]}, f)
    os.replace(tmp, path)
    return True


def clear_roi(profile: str):
    try:
        os.remove(roi_path(profile))
    except OSError:
        pass


def roi_pixels(rect, width: int, height: int):
    """A 0..1 arányos téglalap képpontban: (x0, y0, x1, y1), legalább 64×24."""
    x, y, w, h = rect
    x0, y0 = int(x * width), int(y * height)
    x1, y1 = int(round((x + w) * width)), int(round((y + h) * height))
    x1, y1 = min(width, max(x1, x0 + 64)), min(height, max(y1, y0 + 24))
    x0, y0 = max(0, min(x0, x1 - 64)), max(0, min(y0, y1 - 24))
    return x0, y0, x1, y1


def luma_crop(frame, box) -> np.ndarray:
    """A képkocka egy téglalapjának fényessége (uint8, másolat), a forrás formátumától függetlenül."""
    x0, y0, x1, y1 = box
    fmt = frame.format.name
    h = frame.height
    p = frame.planes[0]
    if fmt in ("nv12", "yuv420p", "yuvj420p", "yuv422p", "yuv444p", "gray"):
        a = np.frombuffer(p, np.uint8)[: p.line_size * h].reshape(h, p.line_size)
        return a[y0:y1, x0:x1].copy()
    if fmt in ("bgra", "bgr0"):
        a = np.frombuffer(p, np.uint8)[: p.line_size * h].reshape(h, p.line_size)[y0:y1, x0 * 4:x1 * 4]
        a = a.reshape(y1 - y0, x1 - x0, 4).astype(np.uint16)
        return ((a[..., 0] * 29 + a[..., 1] * 150 + a[..., 2] * 77) >> 8).astype(np.uint8)
    g = frame.reformat(format="gray")
    return luma_crop(g, box)


# ======================================================================= OCR-motor
@dataclass
class Box:
    text: str
    conf: float
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class Reading:
    text: str = ""                 # tisztított, összefűzött szöveg (üres: nincs felirat)
    conf: float = 0.0
    lines: list = field(default_factory=list)   # [(sor, biztonság)] a teszt-kiíráshoz
    raw: str = ""
    why: str = ""                  # ha elutasítottuk: miért


def assemble(boxes, min_conf=0.6, min_h=10) -> Reading:
    """Az OCR szövegdobozaiból sorokat rak (fentről lefelé, soronként balról jobbra), és összefűzi őket."""
    good = [b for b in boxes if b.text.strip() and b.conf >= min_conf and (b.y1 - b.y0) >= min_h]
    if not good:
        return Reading(why="nincs elég biztos szöveg")
    good.sort(key=lambda b: (b.y0 + b.y1) / 2)
    rows = []
    for b in good:
        yc, h = (b.y0 + b.y1) / 2, b.y1 - b.y0
        if rows and abs(yc - rows[-1]["yc"]) <= 0.6 * max(h, rows[-1]["h"]):
            r = rows[-1]
            r["items"].append(b)
            r["yc"] = (r["yc"] * (len(r["items"]) - 1) + yc) / len(r["items"])
            r["h"] = max(r["h"], h)
        else:
            rows.append({"yc": yc, "h": h, "items": [b]})
    lines, num, den = [], 0.0, 0
    for r in rows:
        items = sorted(r["items"], key=lambda b: b.x0)
        txt = " ".join(i.text.strip() for i in items)
        w = sum(max(1, len(i.text)) for i in items)
        c = sum(i.conf * max(1, len(i.text)) for i in items) / w
        lines.append((txt, c))
        num += c * w
        den += w
    raw = " ".join(t for t, _ in lines)
    return Reading(text=normalize_text(raw), conf=num / max(1, den), lines=lines, raw=raw)


class RapidOcrBackend:
    """rapidocr-onnxruntime (PP-OCR, ONNX, CPU): a modellek a csomaggal jönnek, nincs letöltés és nem kell GPU."""

    name = "RapidOCR"

    def __init__(self, det_side: int = 960, threads: int = 0):
        from rapidocr_onnxruntime import RapidOCR      # ImportError: a hívó jelzi a felhasználónak

        self.lock = threading.Lock()
        n = int(threads) if threads else max(2, min(4, (os.cpu_count() or 4) // 2))
        base = dict(det_limit_type="max", det_limit_side_len=int(det_side), use_cls=False, use_angle_cls=False,
                    det_intra_op_num_threads=n, rec_intra_op_num_threads=n, cls_intra_op_num_threads=1)
        plain = {k: v for k, v in base.items() if "num_threads" not in k}
        old_api = dict(plain, det_model_path=None, cls_model_path=None, rec_model_path=None)
        err = None
        for kw in (base, plain, old_api, {}):
            try:
                self.engine = RapidOCR(**kw)
                break
            except Exception as e:                     # más csomagverzió, más paraméternevek
                err = e
        else:
            raise err
        self.engine(np.zeros((64, 256), np.uint8))     # bemelegítés: a modellek betöltődnek

    def read(self, gray: np.ndarray) -> list:
        with self.lock:
            res, _ = self.engine(gray)
        out = []
        for item in res or []:
            pts, text, score = item[0], item[1], item[2]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            out.append(Box(str(text), float(score), min(xs), min(ys), max(xs), max(ys)))
        return out


_shared = None
_shared_lock = threading.Lock()


def shared_backend() -> RapidOcrBackend:
    """Egyetlen közös OCR-motor a feliratozónak és a kijelölő ablak próbaolvasásának."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = RapidOcrBackend()
        return _shared


def read_text(gray: np.ndarray, backend=None, min_conf: float = 0.6) -> Reading:
    """Egy képrészlet egyszeri olvasása (a kijelölő ablak „Próba” gombjához)."""
    backend = backend or shared_backend()
    rd = assemble(backend.read(gray), min_conf)
    if rd.text:
        ok, why = subtitle_like(rd.text, rd.conf, min_conf)
        if not ok:
            rd.why = why
    return rd


# ======================================================================= sorkövetés
class LineTracker:
    """A másodpercenként néhányszor leolvasott szövegből „felirat-sorokat” csinál: egy sor akkor indul, ha a szöveg
    legalább confirm_n egymás utáni olvasásban ugyanaz (a halványodó, félig látszó szöveg nem számít), és akkor ér
    véget, ha gone_s másodpercig nincs a képen. A kisebb olvasási hibák (hasonlóság ≥ same) ugyanannak a sornak
    számítanak. Ami static_s másodpercnél tovább változatlanul látszik, az nem beszéd, hanem HUD-szöveg:
    ignore_s ideig figyelmen kívül marad.

    feed(t, text, conf) -> [("start", ref) | ("end", ref, end_t)]; a végi esemény sora a start előtt jön, ha a új
    szöveg váltja a régit."""

    def __init__(self, confirm_n=2, gone_s=0.5, same=0.78, static_s=14.0, ignore_s=600.0):
        self.confirm_n, self.gone_s, self.same = confirm_n, gone_s, same
        self.static_s, self.ignore_s = static_s, ignore_s
        self.active: LineRef | None = None
        self.cand = None
        self.gone_since = None
        self.last_seen = 0.0
        self.ignored = []             # [(szöveg, lejárat)]

    def reset(self):
        self.active, self.cand, self.gone_since = None, None, None

    def _is_ignored(self, text, t):
        self.ignored = [(k, exp) for k, exp in self.ignored if exp > t]
        return any(similarity(text, k) >= 0.85 for k, _ in self.ignored)

    def _end_gone(self, t, ev):
        act = self.active
        if act is not None and self.gone_since is not None and t - self.gone_since >= self.gone_s - 1e-6:
            ev.append(("end", act, (self.last_seen + self.gone_since) / 2.0))
            self.active, self.gone_since = None, None

    def feed(self, t, text, conf=1.0):
        ev = []
        text = text or ""
        if text and self._is_ignored(text, t):
            text = ""
        act = self.active
        if not text:
            self.cand = None
            if act is not None:
                if self.gone_since is None:
                    self.gone_since = t
                self._end_gone(t, ev)
            return ev
        if act is not None:
            ka, kt = key_text(act.en), key_text(text)
            grew = is_extension(act.en, text)      # megjelent a felirat folytatása (pl. a második sor)
            if not grew and (similarity(text, act.en) >= self.same or (len(kt) >= 6 and kt in ka)):
                # ugyanaz a felirat (vagy egy részlete, pl. a második sor már eltűnt): él tovább
                self.gone_since, self.cand = None, None
                self.last_seen = t
                if t - act.start_t >= self.static_s:
                    act.static = True
                    self.ignored.append((act.en, t + self.ignore_s))
                    ev.append(("end", act, t))
                    self.active = None
                return ev
            if grew:
                self.last_seen, self.gone_since = t, None     # a régi szöveg benne van az újban: még látszik
            elif self.gone_since is None:                     # más szöveg látszik: a régi nincs a képen
                self.gone_since = t
        c = self.cand
        if c is not None and similarity(text, c["text"]) >= self.same:
            c["n"] += 1
            if (conf, len(text)) > (c["conf"], len(c["text"])):
                c["text"], c["conf"] = text, conf          # a legjobb olvasatot tartjuk meg
        else:
            c = self.cand = {"text": text, "conf": conf, "first": t, "n": 1}
        if c["n"] >= self.confirm_n:
            prev = self.active
            if prev is not None:
                ev.append(("end", prev, c["first"]))
            ref = LineRef(c["text"], c["conf"], c["first"])
            if prev is not None and is_extension(prev.en, ref.en):
                ref.replaces = prev
            self.active, self.cand, self.gone_since, self.last_seen = ref, None, None, t
            ev.append(("start", ref))
        else:
            self._end_gone(t, ev)
        return ev

    def flush(self, t):
        ev = []
        if self.active is not None:
            ev.append(("end", self.active, t))
        self.reset()
        return ev


# ======================================================================= összehangolás a hangfelismeréssel
class Fusion:
    """A képernyőfelirat (OCR) és a hallás utáni felismerés (ASR) közös nyilvántartása.

    asr_blocked(t0, t1): a t0..t1 közötti beszédet lefedi egy képernyőfelirat, a hang utáni felismerés kihagyhatja
    (ez a „másodlagos, szükség esetén mellőzhető” viselkedés). Csak akkor igaz, ha az OCR él és olvas:
    különben a hangfelismerés mindent maga csinál."""

    LEAD_S = 0.6        # a felirat ennyivel a beszéd vége UTÁN is megjelenhet (kép–hang eltolódás)
    TAIL_S = 1.2        # a felirat eltűnése után ennyi ideig még hozzá tartozik a beszéd vége
    DUP_BEFORE_S = 2.0  # ugyanaz a mondat a képen és a hangban: ennyi időn belül számít egynek
    DUP_AFTER_S = 8.0
    DUP_SIM = 0.62
    STALE_S = 4.0       # ennyi ideje nem érkezett olvasás: az OCR-t nem tekintjük élőnek

    def __init__(self, cfg, clock=time.monotonic):
        self.cfg, self.clock = cfg, clock
        self._lock = threading.Lock()
        self.refs = deque(maxlen=24)
        self.asr = deque(maxlen=24)             # [t, szöveg, társítva]
        self.alive = False
        self.seen_t = 0.0

    # ---------------------------------------------------------------- az OCR jelzései
    def set_alive(self, alive: bool):
        self.alive = bool(alive)
        if alive:
            self.seen_t = self.clock()

    def touch(self):
        self.alive = True
        self.seen_t = self.clock()

    def healthy(self, now=None) -> bool:
        now = self.clock() if now is None else now
        return self.alive and now - self.seen_t < self.STALE_S

    def note_ocr(self, ref: LineRef):
        with self._lock:
            self.refs.append(ref)

    def note_asr(self, text: str, t=None):
        with self._lock:
            self.asr.append([self.clock() if t is None else t, text, False])

    # ---------------------------------------------------------------- döntések
    def mode(self) -> str:
        m = getattr(self.cfg, "subocr_mode", "auto")
        return m if m in MODES else "auto"

    def asr_blocked(self, t0: float, t1: float | None = None) -> bool:
        mode = self.mode()
        if mode == "off":
            return False
        now = self.clock()
        if not self.healthy(now):
            return False
        if mode == "ocr_only":
            return True
        t1 = now if t1 is None else max(t1, t0)
        with self._lock:
            for ref in self.refs:
                if ref.static:
                    continue
                hi = float("inf") if ref.end_t is None else ref.end_t + self.TAIL_S
                if ref.start_t - self.LEAD_S <= t1 and hi >= t0:
                    return True
        return False

    def asr_duplicate(self, text: str, t=None) -> bool:
        """A hangból épp kész mondatot a képernyőfelirat már megmutatta-e?"""
        if self.mode() == "off" or not self.healthy():
            return False
        t = self.clock() if t is None else t
        with self._lock:
            for ref in self.refs:
                if ref.asr_matched or ref.static:
                    continue
                if -self.DUP_BEFORE_S <= t - ref.start_t <= self.DUP_AFTER_S and \
                        similarity(text, ref.en) >= self.DUP_SIM:
                    ref.asr_matched = True
                    return True
        return False

    def ocr_duplicate(self, ref: LineRef) -> bool:
        """A képernyőfelirat sorát a hangból már megmutattuk-e?"""
        with self._lock:
            for item in self.asr:
                if item[2]:
                    continue
                if -self.DUP_BEFORE_S <= item[0] - ref.start_t <= self.DUP_AFTER_S and \
                        similarity(item[1], ref.en) >= self.DUP_SIM:
                    item[2] = True
                    ref.asr_matched = True
                    return True
        return False


# ======================================================================= az olvasó
class SubtitleOcr:
    """A képkockákból mintát vesz (másodpercenként subocr_fps-szer, a kijelölt téglalapból), egy munkaszálban
    olvassa, a sorkövetővel felirat-sorokat készít, és minden új sorról értesíti az emit(ref) visszahívást
    (az fordít és kijelez). A video szál csak a másolatot készíti el (offer): sosem várakozik az OCR-re."""

    def __init__(self, cfg, fusion: Fusion, emit, log=print, backend_factory=None, clock=time.monotonic):
        self.cfg, self.fusion, self.emit, self.log = cfg, fusion, emit, log
        self.backend_factory = backend_factory or shared_backend
        self.clock = clock
        self.tracker = LineTracker()
        self.backend = None
        self.error = ""
        self.roi = None
        self._job = None
        self._working = False
        self._next_offer = 0.0
        self._last_proc = 0.0
        self._ev = threading.Event()
        self._quit = False
        self._thread = None
        self._fails = 0
        # állapotkijelzés
        self.reads = 0
        self.lines = 0
        self.read_ms = 0.0
        self.last_text = ""
        self.last_why = ""
        self.last_read_t = 0.0

    # ---------------------------------------------------------------- vezérlés
    def set_roi(self, rect):
        self.roi = clean_rect(rect) if rect else None
        if self.roi is None:
            self._flush(self.clock())

    def start(self):
        if self._thread is not None:
            return
        self._quit = False
        self._thread = threading.Thread(target=self._run, name="felirat-ocr", daemon=True)
        self._thread.start()

    def stop(self):
        self._quit = True
        self._ev.set()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout=4)
        self._flush(self.clock())
        self.fusion.set_alive(False)

    def mode(self) -> str:
        return self.fusion.mode()

    def interval(self) -> float:
        try:
            fps = float(self.cfg.subocr_fps)
        except (AttributeError, TypeError, ValueError):
            fps = 4.0
        return 1.0 / min(10.0, max(1.0, fps))

    # ---------------------------------------------------------------- bemenet (a video szálból)
    def offer(self, frame):
        """Minden képkockára hívható; magától ritkít, és csak a kijelölt téglalapot másolja ki."""
        now = self.clock()
        if now < self._next_offer or self._working or self._job is not None:
            return
        roi = self.roi
        if roi is None or self.mode() == "off" or self.error or self._thread is None:
            return
        self._next_offer = now + self.interval()
        try:
            crop = luma_crop(frame, roi_pixels(roi, frame.width, frame.height))
        except Exception as e:                       # szokatlan képformátum: ne dőljön össze a video szál
            self.error = f"képformátum: {e}"
            return
        self._job = (now, crop)
        self._ev.set()

    def offer_gray(self, gray, t=None):
        """Kész szürkeárnyalatos képrészlet beadása (tesztekhez, kijelölő ablakhoz)."""
        self._job = (self.clock() if t is None else t, gray)
        self._ev.set()

    # ---------------------------------------------------------------- munkaszál
    def _run(self):
        while not self._quit:
            self._ev.wait(0.5)
            self._ev.clear()
            if self._quit:
                break
            now = self.clock()
            if self.mode() == "off" or self.roi is None:
                self._job = None
                self._flush(now)
                continue
            if self.backend is None and not self._load_backend():
                return
            job, self._job = self._job, None
            if job is None:
                # nem érkezik kép (nincs jel): a nyitott felirat-sort lezárjuk, az OCR „nem él”
                if self.tracker.active is not None and now - self._last_proc > 3.0:
                    self._flush(now)
                continue
            self._working = True
            try:
                self._process(*job)
            except Exception as e:               # a munkaszál semmi miatt nem állhat le
                self.log(f"[felirat-olvasó] hiba: {e!r}")
            finally:
                self._working = False

    def _load_backend(self) -> bool:
        try:
            self.backend = self.backend_factory()
            self.log(f"[felirat-olvasó] {getattr(self.backend, 'name', 'OCR')} betöltve")
            return True
        except ImportError:
            self.error = ("hiányzik az OCR-csomag (rapidocr-onnxruntime) – telepítsd: "
                          "python -m pip install rapidocr-onnxruntime")
        except Exception as e:
            self.error = f"az OCR nem indult el: {e}"
        self.fusion.set_alive(False)
        self.log(f"[felirat-olvasó] {self.error}")
        return False

    def _process(self, t, gray):
        t0 = time.perf_counter()
        try:
            rd = assemble(self.backend.read(gray), self._min_conf())
            self._fails = 0
        except Exception as e:
            self._fails += 1
            self.last_why = f"olvasási hiba: {e}"
            if self._fails >= 5:
                self.error = f"az OCR többször hibázott: {e}"
                self.fusion.set_alive(False)
                self.log(f"[felirat-olvasó] {self.error}")
            return
        ms = (time.perf_counter() - t0) * 1000
        self.read_ms = ms if not self.reads else self.read_ms * 0.8 + ms * 0.2
        self.reads += 1
        self._last_proc = self.clock()
        self.last_read_t = self._last_proc
        self.fusion.touch()
        text, conf = "", 0.0
        if rd.text:
            ok, why = subtitle_like(rd.text, rd.conf, self._min_conf())
            self.last_why = why
            if ok:
                text, conf = rd.text, rd.conf
        else:
            self.last_why = rd.why
        self.last_text = text
        self.tracker.static_s = self._cfg_float("subocr_static_s", 14.0, 6.0, 120.0)
        self._dispatch(self.tracker.feed(t, text, conf), t)

    def _min_conf(self) -> float:
        return self._cfg_float("subocr_min_conf", 0.6, 0.3, 0.95)

    def _cfg_float(self, name, default, lo, hi):
        try:
            return min(hi, max(lo, float(getattr(self.cfg, name))))
        except (AttributeError, TypeError, ValueError):
            return default

    def _flush(self, t):
        self._dispatch(self.tracker.flush(t), t)

    def _dispatch(self, events, t):
        for ev in events:
            if ev[0] == "end":
                _, ref, end_t = ev
                ref.set_end(end_t)
            else:
                ref = ev[1]
                self._on_start(ref)

    def _on_start(self, ref: LineRef):
        self.fusion.note_ocr(ref)
        self.log(f"[felirat-olvasó] új felirat ({ref.conf:.0%}): {ref.en}")
        if self.mode() == "auto" and self.fusion.ocr_duplicate(ref):
            ref.suppressed = True
            ref.set_hu(ref.en)
            self.log("[felirat-olvasó] a hangból már megjelent, kihagyva")
            return
        self.lines += 1
        try:
            self.emit(ref)
        except Exception as e:
            self.log(f"[felirat-olvasó] hiba a felirat kiadásakor: {e!r}")

    # ---------------------------------------------------------------- állapot
    def status(self) -> dict:
        mode = self.mode()
        act = self.tracker.active
        if mode == "off":
            state = "off"
        elif self.error:
            state = "error"
        elif self.roi is None:
            state = "no_roi"
        elif self.backend is None:
            state = "loading"
        elif act is not None:
            state = "reading"
        else:
            state = "watching"
        return {"state": state, "mode": mode, "error": self.error, "text": act.en if act is not None else "",
                "last": self.last_text, "why": self.last_why, "reads": self.reads, "lines": self.lines,
                "ms": round(self.read_ms), "roi": self.roi}
