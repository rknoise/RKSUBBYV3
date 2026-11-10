<p align="center"><img src="Logo_v3.png" alt="rksubby V3 logó" width="220"></p>

# rksubby V3 – Xbox-rögzítő élő magyar felirattal

> **English summary:** rksubby V3 is a lightweight, RECentral-style capture app for the Xbox (or any HDMI source) with live Hungarian subtitles. It shows the capture card's picture in its own window at up to **1080p120**, rendered on the GPU with almost no CPU load, records it with **NVENC** (HEVC or H.264 + AAC, crash-safe MP4) at the exact resolution and frame rate of the incoming signal, and turns English speech into Hungarian subtitles on the fly: Silero VAD + faster-whisper on the GPU, translated by DeepL, a local AI (Ollama) or Google. Subtitles are drawn on the picture, saved next to each recording as `.srt`, and can be burned into an exported copy. Everything is configured behind a single gear button. One installer contains everything except the capture card driver and the speech model, which is downloaded on first use.

Az rksubby V3 már nem csak feliratoz: **a capture kártya teljes jelét kezeli**. Mutatja az Xbox képét 120 Hz-en, felveszi, és közben élőben magyar feliratot ír az angol beszédhez. A RECentral, az OBS és a régi rksubbyV2 helyett egyetlen program.

<p align="center"><img src="docs/rksubby_v3_screenshot.jpg" alt="rksubby V3: az Xbox képe 1080p120-ban, oldalt az élő magyar felirat és az utolsó mondatok" width="100%"></p>
<p align="center"><sub>Az Xbox képe 1080p · 120 fps-ben, alatta a felvétel vezérlői, jobbra az élő felirat (DeepL) és az utolsó lefordított mondatok.</sub></p>

## Újdonságok a V3-ban

- **Valódi 1080p120 előnézet.** A kép közvetlenül a videokártyán jelenik meg (Direct3D 11), a színátalakítás és a skálázás is ott történik. Mért érték: 119,9 fps a kártyáról és az előnézetben is, a processzor terhelése felvétel közben ~2%.
- **Felvétel a jelhez igazítva, beállítás nélkül.** 1080p120 jelből 1080p120 fájl, 4K60-ból 4K60. A program indításkor és jelváltáskor felismeri a felbontást és a képkockasebességet. NVIDIA NVENC kódolás (HEVC vagy H.264), AAC hang a kártyáról. Áramszünet vagy összeomlás után is lejátszható MP4.
- **Felirat a képen és a felvétel mellett.** Az élő felirat a képre kerül, és minden felvétel mellé `.srt` fájl készül (a VLC és az MPC magától betölti). Az **Export felirattal** gomb egy olyan változatot készít, amelybe a felirat bele van égetve.
- **Új, egységes felület.** Sötétkék téma rózsaszín–lila–kék kiemeléssel. Középen a kép, alatta a felvétel vezérlői, oldalt az élő felirat, az utolsó mondatok és a felvételek listája.
- **Minden beállítás egy fogaskerék mögött.** Fordító (DeepL, Lokális AI, Google, nincs), DeepL-kulcs beillesztése és ellenőrzése, beszédfelismerő modell, felirat kinézete, felvételi mappa és minőség, capture kártya, hang.
- **Valódi telepítő.** `rksubby_V3_Setup.exe`: Python, Qt, FFmpeg, SDL2, faster-whisper és az NVIDIA CUDA-könyvtárak is benne vannak. Rendszergazdai jog nem kell, és a Gépház › Alkalmazások alól eltávolítható.
- **Egy példány.** A második indítás az elsőt hozza előre, így nem veszik el egymástól a kártyát.
- **Biztonság.** A kártyát kizárólag a szabványos DirectShow-úton nyitja meg (mint az OBS). Gyártói, nem dokumentált driver-hívást nem használ.

## Letöltés és telepítés

