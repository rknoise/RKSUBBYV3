# -*- coding: utf-8 -*-
"""rksubby V3 grafikák: logó (PNG), ikon (ICO), fejléc-logó. Futtatás: python tools/make_assets.py"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "rkv3", "assets")
FONTS = r"C:\Windows\Fonts"
O1, O2, O3 = (255, 63, 180), (155, 77, 255), (47, 140, 255)   # rózsaszín -> lila -> kék


def font(name, size):
    for n in (name, "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(os.path.join(FONTS, n), size)
        except OSError:
            pass
    return ImageFont.load_default()


def hgrad(w, h, stops):
    xs = np.linspace(0, 1, w)
    a = np.zeros((h, w, 4), np.uint8)
    for ch in range(3):
        a[:, :, ch] = np.interp(xs, [p for p, _ in stops], [c[ch] for _, c in stops])[None, :]
    a[:, :, 3] = 255
    return Image.fromarray(a, "RGBA")


def wave_mask(w, h, x0, x1, cy, amp, width):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    pts = []
    n = 400
    for i in range(n + 1):
        t = i / n
        x = x0 + (x1 - x0) * t
        env = math.exp(-((t - 0.52) ** 2) / 0.035)
        y = cy - amp * env * math.sin(t * math.pi * 15) * (0.75 + 0.25 * math.sin(t * 7))
        pts.append((x, y))
    d.line(pts, fill=255, width=width, joint="curve")
    return m


def logo(size=512, with_text=True, version=True):
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.2), fill=255)
    a = np.zeros((S, S, 4), np.uint8)
    yy = np.linspace(0, 1, S)[:, None]
    a[:, :, 0] = (8 + 8 * yy).astype(np.uint8)
    a[:, :, 1] = (11 + 8 * yy).astype(np.uint8)
    a[:, :, 2] = (26 + 22 * yy).astype(np.uint8)
    a[:, :, 3] = 255
    bg = Image.fromarray(a, "RGBA")
    img.paste(bg, (0, 0), m)
    g = hgrad(S, S, [(0, O1), (0.5, O2), (1, O3)])
    cy = S * (0.40 if with_text else 0.5)
    wm = wave_mask(S, S, S * 0.12, S * 0.88, cy, S * (0.17 if with_text else 0.28), max(3, int(S * 0.022)))
    img.paste(g, (0, 0), wm.filter(ImageFilter.GaussianBlur(S * 0.02)).point(lambda v: min(255, int(v * 1.6))))
    img.paste(g, (0, 0), wm)
    if with_text:
        d = ImageDraw.Draw(img)
        f = font("seguibl.ttf", int(S * 0.2))
        d.text((S * 0.5, S * 0.72), "SUBBY", font=f, fill=(245, 245, 248), anchor="mm")
        d.text((S * 0.80, S * 0.585), "RK", font=font("seguibl.ttf", int(S * 0.085)), fill=(120, 175, 255), anchor="mm")
        if version:
            tm = Image.new("L", (S, S), 0)
            ImageDraw.Draw(tm).text((S * 0.80, S * 0.87), "V3", font=font("seguibl.ttf", int(S * 0.12)), fill=255,
                                    anchor="mm")
            img.paste(g, (0, 0), tm)
    return img


def wordmark(h=96):
    """Fejléc-logó: hullám + 'rksubby V3' felirat, átlátszó háttér."""
    W = int(h * 4.6)
    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    g = hgrad(W, h, [(0, O1), (0.5, O2), (1, O3)])
    wm = wave_mask(W, h, h * 0.05, h * 1.05, h * 0.5, h * 0.3, max(2, int(h * 0.06)))
    img.paste(g, (0, 0), wm.filter(ImageFilter.GaussianBlur(h * 0.04)))
    img.paste(g, (0, 0), wm)
    d = ImageDraw.Draw(img)
    f = font("seguibli.ttf", int(h * 0.52))
    x = h * 1.22
    d.text((x, h * 0.5), "rksubby", font=f, fill=(245, 245, 248), anchor="lm")
    tw = d.textlength("rksubby", font=f)
    tm = Image.new("L", (W, h), 0)
    ImageDraw.Draw(tm).text((x + tw + h * 0.12, h * 0.5), "V3", font=f, fill=255, anchor="lm")
    img.paste(g, (0, 0), tm)
    return img.crop(img.getbbox())


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    L = logo(512)
    L.save(os.path.join(OUT, "logo512.png"))
    L.save(os.path.join(ROOT, "Logo_v3.png"))
    ic = logo(256, with_text=False)
    ic.save(os.path.join(OUT, "icon256.png"))
    ic.save(os.path.join(OUT, "rksubby.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    wordmark(96).save(os.path.join(OUT, "wordmark.png"))
    print("ok")
