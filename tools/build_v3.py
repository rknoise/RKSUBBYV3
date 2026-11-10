# -*- coding: utf-8 -*-
"""rksubby V3 build:  PyInstaller (onedir, minden csomaggal és a CUDA-könyvtárakkal)  ->  Inno Setup telepítő.

Futtatás (a projektmappából):  .venv-v3\\Scripts\\python.exe tools\\build_v3.py
Eredmény:  dist\\rksubby_V3_Setup.exe  (+ dist\\OLVASS_EL.txt)
"""
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
SP = os.path.join(os.path.dirname(os.path.dirname(PY)), "Lib", "site-packages")
VERSION = "3.0.0"


def run(args, **kw):
    print(">", " ".join(args), flush=True)
    subprocess.check_call(args, cwd=ROOT, **kw)


def iscc():
    for p in [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"]:
        if os.path.isfile(p):
            return p
    return shutil.which("ISCC") or ""


def main():
    only_installer = "--installer-only" in sys.argv
    if not only_installer:
        build_app()
    make_installer()


def build_app():
    run([PY, "tools/make_assets.py"])
    shutil.rmtree(os.path.join(BUILD, "v3"), ignore_errors=True)
    args = [PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "rksubby",
            "--icon", os.path.join(ROOT, "rkv3", "assets", "rksubby.ico"),
            "--distpath", os.path.join(BUILD, "v3", "app"), "--workpath", os.path.join(BUILD, "v3", "work"),
            "--specpath", os.path.join(BUILD, "v3"),
            "--add-data", os.path.join(ROOT, "rkv3", "assets") + ";rkv3/assets",
            "--add-data", os.path.join(ROOT, "profiles") + ";profiles",
            "--add-data", os.path.join(ROOT, "subby", "web") + ";subby/web",
            "--add-data", os.path.join(ROOT, "teszt_angol.wav") + ";.",
            "--collect-all", "sdl2dll", "--collect-all", "av", "--collect-all", "faster_whisper",
            "--collect-all", "ctranslate2", "--collect-binaries", "onnxruntime",
            "--hidden-import", "sdl2", "--hidden-import", "PySide6.QtNetwork",
            "--exclude-module", "PyQt5", "--exclude-module", "tkinter", "--exclude-module", "matplotlib",
            "--exclude-module", "torch", "--exclude-module", "IPython"]
    for pkg in ("cublas", "cudnn", "cuda_nvrtc"):
        b = os.path.join(SP, "nvidia", pkg, "bin")
        if os.path.isdir(b):
            for dll in glob.glob(os.path.join(b, "*.dll")):
                args += ["--add-binary", f"{dll};nvidia/{pkg}/bin"]
    args.append(os.path.join(ROOT, "rksubby.py"))
    run(args)


def make_installer():
    app_dir = os.path.join(BUILD, "v3", "app", "rksubby")
    os.makedirs(DIST, exist_ok=True)
    exe = iscc()
    if not exe:
        sys.exit("Inno Setup (ISCC.exe) nem található – telepítsd: winget install JRSoftware.InnoSetup")
    run([exe, f"/DAppVersion={VERSION}", f"/DSrcDir={app_dir}", f"/DOutDir={DIST}",
         os.path.join(ROOT, "installer", "rksubby_v3.iss")])
    shutil.copy2(os.path.join(ROOT, "installer", "OLVASS_EL.txt"), DIST)
    print("OK ->", DIST)


if __name__ == "__main__":
    main()
