# -*- coding: utf-8 -*-
"""A játékfelirat-olvasó (OCR) és az összehangolás a hangfelismeréssel – GPU, kártya és hálózat nélkül.
Futtatás: python -m unittest discover -s tests"""

import concurrent.futures
import os
import queue
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP_HOME = tempfile.mkdtemp(prefix="rksubby-test-ocr-")
os.environ.setdefault("RKSUBBY_HOME", _TMP_HOME)       # a valódi beállításokhoz nem nyúlunk

import numpy as np  # noqa: E402

from subby import subocr  # noqa: E402
from subby.config import Config  # noqa: E402
from subby.display import SubtitleSink  # noqa: E402
from subby.subocr import (Box, Fusion, LineRef, LineTracker, SubtitleOcr, assemble, is_extension,  # noqa: E402
                          normalize_text, similarity, subtitle_like)


def make_cfg(**changes):
    c = Config(path=os.path.join(_TMP_HOME, "config.json"), load=False)
    c.update(dict({"log_enabled": False}, **changes))
    return c


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ----------------------------------------------------------------------------------------------- szöveg
class TextTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_text("  Get in  the car , now !  "), "Get in the car, now!")
        self.assertEqual(normalize_text("l'm sure | don't know"), "I'm sure I don't know")
        self.assertEqual(normalize_text("Fm coming back.Then we go"), "I'm coming back. Then we go")
        self.assertEqual(normalize_text("Jason: Hold on, Im coming!"), "Jason: Hold on, I'm coming!")
        self.assertEqual(normalize_text("I dont care, THEYRE here"), "I don't care, THEY'RE here")
        self.assertEqual(normalize_text("\u201cWhat\u2019s that?\u201d"), "\"What's that?\"")
        self.assertEqual(normalize_text("It's 5 pm, l think."), "It's 5 pm, I think.")
        self.assertEqual(normalize_text("Wait... what?"), "Wait... what?")
        self.assertEqual(normalize_text(""), "")

    def test_similarity_ignores_spaces_and_case(self):
        self.assertEqual(similarity("The whole damn city", "thewhole DAMN city!"), 1.0)
        self.assertGreater(similarity("Get in the car, Lucia.", "Get in the car Lucia"), 0.95)
        self.assertLess(similarity("Get in the car", "Where is the money"), 0.5)
        self.assertEqual(similarity("", "abc"), 0.0)

    def test_extension(self):
        self.assertTrue(is_extension("Get in the car.", "Get in the car. We don't have much time."))
        self.assertFalse(is_extension("Get in the car.", "Where is the money?"))
        self.assertFalse(is_extension("Get in the car.", "Get in the car now."))      # túl kicsi a bővülés

    def test_subtitle_like(self):
        self.assertTrue(subtitle_like("Get in the car, Lucia.", 0.9)[0])
        self.assertTrue(subtitle_like("Yeah.", 0.75)[0])
        self.assertTrue(subtitle_like("No.", 0.9)[0])
        self.assertFalse(subtitle_like("No.", 0.7)[0])                   # túl rövid és bizonytalan
        self.assertFalse(subtitle_like("Get in the car", 0.4)[0])
        self.assertFalse(subtitle_like("[engine revving]", 0.95)[0])     # hangleírás
        self.assertFalse(subtitle_like("12:45 PM $4,500", 0.95)[0])      # HUD-számok
        self.assertFalse(subtitle_like("", 0.9)[0])

    def test_assemble_orders_lines_and_filters(self):
        boxes = [Box("the whole damn city!", 0.9, 400, 90, 800, 130),
                 Box("We need to get to the airport", 0.9, 300, 40, 900, 80),
                 Box("junk", 0.2, 0, 0, 50, 30),
                 Box("lock down", 0.88, 600, 40, 700, 80)]           # ugyanabban a sorban, jobbra
        rd = assemble(boxes, 0.6)
        self.assertEqual(rd.text, "We need to get to the airport lock down the whole damn city!")
        self.assertEqual(len(rd.lines), 2)
        self.assertEqual(assemble([Box("x", 0.1, 0, 0, 10, 30)], 0.6).text, "")


# ----------------------------------------------------------------------------------------------- sorkövető
def feed_seq(tr, seq, start=0.0, step=0.25):
    """seq: szövegek (vagy "") sorozata; visszaadja az összes eseményt (idő, esemény)."""
    out, t = [], start
    for text in seq:
        for ev in tr.feed(t, text, 0.9 if text else 0.0):
            out.append((t, ev))
        t += step
    return out


def kinds(events):
    return [ev[0] for _, ev in events]