1. Töltsd le a **[Releases](https://github.com/rknoise/rksubbyV2/releases)** oldalról a `rksubby_V3_Setup.exe`-t.
2. Futtasd. Kb. 2–4 GB helyet foglal (a CUDA-könyvtárak miatt), rendszergazdai jog nem kell.
3. Indítsd az asztali **rksubby V3** ikonnal.

Az első feliratozáskor a program egyszer letölti a beszédfelismerő modellt (distil-large-v3, kb. 1,5 GB, pár perc). A régi V2 beállításait (DeepL-kulcs, játékprofil, felirat stílusa) a V3 magától átveszi, mert ugyanabban a mappában vannak: `%USERPROFILE%\.rksubby`.

**Amit a telepítő nem tud feltenni:** a capture kártya illesztőprogramját (pl. AVerMedia GC573 a gyártó oldaláról). Ha nem találja a kártyát, a telepítő szól.

## Kezelés

| | |
|---|---|
| **F9** | felvétel indítása / leállítása |
| **F10** | szünet / folytatás (a felvétel és az időzítő is megáll) |
| **F8** | felirat a képen be/ki |
| **F11** vagy dupla kattintás | teljes képernyő, **Esc**: vissza |
| **F12** | pillanatkép (PNG, a felvételek mappájának `Képek` almappájába) |
| **F5** | jel újrafelismerése |

- **Élő felirat panel:** állapot, hangszint, fordító és DeepL-keret, a feliratozás indítása és leállítása.
- **Utolsó mondatok:** a magyar felirat és alatta az angol eredeti, időponttal.
- **Felvételek:** lejátszás, mappa megnyitása, **Export felirattal**, Lomtárba helyezés (megerősítés után, visszaállítható).

## Beállítások (fogaskerék)

- **Fordítás:** DeepL (a legjobb minőség, kulcs kell), **Lokális AI** (Ollama a saját gépeden, semmi nem megy ki), Google (ingyenes, kulcs nélkül) vagy fordítás nélkül (angol felirat).
  - A DeepL-kulcsot beillesztheted, és egy kattintással ellenőrizheted, ilyenkor a havi keretet is látod.
  - Állítható a hangnem (tegező vagy magázó) és a játékprofil szótára (glosszárium).
  - Tartalék fordító: ha a választott nem elérhető, a felirat akkor is magyar marad.
- **Beszédfelismerés:** modell (distil-large-v3 az ajánlott, a large-v3-turbo pontosabb), GPU vagy processzor, játékprofil (GTA V, GTA VI, RDR2, általános), angol előnézet, a mondat végét jelző csend hossza, automatikus indulás.
- **Felirat:** betűméret, szín, háttérdoboz, sorok száma, pozíció. OBS böngészőforrás-felirat is kérhető: `http://127.0.0.1:8765/`.
- **Felvétel:** mappa (alapértelmezett: `Videók\rksubby`), minőség (normál, magas, maximális), kódolás (HEVC vagy H.264), SRT mentése.
- **Kép és hang:** capture kártya kiválasztása (alapértelmezés szerint automatikus), a játék hangja szóljon-e a gépen.

### DeepL-kulcs

1. Regisztrálj a DeepL API Free csomagra: <https://www.deepl.com/pro-api> (havi 500 000 karakter ingyen).
2. Másold ki a kulcsot: <https://www.deepl.com/your-account/keys>. A Free kulcs vége `:fx`.
3. Fogaskerék › Fordítás › **Beillesztés**, majd **Ellenőrzés**.

A kulcs a felhasználói mappádba kerül (`%USERPROFILE%\.rksubby\deepl_key.txt`), nem a programmappába.

### Lokális AI (Ollama)

Telepítsd az [Ollamát](https://ollama.com), majd parancssorban: `ollama pull translategemma:12b`. A fogaskerék › Fordítás oldalon válaszd a **Lokális AI**-t, és nyomd meg az **Ellenőrzés** gombot. A szöveg ilyenkor nem hagyja el a gépet.

## Hogyan működik

```
Xbox ─HDMI─> capture kártya (DirectShow, egy csatorna: kép + hang)
               │
               ├─ kép (NV12) ──> előnézet: SDL2 + Direct3D 11 a Qt-ablakban ──> felirat a képen
               ├─ kép + hang ──> NVENC (HEVC/H.264) + AAC ──> .mp4  (+ .srt a felirattal)
               └─ hang ──┬──> hangszóró
                         └──> Silero VAD ─> faster-whisper (GPU) ─> DeepL / Lokális AI / Google ─> magyar felirat
```

**Jelfelismerés:** a kártya bármilyen kért formátumra skáláz, ezért a program a ténylegesen beérkező képet méri. A felbontást abból ismeri fel, hogy melyik módban (4K, 1440p, 1080p) van még valódi finom részlet a képben. Az fps-t a képkockák beérkezési üteméből méri: a kártya nem gyárt képkockát, így 60 Hz-es jelből 120-ra kérve is csak 60 érkezik. A felirat a tartalom ütemét sosem használja, mert egy 60 fps-es játék 120 Hz-es jelben is lehet. Jelváltáskor (az Xbox a HDMI-t újraszinkronizálja) magától újramér.

## Mért teljesítmény

RTX 3090, Ryzen 7 7800X3D, AVerMedia Live Gamer 4K (GC573), Xbox Series (2026. november):

| | |
|---|---|
| Előnézet 1080p120 jelnél | **119,9 fps** (a kártyáról 119,9 fps) |
| Felvétel 1080p120 (ffprobe) | HEVC 1920×1080, `r_frame_rate=120/1`, 1763 képkocka / 14,71 s, 0 eldobott |
| Processzorterhelés felvétel közben | ~2% (az egész gépre vetítve) |
| Mondat vége → magyar felirat (V2-es mérés, DeepL) | ~0,8 mp (medián) |
| Feliratozó indulása (modell a gyorsítótárban) | ~7 mp |

## Követelmények

- Windows 10 (1809+) vagy 11, 64 bites
- Capture kártya illesztőprogrammal. Az AVerMedia Live Gamer 4K (GC573) kipróbált; más DirectShow-os kártya is működhet.
- NVIDIA videokártya ajánlott (felvétel NVENC-cel, gyors felismerés, 6+ GB VRAM). Nélküle is elindul: a felvétel processzorral kódol, a felismerés lassabb.
- Fordításhoz internet (DeepL vagy Google), vagy Ollama a lokális AI-hoz

## Hibaelhárítás

**„A kártya nem nyitható meg”.** A kártyát egyszerre egy program használhatja: zárd be a RECentralt, az OBS-t és a régi rksubbyV2-t. A V3 pár másodpercenként magától újrapróbálja.

**„Nincs jel”.** Be van kapcsolva az Xbox? Jó a HDMI-kábel? Az Xbox 120 Hz-hez: Beállítások › Általános › TV és megjelenítés › Frissítési gyakoriság: 120 Hz.

**60 fps-t mutat 120 helyett.** Az Xbox csak akkor küld 120 Hz-et, ha a játék vagy a menü támogatja. Sok játék 60 Hz-re váltja a jelet, ilyenkor a felvétel is 60 fps lesz. Ha az Xbox visszavált, a program magától újramér (vagy nyomd meg az F5-öt).

**Nem jön hang / „NEM JÖN HANG”.** Szól a játék? A panel hangszintjelzőjének mozognia kell. Egy másik program is használhatja a kártyát.

**Lassú vagy hiányzó felirat.** Fogaskerék › Beszédfelismerés: GPU legyen kiválasztva. Az első indításkor a modell letöltése pár percig tart.

**DeepL-hibák.** 403: rossz a kulcs. 456: elfogyott a havi keret, ilyenkor a tartalék fordító (Lokális AI vagy Google) dolgozik.

Napló: `%USERPROFILE%\.rksubby\rksubby.log` (Fogaskerék › Névjegy › Naplómappa).

## Fejlesztőknek

```bat
uv venv --python 3.12 .venv-v3
uv pip install --python .venv-v3\Scripts\python.exe -r requirements.txt
.venv-v3\Scripts\python.exe rksubby.py
```

Tesztek: `.venv-v3\Scripts\python.exe -m unittest discover -s tests`
Telepítő építése (Inno Setup 6 kell: `winget install JRSoftware.InnoSetup`): `.venv-v3\Scripts\python.exe tools\build_v3.py`, az eredmény: `dist\rksubby_V3_Setup.exe`.

```
rksubby.py            indító
rkv3/                 V3: felület és rögzítő
  app.py              főablak, vezérlés (kártya, felvétel, feliratozó, felvételek)
  capture.py          kártyakeresés, jelfelismerés, élő kép + hang, NVENC-felvétel
  video.py            GPU-s előnézet (SDL2/D3D11 a Qt-ablakban), felirat rajzolása
  settings.py         beállítások (fogaskerék)
  export.py           feliratos (beégetett) export
  theme.py, icons.py  kinézet
subby/                a feliratozó motor (V2-ből): VAD, faster-whisper, fordítók, feliratállapot, OBS-szerver
profiles/             játékprofilok (nevek, glosszárium)
installer/            Inno Setup szkript, OLVASS_EL.txt
tools/                build_v3.py, make_assets.py
```

## Adatvédelem

A beszédfelismerés helyben fut. Csak a felismert angol szöveg megy a fordítóhoz (DeepL vagy Google). Lokális AI esetén semmi nem hagyja el a gépet. A `sub_log.tsv` a felhasználói mappában helyben tárolja a felismert mondatokat; a beállításokban kikapcsolható.

## Felhasznált projektek

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) és [CTranslate2](https://github.com/OpenNMT/CTranslate2) (MIT), [Whisper](https://github.com/openai/whisper) / [distil-whisper](https://github.com/huggingface/distil-whisper) modellek, [Silero VAD](https://github.com/snakers4/silero-vad) (MIT), [FFmpeg](https://ffmpeg.org) és [PyAV](https://github.com/PyAV-Org/PyAV) (LGPL), [SDL2](https://libsdl.org) (zlib), [Qt for Python / PySide6](https://www.qt.io/qt-for-python) (LGPL), NVIDIA CUDA-könyvtárak (cuBLAS, cuDNN – NVIDIA-licenc), [DeepL API](https://www.deepl.com/pro-api), [Ollama](https://ollama.com), [Inno Setup](https://jrsoftware.org/isinfo.php).
A `teszt_angol.wav` J. F. Kennedy 1961-es beiktatási beszédéből származik (közkincs). Az Xbox a Microsoft védjegye; az rksubby független, nem hivatalos program.
