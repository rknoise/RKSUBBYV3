# -*- coding: utf-8 -*-
"""rksubby V3 – Xbox (HDMI) kép 1080p120-ig, felvétel NVENC-cel és élő magyar felirat angol beszédhez.

Indítás:  python rksubby.py      (a telepített változat: Asztal › rksubby V3)
Beállítások: a program fogaskerék-gombja; fájl: %USERPROFILE%/.rksubby/config.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if getattr(sys, "frozen", False):   # telepített változat: a csomagolt SDL2.dll
    os.environ.setdefault("PYSDL2_DLL_PATH", os.path.join(sys._MEIPASS, "sdl2dll", "dll"))

from rkv3.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