class TrackerTests(unittest.TestCase):
    A = "Get in the car, Lucia. We don't have much time."
    B = "Where is the money you promised me?"

    def test_confirm_after_two_reads_and_end_after_gone(self):
        tr = LineTracker()
        ev = feed_seq(tr, [self.A, self.A, self.A, self.A, "", "", "", ""])
        self.assertEqual(kinds(ev), ["start", "end"])
        t_start, (_, ref) = ev[0]
        self.assertEqual(t_start, 0.25)                    # a 2. egyező olvasásnál indul
        self.assertEqual(ref.start_t, 0.0)                 # de az első látás idejétől számít
        t_end, (_, ref2, end_t) = ev[1]
        self.assertIs(ref, ref2)
        self.assertAlmostEqual(end_t, (0.75 + 1.0) / 2)    # az utolsó látás és az első üres olvasás között
        self.assertEqual(t_end, 1.5)                       # gone_s = 0.5 s üres olvasás után zár

    def test_single_read_is_not_a_line(self):
        tr = LineTracker()
        self.assertEqual(feed_seq(tr, [self.A, "", self.A, "", self.B, ""]), [])

    def test_fading_text_with_misreads_stays_one_line(self):
        tr = LineTracker()
        misread = "Get in the car, Luca. We dont have much tlme."
        ev = feed_seq(tr, [self.A, self.A, misread, self.A, misread, self.A, "", "", ""])
        self.assertEqual(kinds(ev), ["start", "end"])

    def test_noise_never_confirms(self):
        tr = LineTracker()
        noise = ["Gxt in", "The cqr Lu", "ffff", "Get i", "ther", "mmm"]
        self.assertEqual(feed_seq(tr, noise * 2), [])

    def test_new_line_replaces_old(self):
        tr = LineTracker()
        ev = feed_seq(tr, [self.A, self.A, self.A, self.B, self.B, self.B, "", "", ""])
        self.assertEqual(kinds(ev), ["start", "end", "start", "end"])
        first, ended, second = ev[0][1][1], ev[1][1][1], ev[2][1][1]
        self.assertIs(first, ended)
        self.assertEqual(second.en, self.B)
        self.assertIsNone(second.replaces)                 # nem bővítés
        self.assertAlmostEqual(ev[1][1][2], 0.75)          # a régi addig tart, amíg az új először megjelent

    def test_second_line_appearing_extends_and_replaces(self):
        tr = LineTracker()
        longer = self.A + " Move it!"
        ev = feed_seq(tr, [self.A, self.A, self.A, longer, longer, longer, "", "", ""])
        starts = [e[1][1] for e in ev if e[1][0] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertIs(starts[1].replaces, starts[0])

    def test_shorter_fragment_is_same_line(self):
        tr = LineTracker()
        longer = self.A + " Move it, move it!"
        ev = feed_seq(tr, [longer, longer, self.A, self.A, "Move it, move it!", "", "", ""])
        self.assertEqual(kinds(ev), ["start", "end"])

    def test_static_text_is_ignored_afterwards(self):
        tr = LineTracker(static_s=3.0, ignore_s=60.0)
        hud = "Follow Lucia to the safehouse"
        ev = feed_seq(tr, [hud] * 20)                      # 5 mp-ig változatlan
        self.assertEqual(kinds(ev), ["start", "end"])
        ref = ev[0][1][1]
        self.assertTrue(ref.static)
        self.assertEqual(feed_seq(tr, [hud] * 6, start=5.0), [])    # tovább nem indul újra
        # más szöveg viszont igen
        self.assertEqual(kinds(feed_seq(tr, [self.B] * 3, start=7.0)), ["start"])

    def test_flush_ends_active_line(self):
        tr = LineTracker()
        feed_seq(tr, [self.A, self.A])
        ev = tr.flush(9.0)
        self.assertEqual([e[0] for e in ev], ["end"])
        self.assertEqual(ev[0][2], 9.0)
        self.assertIsNone(tr.active)


# ----------------------------------------------------------------------------------------------- LineRef
class LineRefTests(unittest.TestCase):
    def test_complete_needs_end_and_hu(self):
        calls = []
        ref = LineRef("Get in", 0.9, 10.0)
        ref.on_complete(calls.append)
        ref.set_end(12.0)
        self.assertEqual(calls, [])
        ref.set_hu("Szállj be")
        self.assertEqual(calls, [ref])
        ref.set_hu("másik")
        ref.set_end(13.0)
        self.assertEqual(len(calls), 1)                    # egyszer fut
        self.assertEqual((ref.hu, ref.end_t), ("Szállj be", 12.0))

    def test_hu_first_then_end_and_late_registration(self):
        calls = []
        ref = LineRef("Get in", 0.9, 10.0)
        ref.set_hu("Szállj be")
        ref.on_complete(lambda r: calls.append("a"))
        self.assertEqual(calls, [])
        ref.set_end(11.0)
        self.assertEqual(calls, ["a"])
        ref.on_complete(lambda r: calls.append("late"))    # már kész: azonnal fut
        self.assertEqual(calls, ["a", "late"])


# ----------------------------------------------------------------------------------------------- Fusion
class FusionTests(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self.cfg = make_cfg()
        self.f = Fusion(self.cfg, clock=self.clk)

    def line(self, start, end=None, text="Get in the car, Lucia."):
        ref = LineRef(text, 0.9, start)
        if end is not None:
            ref.set_end(end)
        self.f.note_ocr(ref)
        return ref

    def test_not_blocking_when_ocr_not_alive(self):
        self.line(995.0, None)
        self.assertFalse(self.f.asr_blocked(996.0, 998.0))     # az OCR nem él: a hang mindent maga csinál
        self.f.set_alive(True)
        self.assertTrue(self.f.asr_blocked(996.0, 998.0))
        self.clk.t += 10                                        # nem jön több olvasás: elavult
        self.assertFalse(self.f.asr_blocked(996.0, 998.0))

    def test_overlap_rules(self):
        self.f.set_alive(True)
        self.line(1000.0, 1004.0)                               # 1000..1004 látszik
        self.clk.t = 1005.0
        self.f.touch()
        self.assertTrue(self.f.asr_blocked(1001.0, 1003.0))     # beleesik
        self.assertTrue(self.f.asr_blocked(999.5, 1000.2))      # a beszéd kicsivel a felirat előtt kezdődik
        self.assertTrue(self.f.asr_blocked(1004.5, 1005.0))     # utóidő
        self.assertFalse(self.f.asr_blocked(1006.0, 1007.0))    # már nincs lefedve
        self.assertFalse(self.f.asr_blocked(990.0, 991.0))      # korábbi beszéd

    def test_open_line_blocks_until_end(self):
        self.f.set_alive(True)
        ref = self.line(1000.0, None)
        self.clk.t = 1003.0
        self.f.touch()
        self.assertTrue(self.f.asr_blocked(1002.0))
        ref.set_end(1003.0)
        self.clk.t = 1010.0
        self.f.touch()
        self.assertFalse(self.f.asr_blocked(1008.0, 1009.0))

    def test_static_text_does_not_block(self):
        self.f.set_alive(True)
        ref = self.line(1000.0, None)
        ref.static = True
        self.assertFalse(self.f.asr_blocked(1001.0, 1002.0))

    def test_modes(self):
        self.f.set_alive(True)
        self.cfg.update({"subocr_mode": "ocr_only"})
        self.assertTrue(self.f.asr_blocked(1.0, 2.0))           # felirat nélkül is: a hangot nem fordítjuk
        self.cfg.update({"subocr_mode": "off"})
        self.line(1000.0, None)
        self.assertFalse(self.f.asr_blocked(1001.0, 1002.0))
        self.f.set_alive(False)
        self.cfg.update({"subocr_mode": "ocr_only"})
        self.assertFalse(self.f.asr_blocked(1.0, 2.0))          # az OCR nem él: a hang átveszi

    def test_asr_duplicate_is_one_to_one(self):
        self.f.set_alive(True)
        ref = self.line(1000.0, 1003.0, "Get in the car, Lucia.")
        self.clk.t = 1002.0
        self.assertTrue(self.f.asr_duplicate("get in the car lucia"))
        self.assertTrue(ref.asr_matched)
        self.assertFalse(self.f.asr_duplicate("get in the car lucia"))      # egy felirat egyszer párosítható
        ref2 = self.line(1001.0, None, "Where is the money?")
        self.assertFalse(self.f.asr_duplicate("Completely different words here"))
        self.assertFalse(ref2.asr_matched)

    def test_ocr_duplicate_of_recent_asr_line(self):
        self.f.set_alive(True)
        self.f.note_asr("Get in the car Lucia", t=1000.5)
        ref = LineRef("Get in the car, Lucia.", 0.9, 1000.0)
        self.assertTrue(self.f.ocr_duplicate(ref))
        self.assertFalse(self.f.ocr_duplicate(LineRef("Get in the car, Lucia.", 0.9, 1001.0)))   # már elhasznált
        other = LineRef("Where is the money?", 0.9, 1000.0)
        self.assertFalse(self.f.ocr_duplicate(other))
        self.f.note_asr("Get in the car Lucia", t=1100.0)                  # túl régen/máskor
        self.assertFalse(self.f.ocr_duplicate(LineRef("Get in the car, Lucia.", 0.9, 1000.0)))


# ----------------------------------------------------------------------------------------------- kijelző
class SinkOcrTests(unittest.TestCase):
    def make(self, **cfg):
        return SubtitleSink(make_cfg(**cfg), log_path="", echo=lambda *a: None)

    def test_hungarian_stays_while_english_is_visible(self):
        sink = self.make(subocr_linger_s=0.4)
        ref = LineRef("Get in the car", 0.9, time.monotonic() - 1.0)
        sink.final("Szállj be az autóba", ref.en, 0, 300, 900, ref=ref)
        ln = sink.lines[0]
        late = time.monotonic() + 9.0                          # jóval a hangból számolt idő után
        self.assertFalse(sink._alive({"until": ln["until"]}, late))          # a hangból jövő sor lejárt volna
        self.assertTrue(sink._alive(ln, late))                 # az OCR-sor él, amíg az angol látszik
        ref.set_end(late)
        self.assertTrue(sink._alive(ln, late + 0.3))
        self.assertFalse(sink._alive(ln, late + 0.5))          # end_t + utóidő után lejár
        # nagyon hosszú ideig látszó (be nem záruló) angolnál is van felső korlát
        open_ref = LineRef("Stuck", 0.9, time.monotonic())
        self.assertTrue(sink._alive(dict(ln, ref=open_ref, hard=late + 1), late))
        self.assertFalse(sink._alive(dict(ln, ref=open_ref, hard=late - 1), late))

    def test_replaced_line_is_removed(self):
        sink = self.make(max_lines=2)
        t0 = time.monotonic()
        a = LineRef("Get in the car.", 0.9, t0)
        sink.final("Szállj be.", a.en, 0, 1, 1, ref=a)
        b = LineRef("Get in the car. We don't have much time.", 0.9, t0 + 1)
        b.replaces = a
        sink.final("Szállj be. Nincs sok időnk.", b.en, 0, 1, 1, ref=b)
        self.assertEqual([ln["hu"] for ln in sink.state()["lines"]], ["Szállj be. Nincs sok időnk."])
        self.assertEqual(sink.history_list()[-1]["src"], "ocr")

    def test_srt_cue_has_exact_screen_timing(self):
        tmp = tempfile.mkdtemp(prefix="rksubby-srt-")
        sink = self.make()
        from subby.display import SrtWriter

        sink.srt = SrtWriter(tmp)
        t0 = sink.srt.t0
        ref = LineRef("Get in the car", 0.9, t0 + 10.0)
        sink.final("Szállj be az autóba", ref.en, 0, 300, 900, ref=ref)
        with open(sink.srt.path, encoding="utf-8-sig") as f:
            self.assertEqual(f.read(), "")                     # az angol még látszik: nincs kész sor
        ref.set_end(t0 + 13.5)
        sink.stop_srt()
        with open(sink.srt.path if sink.srt else os.path.join(tmp, os.listdir(tmp)[0]), encoding="utf-8-sig") as f:
            txt = f.read()
        self.assertIn("00:00:10,000 --> 00:00:13,500", txt)
        self.assertIn("Szállj be az autóba", txt)

    def test_asr_path_unchanged(self):
        sink = self.make(max_lines=2)
        sink.final("hu0", "en0", 100, 200, 600)
        self.assertEqual([ln["hu"] for ln in sink.state()["lines"]], ["hu0"])
        self.assertNotIn("ref", sink.lines[0])
        self.assertEqual(sink.history_list()[-1]["src"], "asr")


# ----------------------------------------------------------------------------------------------- olvasó
class FakeBackend:
    """Előre megadott szövegeket „olvas”: képkockánként a következőt (üres lista: nincs szöveg)."""
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.shapes = []

    def read(self, gray):
        self.calls += 1
        self.shapes.append(gray.shape)
        item = self.script.pop(0) if self.script else ""
        if not item:
            return []
        return [Box(item, 0.93, 20, 20, 600, 60)]


def make_ocr(script, cfg=None, clk=None, **kw):
    cfg = cfg or make_cfg()
    clk = clk or Clock()
    fusion = Fusion(cfg, clock=clk)
    emitted = []
    ocr = SubtitleOcr(cfg, fusion, emitted.append, log=lambda *a: None,
                      backend_factory=lambda: FakeBackend(script), clock=clk)
    ocr.backend = ocr.backend_factory()
    return ocr, fusion, emitted, clk


class OcrReaderTests(unittest.TestCase):
    A = "Get in the car, Lucia. We don't have much time."

    def run_script(self, ocr, clk, n, step=0.25):
        img = np.zeros((60, 600), np.uint8)
        for _ in range(n):
            ocr._process(clk.t, img)
            clk.t += step

    def test_line_emitted_once_and_fusion_alive(self):
        ocr, fusion, emitted, clk = make_ocr([self.A] * 6 + [""] * 3)
        ocr.set_roi((0.1, 0.8, 0.8, 0.15))
        self.run_script(ocr, clk, 9)
        self.assertEqual(len(emitted), 1)
        ref = emitted[0]
        self.assertEqual(ref.en, self.A)
        self.assertIsNotNone(ref.end_t)                       # a végén lezárult
        self.assertTrue(fusion.healthy(clk.t))
        self.assertEqual(ocr.status()["lines"], 1)

    def test_readings_filtered_as_not_subtitle(self):
        ocr, fusion, emitted, clk = make_ocr(["12:45 PM $4,500"] * 6 + ["[music playing]"] * 4)
        self.run_script(ocr, clk, 10)
        self.assertEqual(emitted, [])

    def test_ocr_line_suppressed_when_asr_already_showed_it(self):
        ocr, fusion, emitted, clk = make_ocr([self.A] * 4)
        fusion.note_asr("Get in the car Lucia we dont have much time", t=clk.t)
        self.run_script(ocr, clk, 4)
        self.assertEqual(emitted, [])                          # már megjelent a hangból
        self.assertTrue(fusion.refs[0].suppressed)
        self.assertEqual(fusion.refs[0].hu, self.A)            # az SRT-lánc ne ragadjon be

    def test_off_mode_flushes_and_ignores(self):
        cfg = make_cfg()
        ocr, fusion, emitted, clk = make_ocr([self.A] * 4, cfg=cfg)
        ocr.set_roi((0.1, 0.8, 0.8, 0.15))
        self.run_script(ocr, clk, 4)
        self.assertEqual(len(emitted), 1)
        cfg.update({"subocr_mode": "off"})
        self.assertEqual(ocr.status()["state"], "off")
        ocr._flush(clk.t)
        self.assertIsNotNone(emitted[0].end_t)

    def test_status_states(self):
        cfg = make_cfg()
        ocr, fusion, emitted, clk = make_ocr([self.A] * 3, cfg=cfg)
        self.assertEqual(ocr.status()["state"], "no_roi")
        ocr.set_roi((0.1, 0.8, 0.8, 0.15))
        self.assertEqual(ocr.status()["state"], "watching")
        self.run_script(ocr, clk, 3)
        self.assertEqual(ocr.status()["state"], "reading")
        self.assertEqual(ocr.status()["text"], self.A)
        ocr.error = "valami"
        self.assertEqual(ocr.status()["state"], "error")

    def test_backend_errors_stop_the_reader_but_not_the_program(self):
        class Boom:
            def read(self, gray):
                raise RuntimeError("onnx")

        ocr, fusion, emitted, clk = make_ocr([])
        ocr.backend = Boom()
        self.run_script(ocr, clk, 6)
        self.assertIn("onnx", ocr.error)
        self.assertFalse(fusion.alive)                        # a hangfelismerés átveszi

    def test_missing_package_reported(self):
        def no_pkg():
            raise ImportError("rapidocr_onnxruntime")

        cfg = make_cfg()
        fusion = Fusion(cfg)
        ocr = SubtitleOcr(cfg, fusion, lambda r: None, log=lambda *a: None, backend_factory=no_pkg)
        ocr.set_roi((0.1, 0.8, 0.8, 0.15))
        self.assertFalse(ocr._load_backend())
        self.assertIn("rapidocr-onnxruntime", ocr.error)
        self.assertEqual(ocr.status()["state"], "error")
        self.assertFalse(fusion.alive)

    def test_thread_end_to_end_with_fake_frames(self):
        cfg = make_cfg(subocr_fps=10)
        ocr, fusion, emitted, clk = make_ocr([self.A] * 40, cfg=cfg, clk=time.monotonic)
        ocr.clock = time.monotonic
        fusion.clock = time.monotonic
        ocr.backend = None                                    # a szál tölti be (lustán)
        ocr.set_roi((0.1, 0.8, 0.8, 0.15))
        ocr.start()
        try:
            frame = make_frame(1920, 1080)
            deadline = time.monotonic() + 8
            while not emitted and time.monotonic() < deadline:
                ocr.offer(frame)
                time.sleep(0.01)
            self.assertEqual(len(emitted), 1)
            self.assertEqual(ocr.backend.shapes[0], (162, 1536))     # a kijelölt téglalap (0.15·1080 × 0.8·1920)
        finally:
            ocr.stop()
        self.assertIsNotNone(emitted[0].end_t)                # leállításkor a nyitott sor lezárul
        self.assertFalse(fusion.alive)


# ----------------------------------------------------------------------------------------------- kép és ROI
class _Plane(bytes):
    pass


class _Fmt:
    def __init__(self, name):
        self.name = name


class FakeFrame:
    def __init__(self, w, h, fmt, plane):
        self.width, self.height, self.format, self.planes = w, h, _Fmt(fmt), [plane]


def make_frame(w, h, fmt="nv12", pad=64, mark=None):
    ls = w + pad
    arr = np.zeros((h, ls), np.uint8)
    if mark:
        x0, y0, x1, y1 = mark
        arr[y0:y1, x0:x1] = 200
    plane = _Plane(arr.tobytes())
    plane.line_size = ls
    return FakeFrame(w, h, fmt, plane)


class ImageTests(unittest.TestCase):
    def test_luma_crop_respects_stride(self):
        fr = make_frame(1920, 1080, mark=(300, 900, 400, 950))
        box = subocr.roi_pixels((0.1, 0.8, 0.8, 0.15), 1920, 1080)
        self.assertEqual(box, (192, 864, 1728, 1026))
        crop = subocr.luma_crop(fr, box)
        self.assertEqual(crop.shape, (162, 1536))
        self.assertEqual(int(crop[900 - 864, 350 - 192]), 200)
        self.assertEqual(int(crop[0, 0]), 0)
        crop[:] = 7                                           # másolat: a képkockát nem érinti
        self.assertEqual(int(np.frombuffer(fr.planes[0], np.uint8)[900 * fr.planes[0].line_size + 350]), 200)

    def test_roi_validation_and_storage(self):
        self.assertIsNone(subocr.clean_rect((0.1, 0.1, 0.001, 0.5)))
        self.assertIsNone(subocr.clean_rect("nem téglalap"))
        self.assertIsNone(subocr.clean_rect((0.1, 0.1, float("nan"), 0.5)))
        x, y, w, h = subocr.clean_rect((0.9, 0.9, 0.5, 0.5))
        self.assertAlmostEqual(x + w, 1.0)
        self.assertAlmostEqual(y + h, 1.0)
        self.assertIsNone(subocr.load_roi("teszt-profil"))
        self.assertTrue(subocr.save_roi("teszt-profil", (0.12, 0.76, 0.76, 0.19)))
        self.assertEqual(subocr.load_roi("teszt-profil"), (0.12, 0.76, 0.76, 0.19))
        self.assertFalse(subocr.save_roi("teszt-profil", (0.1, 0.1, 0.0, 0.0)))
        self.assertIsNone(subocr.load_roi("../kilep"))        # érvénytelen azonosító: az „altalanos” profilra esik vissza
        subocr.clear_roi("teszt-profil")
        self.assertIsNone(subocr.load_roi("teszt-profil"))


# ----------------------------------------------------------------------------------------------- hang + felirat
class FakeAsr:
    def __init__(self, text="Get in the car, Lucia."):
        self.calls, self.text = 0, text

    def transcribe(self, audio, **k):
        self.calls += 1

        class R:
            pass

        r = R()
        r.text, r.avg_logprob, r.no_speech_prob, r.compression_ratio, r.words, r.ms = \
            self.text, -0.2, 0.0, 1.2, [], 120.0
        return r


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def submit(self, text, context, kind="final"):
        self.calls.append((text, list(context), kind))
        f = concurrent.futures.Future()
        f.set_result(("HU: " + text, 10.0))
        return f

    def note_shown(self, en):
        pass


def make_utterance(base, n_speech=30, n_silence=14):
    from subby.subtitler import Utterance

    u = Utterance(0.5, 0.35)
    t = base
    for i in range(n_speech + n_silence):
        u.append(np.zeros(512, np.float32), 0.9 if i < n_speech else 0.05, t)
        t += 0.032
    return u


class SubtitlerFusionTests(unittest.TestCase):
    def setUp(self):
        from subby.subtitler import LiveSubtitler

        self.cfg = make_cfg(spec_translate=True)
        self.fusion = Fusion(self.cfg)
        self.asr = FakeAsr()
        self.tr = FakeTranslator()
        self.q = queue.Queue()
        sink = SubtitleSink(self.cfg, log_path="", echo=lambda *a: None)
        self.sub = LiveSubtitler(self.cfg, self.asr, lambda f: 0.9, self.tr, sink, self.q,
                                 log=lambda *a: None, fusion=self.fusion)

    def screen_line(self, start, end=None, text="Get in the car, Lucia."):
        self.fusion.set_alive(True)
        ref = LineRef(text, 0.9, start)
        if end is not None:
            ref.set_end(end)
        self.fusion.note_ocr(ref)
        return ref

    def test_without_screen_subtitle_asr_works_as_before(self):
        self.sub.utt = make_utterance(time.monotonic() - 2.0)
        self.sub._finalize_silence()
        self.assertEqual(self.asr.calls, 1)
        item = self.q.get_nowait()
        self.assertEqual(item.en, "Get in the car, Lucia.")
        self.assertIsNone(item.ref)

    def test_screen_subtitle_makes_asr_secondary(self):
        now = time.monotonic()
        self.screen_line(now - 2.2)                            # a felirat a beszéddel egy időben jelent meg
        self.sub.utt = make_utterance(now - 2.0)
        self.sub._prefinal()                                   # a csend elején sem foglalja a GPU-t
        self.sub._finalize_silence()
        self.assertEqual(self.asr.calls, 0)
        self.assertTrue(self.q.empty())
        self.assertEqual(self.tr.calls, [])

    def test_speech_without_subtitle_is_still_transcribed(self):
        now = time.monotonic()
        self.screen_line(now - 30.0, now - 26.0)               # régi felirat: ez a beszéd nincs lefedve
        self.sub.utt = make_utterance(now - 2.0)
        self.sub._finalize_silence()
        self.assertEqual(self.asr.calls, 1)
        self.assertFalse(self.q.empty())

    def test_no_partial_transcription_when_covered(self):
        now = time.monotonic()
        self.screen_line(now - 3.0)
        self.cfg.update({"show_partial_en": True})
        self.sub.utt = make_utterance(now - 3.0, n_speech=60, n_silence=0)
        self.sub.last_partial_t = 0.0
        self.sub._maybe_partial()
        self.assertEqual(self.asr.calls, 0)

    def test_asr_line_already_shown_by_screen_subtitle_is_dropped(self):
        now = time.monotonic()
        self.fusion.set_alive(True)
        ref = LineRef("Get in the car, Lucia.", 0.9, now - 1.0)
        ref.set_end(now - 0.2)
        self.fusion.note_ocr(ref)
        self.cfg.update({"subocr_mode": "auto"})
        self.sub._emit_final("Get in the car Lucia", now, 100.0)     # a felirat már kint volt
        self.assertTrue(self.q.empty())
        self.assertEqual(self.sub.last_committed, "Get in the car Lucia")

    def test_asr_final_noted_for_ocr_duplicate_check(self):
        self.sub._emit_final("Where is the money?", time.monotonic(), 100.0)
        self.assertEqual(len(self.fusion.asr), 1)
        self.assertFalse(self.q.empty())

    def test_note_external_feeds_context_thread_safely(self):
        errors = []

        def hammer():
            try:
                for i in range(400):
                    self.sub.note_external(f"line number {i} with words")
                    self.sub.context_list()
            except Exception as e:           # pragma: no cover
                errors.append(e)

        ths = [threading.Thread(target=hammer) for _ in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(errors, [])
        self.assertTrue(self.sub.context_list())
        self.assertTrue(self.sub._prompt())

    def test_ocr_only_mode_never_transcribes(self):
        self.cfg.update({"subocr_mode": "ocr_only"})
        self.fusion.set_alive(True)
        self.sub.utt = make_utterance(time.monotonic() - 2.0)
        self.sub._finalize_silence()
        self.assertEqual(self.asr.calls, 0)


# ----------------------------------------------------------------------------------------------- motor
class EngineWiringTests(unittest.TestCase):
    def test_ocr_line_goes_through_translator_to_sink(self):
        from subby.engine import Engine, display_loop
        from subby.subtitler import FinalItem, LiveSubtitler

        cfg = make_cfg()
        sink = SubtitleSink(cfg, log_path="", echo=lambda *a: None)
        eng = Engine(cfg, sink, log=lambda *a: None)
        fusion, tr, q = Fusion(cfg), FakeTranslator(), queue.Queue()
        sub = LiveSubtitler(cfg, FakeAsr(), lambda f: 0.9, tr, sink, q, log=lambda *a: None, fusion=fusion)
        sub.note_external("Previous line of dialogue")
        ocr = eng._make_ocr(fusion, sub, tr, q, FinalItem)
        ref = LineRef("Get in the car, Lucia.", 0.9, time.monotonic())
        ocr.emit(ref)
        text, context, kind = tr.calls[0]
        self.assertEqual((text, kind), ("Get in the car, Lucia.", "final"))
        self.assertEqual(context, ["Previous line of dialogue"])      # az előző mondat kontextus
        self.assertIn("Get in the car, Lucia.", sub.context_list())   # és a felirat is bekerül a kontextusba
        stop = threading.Event()
        th = threading.Thread(target=display_loop, args=(q, sink, tr, stop, 2.0), daemon=True)
        th.start()
        deadline = time.monotonic() + 3
        while not sink.state()["lines"] and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()
        th.join(2)
        self.assertEqual([ln["hu"] for ln in sink.state()["lines"]], ["HU: Get in the car, Lucia."])
        self.assertEqual(ref.hu, "HU: Get in the car, Lucia.")        # a magyar a sorhoz kerül (SRT-hez)
        self.assertIsNone(ref.end_t)
        self.assertTrue(sink.lines[0]["ref"] is ref)

    def test_reload_ocr_without_engine_is_noop(self):
        from subby.engine import Engine

        eng = Engine(make_cfg(), SubtitleSink(make_cfg(), log_path="", echo=lambda *a: None), log=lambda *a: None)
        self.assertFalse(eng.reload_ocr())


# ----------------------------------------------------------------------------------------------- valódi OCR
def _font():
    for p in ("C:\\Windows\\Fonts\\arialbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return p
    return None


def render_subtitle(lines, size=40, w=1536, h=270, seed=3, bright=False):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = np.random.default_rng(seed)
    base = rng.integers(150 if bright else 10, 240 if bright else 80, (h, w)).astype(np.uint8)
    img = Image.fromarray(base).convert("RGB").filter(ImageFilter.GaussianBlur(3))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(_font(), size)
    y = h - 28 - len(lines) * int(size * 1.25)
    for ln in lines:
        tw = d.textlength(ln, font=f)
        d.text(((w - tw) / 2, y), ln, font=f, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
        y += int(size * 1.25)
    return np.array(img.convert("L"))


class RealOcrTests(unittest.TestCase):
    """A valódi OCR-motorral (rapidocr-onnxruntime), szintetikus játékfelirat-képeken. Ha a csomag vagy a betűtípus
    nincs meg, kimarad."""

    @classmethod
    def setUpClass(cls):
        try:
            import PIL  # noqa: F401
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("rapidocr-onnxruntime / Pillow nincs telepítve")
        if not _font():
            raise unittest.SkipTest("nincs szabad betűtípus a próbaképekhez")
        cls.backend = subocr.RapidOcrBackend()

    def read(self, img):
        return subocr.read_text(img, self.backend, 0.6)

    def test_reads_game_style_subtitles(self):
        cases = [(["Get in the car, Lucia. We don't have much time."], 40, False),
                 (["You think they're gonna just let us walk out of here?", "I'm not going back to that place."], 36, False),
                 (["Five hundred thousand dollars, in cash, by Friday."], 44, True),
                 (["Jason: Hold on, I'm coming!"], 38, False)]
        for lines, size, bright in cases:
            rd = self.read(render_subtitle(lines, size, bright=bright))
            self.assertGreaterEqual(similarity(rd.text, " ".join(lines)), 0.93, (lines, rd.text))
            self.assertFalse(rd.why, (lines, rd.why))

    def test_no_text_gives_no_reading(self):
        for bright in (False, True):
            self.assertEqual(self.read(render_subtitle([], bright=bright)).text, "")

    def test_hud_numbers_rejected(self):
        rd = self.read(render_subtitle(["$12,450   08:32 PM"], 40))
        self.assertTrue(rd.why or not rd.text, rd.text)

    def test_full_pipeline_on_real_reads(self):
        """Képkockák -> OCR -> sorkövetés -> kiadott sor, a valódi motorral."""
        cfg = make_cfg()
        clk = Clock()
        fusion = Fusion(cfg, clock=clk)
        emitted = []
        ocr = SubtitleOcr(cfg, fusion, emitted.append, log=lambda *a: None, backend_factory=lambda: self.backend,
                          clock=clk)
        ocr.backend = self.backend
        ocr.set_roi((0.1, 0.8, 0.8, 0.15))
        sub = render_subtitle(["Get in the car, Lucia. We don't have much time."])
        empty = render_subtitle([], seed=9)
        for img in [empty, sub, sub, sub, sub, empty, empty, empty, empty]:
            ocr._process(clk.t, img)
            clk.t += 0.25
        self.assertEqual(len(emitted), 1)
        self.assertGreaterEqual(similarity(emitted[0].en, "Get in the car, Lucia. We don't have much time."), 0.93)
        self.assertIsNotNone(emitted[0].end_t)


if __name__ == "__main__":
    unittest.main()
