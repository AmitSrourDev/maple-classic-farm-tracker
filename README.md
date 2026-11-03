# MapleStory Classic Farm Tracker

An overlay for **MapleStory Classic World** that reads your screen and tells you, while you grind:

- **EXP/min** and a **farm rate** that ignores standing around and the fresh-spawn burst, ETA to the next level
- **potion counts** from the quickslot, usage per hour and what they cost you in **mesos per minute**
- **mesos** (from the inventory window) and your **net mesos/min** after pots
- **which map you farm** and a per-map **grind log** (farm EXP/h, pots cost, mesos, net)
- a **kill model** from your meowdb build: one-cast chance, casts and MP per kill for the map's mob

It only takes screenshots and OCRs pixels. It never touches the game process, its memory, its window or its files, and never sends input.

```
Lv 31  5,515 / 108,480  5.08 %   10:58:53
FARM 590/min  35.4k/h  (last 12 active min, trimmed)    session AVG 514/min  25m  +10,292
To Lv 32: 102,965  ETA 2h55m at farm rate / 3h20m at session avg   level-ups 1
Pots Meat 142 (-60/h = 80/min) · Lemon 60 (-280/h = 700/min)
Mesos 25,562  +1,500/min gross  -780/min pots  = +720/min net (43.2k/h)
Farming Kerning City Subway: Transfer Area  ·  14 min, 571 EXP/min (34.3k/h), pots 700/min, mesos +21,000
Model (solo, from build.json): Stirge: 100% one-cast, 1.00 casts/kill, 16 MP/kill (48 mesos in Lemon) ...
```

## Install (Windows)

1. Download **`MapleFarmTracker-Setup-<version>.exe`** from the [latest release](../../releases/latest) and run it (per-user install, no admin). Tick *Start with Windows* if you want the overlay at login.
2. Start the game **windowed or borderless** (exclusive fullscreen captures black).
3. Open *MapleStory Classic Farm Tracker* from the Start menu. The overlay appears bottom-right, finds the EXP bar and the quickslot, and starts counting. Drag it anywhere; right-click for the menu; double-click for the grind log.

The app checks the [releases](../../releases) on start and every 6 hours and installs new versions by itself (installer verified against `SHA256SUMS.txt`; turn it off under *Settings > Updates*). A portable zip is published too (keeps its data next to the exe). Windows SmartScreen may warn on first run because the binaries are not code-signed.

## Settings (right-click > Settings)

- **General**: read intervals, rolling windows, red threshold for pots, monitor, overlay font/opacity/width, *Start with Windows*.
- **Pots**: tick the quickslot keys that hold potions, name them, set the unit price you pay (NPC price). Default: Del = Meat 80, End = Lemon 150.
- **meowdb**: paste your meowdb session cookie once and *Sync now* pulls your account export (your character builds) into `build.json` for the kill model. meowdb logs in with Google, so there is no password login; the cookie stays on your disk and is only sent to `meowdb.com/msclassic/api/me/export`.
- **Updates**: auto-install toggle, repo, *Check now*, *Install update*.
- **Set region** (menu): drag a box around the EXP bar, the quickslot grid or the mesos number if auto-detection misses on your layout. *Clear all* returns to auto.

Data lives in `%APPDATA%\MapleFarmTracker` (settings, `grind_log.csv`, per-session CSV + summary, `build.json`, cookie). *Open data folder* is in the menu.

## How it works

- **EXP**: finds `11,323 [17.79%]` on the status bar once (OCR, bottom 30% of the screen, then the whole screen for windowed games placed higher up, then the other monitors), then re-reads that small region every 10 s. Every read must agree with the Classic World EXP table for your level or level+1, else it is dropped; level changes and big jumps need two consecutive agreeing reads. Level-ups are counted correctly.
- **Farm rate**: per-minute EXP buckets, minutes under 50 EXP dropped, last 15 active minutes, top and bottom sixth trimmed.
- **Pots**: the 2x4 quickslot grid is found by its key labels + light cell backgrounds; counts are OCR'd every 30 s and validated; usage/h is a robust slope (Theil-Sen).
- **Mesos**: `<number> MESOS` on the inventory window, found when the window is open, then re-read each cycle.
- **Map**: the minimap title, snapped to meowdb's list of all 453 Classic World maps (`maple_maps.txt`).
- **Grind log**: a segment starts when EXP rises, ends on a map change or after 2 min without gain, and is logged if it lasted at least 1 min at 50+ EXP/min.
- **Kill model** (`maple_build.py`, thief/assassin only so far): meowdb's damage, defense, level-gap and accuracy formulas, Lucky Seven's per-level MP/damage table, and a mob table (Stirge, Jr. Necki, Horny/Zombie Mushroom, Evil Eye, Ligator, Curse Eye, Jr. Wraith, Lupin, Cold Eye, Zombie Lupin, Wraith). `python maple_build.py --map "Transfer Area"` prints the full table. Other jobs: the tracker works for everyone; only this model line is thief-specific.

## Different setups

Any resolution and UI scale the client offers (the OCR locates text, it does not assume pixel positions); primary or secondary monitor; windowed game anywhere on screen; any job (potion, EXP, mesos and map features are class-independent); any potions (name and price them in *Pots*). Tested on 2560x1440 and a 3440x1440 ultrawide. If something is not found on your layout, use *Set region* once, or open an issue with a screenshot.

## Run from source

```
git clone https://github.com/AmitSrourDev/maple-classic-farm-tracker
cd maple-classic-farm-tracker
MapleHUD.bat        # overlay   (creates .venv, installs requirements)
run.bat             # console
python maple_hud.py --selftest
python tests/test_session.py && python tests/test_synth.py 20
```

Releases are built by GitHub Actions on a `v*` tag (`.github/workflows/release.yml`): PyInstaller folder build (GUI + console exe), Inno Setup installer, portable zip, `SHA256SUMS.txt`. `build_exe.bat` does the PyInstaller part locally.

## Privacy and the game

Screenshots stay on your machine; OCR runs locally (RapidOCR / PP-OCRv3 on CPU). Network traffic: GitHub (update check, downloads), meowdb (only what you trigger in Settings, with your cookie). Nothing is sent anywhere else. The overlay is a plain always-on-top window; capture is the same GDI path the Snipping Tool uses.

## Limits

- The count font's "6" can OCR as "8" at some UI scales (60 shows as 80 until the digit changes). Rates and costs are unaffected.
- Mesos need the inventory window open at least now and then.
- Map name needs the minimap panel open.
- Kill model: thief/assassin only; Lemon's MP value is set to 50 (edit `MP_POTS` in `maple_build.py` if meowdb says otherwise).

MIT license. Not affiliated with Nexon or meowdb.
