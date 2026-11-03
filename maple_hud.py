#!/usr/bin/env python3
"""
maple_hud.py - MapleStory Classic Farm Tracker: EXP/min, farm rate, potions and
their cost, mesos, per-map grind log. Overlay (default) or console (--console).

What it reads, all by screenshotting the PRIMARY MONITOR (plain desktop
capture, like the Snipping Tool) and OCRing pixels:
  - the EXP readout on the status bar          -> EXP/min, ETA to level
  - the 2x4 quickslot counts                   -> potion usage/h, mesos/min
  - the minimap title (top-left)               -> which map you farm
  - "<number> MESOS" (inventory window, when open) -> mesos/min, net after pots
It never opens the game process, reads its memory, hooks windows, injects
anything, sends input, or touches files in the game folder.

Run:  MapleHUD.bat                  overlay (bottom-right, draggable)
      run.bat / python maple_hud.py --console     same engine in the terminal
      python maple_hud.py --pots "Del=Meat:80,End=Lemon:150"   set pot slots
Settings: maple_hud_settings.json next to this file (created on first run).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from app_paths import APP_TITLE, DATA_DIR, FROZEN, data
from maple_exp import (EXP_TABLE, Grabber, LiveBlock, Ocr, Session, _make_dpi_aware, fmt_min,
                       locate_exp_region, read_exp_from_line)
from version import REPO, __version__
from maple_slots import (KEY_POS, KEYS, GrindLog, MapWatcher, MesoTracker, SlotTracker, cell_rect,
                         find_mesos, find_quickslot, read_map_name, read_mesos, read_slot_count)
try:
    import maple_build
except Exception:  # model is optional
    maple_build = None

HERE = DATA_DIR
SETTINGS_PATH = data("maple_hud_settings.json")

DEFAULT_SETTINGS = {
    "pos": None,                       # [x, y] of the overlay; None = bottom-right
    "interval": 10,                    # seconds between EXP reads
    "slots_every": 3,                  # read quickslot counts every N EXP reads
    "map_every_sec": 15,               # read the minimap title this often
    "mesos_scan_sec": 30,              # look for the MESOS label this often while not locked
    "window_min": 5,                   # rolling EXP window (minutes)
    "farm_window_min": 15,             # farming rate: last N ACTIVE minutes (idle minutes skipped)
    "pots_window_min": 15,             # usage / mesos rate window (minutes)
    "monitor": 1,                      # mss monitor index (1 = primary)
    "font_size": 10,
    "alpha": 0.92,
    "columns": 88,                     # overlay width in characters
    "low_threshold": 30,               # potion count that turns red
    "slots": {                         # key -> item name and what you pay per unit (NPC price)
        "Del": {"name": "Meat", "price": 80},
        "End": {"name": "Lemon", "price": 150},
    },
    "grind_min_minutes": 1.0,
    "grind_min_rate": 50.0,
    "grind_idle_sec": 120,
    "exp_region": None,                # [L, T, W, H] override
    "quickslot": None,                 # {"x0","y0","pitch","size"} override
    "mesos_region": None,              # [L, T, W, H] override
    "auto_update": True,               # install new releases automatically (installed build only)
    "update_repo": REPO,               # GitHub owner/name the updater polls
    "start_with_windows": False,       # registry Run key (installed build)
    "job": "assassin",                 # kill model is implemented for thief/assassin (Lucky Seven)
}


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    if SETTINGS_PATH.exists():
        try:
            user = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k == "slots" and isinstance(v, dict):
                    s["slots"] = v
                else:
                    s[k] = v
        except Exception:
            pass
    return s


def save_settings(s):
    try:
        SETTINGS_PATH.write_text(json.dumps({k: v for k, v in s.items() if not k.startswith("_")}, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass


def parse_pots(spec: str):
    """'Del=Meat:80,End=Lemon:150' -> {'Del': {'name': 'Meat', 'price': 80}, ...}"""
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, rest = part.partition("=")
        name, _, price = rest.partition(":")
        key = {k.lower(): k for k in KEY_POS}.get(key.strip().lower())
        if not key:
            raise ValueError(f"unknown slot key {part!r}; use one of {list(KEY_POS)}")
        out[key] = {"name": name.strip() or key, "price": float(price) if price.strip() else 0}
    return out


def theil_sen_per_hour(points, min_dt=60):
    """Robust slope (units per hour) of (t, value) points; None if too few."""
    if len(points) < 3:
        return None
    slopes = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dt = points[j][0] - points[i][0]
            if dt >= min_dt:
                slopes.append((points[j][1] - points[i][1]) / dt * 3600.0)
    if not slopes:
        return None
    return float(np.median(slopes))


def k(n):
    """compact number: 47,760 -> 47.8k"""
    if n is None:
        return "-"
    n = float(n)
    if abs(n) >= 1e6:
        return f"{n / 1e6:.2f}m"
    if abs(n) >= 1e4:
        return f"{n / 1e3:.1f}k"
    return f"{n:,.0f}"


# --------------------------------------------------------------------------
# Start with Windows (HKCU Run key) and update checks
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command():
    if FROZEN:
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def set_start_with_windows(enable: bool) -> str:
    if sys.platform != "win32":
        return "only on Windows"
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, "MapleFarmTracker", 0, winreg.REG_SZ, startup_command())
                return "will start with Windows"
            try:
                winreg.DeleteValue(k, "MapleFarmTracker")
            except FileNotFoundError:
                pass
            return "removed from startup"
    except OSError as e:
        return f"registry error: {e}"


def get_start_with_windows() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "MapleFarmTracker")
            return True
    except OSError:
        return False


class UpdateWorker(threading.Thread):
    """Polls GitHub releases; installs automatically when allowed (installed build only)."""

    def __init__(self, settings, on_info, every_sec=6 * 3600, first_delay=20):
        super().__init__(daemon=True)
        self.s = settings
        self.on_info = on_info
        self.every = every_sec
        self.first_delay = first_delay
        self.stop_flag = threading.Event()
        self.force = threading.Event()
        self.last = None

    def run(self):
        import updater
        self.stop_flag.wait(self.first_delay)
        while not self.stop_flag.is_set():
            self.force.clear()
            info = updater.check_latest(self.s.get("update_repo") or REPO)
            self.last = info
            self.on_info(info)
            if info and info.get("newer") and FROZEN and self.s.get("auto_update", True) and info.get("installer_url"):
                try:
                    path = updater.download_installer(info)
                    updater.run_installer(path)   # closes and restarts the app
                    return
                except Exception as e:
                    self.on_info({**info, "error": str(e)})
            woke = self.force.wait(self.every)
            if not woke:
                continue


# --------------------------------------------------------------------------
# Engine (worker thread)
# --------------------------------------------------------------------------
class Engine(threading.Thread):
    def __init__(self, settings, ui_queue=None, debug_dir=None):
        super().__init__(daemon=True)
        self.s = settings
        self.ui_queue = ui_queue          # requests to the Tk thread: ("hide"|"show", Event); None in console mode
        self.cmds = queue.Queue()         # commands from the UI
        self.debug_dir = debug_dir
        self.lock = threading.Lock()
        self.state = {"status": "starting", "log": []}
        self.stop_flag = threading.Event()
        self.paused = False
        self.hud_rect = None              # overlay rect, excluded from on-screen searches
        self.update_info = None

    # ---- helpers ----------------------------------------------------------
    def _ui(self, what, timeout=3.0):
        if self.ui_queue is None:
            return
        ev = threading.Event()
        self.ui_queue.put((what, ev))
        ev.wait(timeout)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _log(self, msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        with self.lock:
            self.state.setdefault("log", []).append(line)
            self.state["log"] = self.state["log"][-80:]
        if self.debug_dir:
            print(line, file=sys.stderr, flush=True)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _grid_from_rect(self, r):
        L, T, W, H = r
        pitch = W / 4.0
        return {"x0": float(L), "y0": float(T), "pitch": pitch, "size": pitch * 0.94}

    # ---- locating ---------------------------------------------------------
    def _locate(self, grab, ocr):
        """Hide the overlay, grab the whole monitor, find EXP bar + quickslot."""
        self._ui("hide")
        try:
            time.sleep(0.25)
            m = grab.monitor
            full = grab.grab((m["left"], m["top"], m["width"], m["height"]))
        finally:
            self._ui("show")
        if self.debug_dir:
            full.save(self.debug_dir / "hud_full.png")
        W, H = full.size
        exp_rect, hint = None, None
        if self.s.get("exp_region"):
            exp_rect = tuple(self.s["exp_region"])
        else:
            strip = full.crop((0, int(H * 0.70), W, H))
            exp_rect, hint = locate_exp_region(ocr, strip, (m["left"], m["top"] + int(H * 0.70)), self.debug_dir)
            if not exp_rect:   # windowed game higher up the screen: scan everything once
                exp_rect, hint = locate_exp_region(ocr, full, (m["left"], m["top"]), self.debug_dir)
        if self.s.get("quickslot"):
            q = self.s["quickslot"]
            grid = self._grid_from_rect(q) if isinstance(q, list) else dict(q)
        else:
            half = full.crop((0, int(H * 0.5), W, H))
            log = []
            grid = find_quickslot(ocr, half, (m["left"], m["top"] + int(H * 0.5)), log)
            for line in log:
                self._log("quickslot: " + line)
        map_rect = (m["left"], m["top"], int(W * 0.30), int(H * 0.15))
        return exp_rect, hint, grid, map_rect

    def _scan_mesos(self, grab, ocr):
        if self.s.get("mesos_region"):
            return tuple(self.s["mesos_region"])
        m = grab.monitor
        full = grab.grab((m["left"], m["top"], m["width"], m["height"]))
        rect, _ = find_mesos(ocr, full, (m["left"], m["top"]), self.hud_rect)
        return rect

    # ---- main loop --------------------------------------------------------
    def run(self):
        try:
            self._run()
        except Exception as e:  # keep the UI alive and show the error
            import traceback
            self._log("engine crashed: " + "".join(traceback.format_exception(e)).strip().splitlines()[-1])
            self._set(status="error: " + str(e))

    def _run(self):
        self._set(status="loading OCR...")
        ocr = Ocr()
        grab = Grabber(int(self.s.get("monitor", 1)))
        m = grab.monitor
        self._set(status="locating EXP bar / quickslot...", monitor=(m["left"], m["top"], m["width"], m["height"]))
        exp_rect = grid = None
        hint = None
        map_rect = None
        n_monitors = max(1, len(grab.sct.monitors) - 1)
        order = [int(self.s.get("monitor", 1))] + [i for i in range(1, n_monitors + 1) if i != int(self.s.get("monitor", 1))]
        for attempt in range(3):
            for mon in order:          # try the configured monitor, then the others
                grab = Grabber(mon)
                m = grab.monitor
                exp_rect, hint, grid, map_rect = self._locate(grab, ocr)
                if exp_rect:
                    if mon != int(self.s.get("monitor", 1)):
                        self.s["monitor"] = mon
                        self._log(f"game found on monitor {mon}")
                    break
            if exp_rect:
                break
            self._set(status=f"EXP bar not found (try {attempt + 1}/3) - is the game visible and windowed?")
            self.stop_flag.wait(3)
        if not exp_rect:
            self._set(status="EXP bar not found. Re-locate (menu) when the game is visible.")
        self._log(f"EXP region {exp_rect} level hint {hint}; quickslot {grid and {kk: round(v, 1) for kk, v in grid.items() if kk != 'keys_seen'}}")
        self._set(exp_rect=exp_rect, grid=grid, status="tracking" if exp_rect else self.state["status"])

        win = int(self.s.get("pots_window_min", 15)) * 60
        session = Session(float(self.s.get("window_min", 5)))
        slots = SlotTracker(list(KEY_POS), window_sec=win)
        mesos = MesoTracker(window_sec=win)
        maps = MapWatcher()
        seg_pots = {}        # per open segment: key -> [(t, count)]
        seg_mesos = {}       # per open segment: [(t, mesos)]

        def seg_extras(seg):
            """Potion cost and meso delta of a closing segment."""
            dur_h = max(1.0, seg["last_gain"] - seg["start"]) / 3600.0
            cost = None
            for key, pts in seg_pots.items():
                price = float(self.s["slots"].get(key, {}).get("price", 0) or 0)
                rate = theil_sen_per_hour(pts, min_dt=30)
                if price and rate is not None and rate < 0:
                    cost = (cost or 0.0) + (-rate) * dur_h * price
            delta = None
            pts = [p for p in seg_mesos.get("pts", []) if seg["start"] <= p[0] <= seg["last_gain"] + 60]
            if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 60:
                delta = pts[-1][1] - pts[0][1]
            return {"pots_cost": cost, "mesos": delta}

        glog = GrindLog(HERE / "grind_log.csv", float(self.s.get("grind_min_minutes", 1.0)),
                        float(self.s.get("grind_min_rate", 50.0)), int(self.s.get("grind_idle_sec", 120)),
                        extras=seg_extras)
        csv_path = HERE / f"maple_exp_{datetime.now():%Y-%m-%d_%H%M}.csv"
        csv_f = open(csv_path, "a", newline="", encoding="utf-8")
        csv_f.write("time,level,exp,pct,gained_total,avg_exp_per_min,rolling_exp_per_min,map,mesos,ocr_text\n")
        self._set(csv=str(csv_path))

        build = None
        if maple_build is not None and (HERE / "build.json").exists():
            try:
                build = maple_build.load_build(HERE / "build.json")
            except Exception as e:
                self._log(f"build.json not usable: {e}")
        model_cache = {}
        n = 0
        fails = 0
        meso_rect = None
        meso_fails = 0
        last_map_t = last_meso_scan = 0.0
        last_text = ""
        interval = float(self.s.get("interval", 10))
        slots_every = max(1, int(self.s.get("slots_every", 3)))
        cur_seg = None
        while not self.stop_flag.is_set():
            t = time.time()
            # ---- commands from the UI ----
            try:
                while True:
                    cmd = self.cmds.get_nowait()
                    if cmd == "pause":
                        self.paused = not self.paused
                        self._set(status="paused" if self.paused else "tracking")
                    elif cmd == "reset":
                        glog.flush(t)
                        session = Session(float(self.s.get("window_min", 5)))
                        self._log("session reset")
                    elif cmd == "relocate":
                        exp_rect, hint, grid, map_rect = self._locate(grab, ocr)
                        meso_rect = None
                        self._set(exp_rect=exp_rect, grid=grid)
                        self._log(f"re-located: exp {exp_rect} grid {bool(grid)}")
                        fails = 0
                    elif cmd == "quit":
                        self.stop_flag.set()
            except queue.Empty:
                pass
            if self.stop_flag.is_set():
                break
            if self.paused or not exp_rect:
                self.stop_flag.wait(1.0)
                continue

            # ---- EXP ----
            crop = grab.grab(exp_rect)
            expected = tol = None
            if session.last:
                dt = t - session.last[0]
                per_sec = session.rolling_rate() / 60.0
                expected = session.last[2] + per_sec * dt
                tol = max(0.01 * EXP_TABLE[session.last[1]], 3 * per_sec * dt + 100)
            prev_for_read = session.last[1] if (session.last and fails < 12) else None
            r = read_exp_from_line(ocr, crop, prev_for_read, hint, expected, tol, self.debug_dir)
            if r and r[0] is not None:
                lv, exp, pct, last_text = r
                fails = 0
                if session.add(t, lv, exp, pct):
                    glog.on_sample(t, lv, session.gained)
                    csv_f.write(f"{datetime.fromtimestamp(t).isoformat(timespec='seconds')},{lv},{exp},{pct},"
                                f"{session.gained},{session.avg_rate():.1f},{session.rolling_rate():.1f},"
                                f"{(maps.current or '').replace(',', ' ')},{mesos.value if mesos.value is not None else ''},"
                                f"{last_text.replace(',', ' ')}\n")
                    csv_f.flush()
            else:
                last_text = r[3] if r else ""
                session.bad += 1
                fails += 1
                if fails % 6 == 0:
                    new_rect, new_hint, new_grid, map_rect = self._locate(grab, ocr)
                    if new_rect:
                        exp_rect, hint = new_rect, (new_hint or hint)
                        grid = new_grid or grid
                        self._set(exp_rect=exp_rect, grid=grid)
                        self._log(f"re-located after {fails} failed reads: {exp_rect}")
            glog.on_tick(t)
            if glog.seg is not cur_seg:          # segment started or closed: reset per-segment buffers
                cur_seg = glog.seg
                seg_pots.clear()
                seg_mesos.clear()

            # ---- quickslot counts ----
            if grid and n % slots_every == 0:
                for key in KEY_POS:
                    x, y, s_, _ = cell_rect(grid, key)
                    cell = grab.grab((x, y, s_, s_))
                    before = slots.count.get(key)
                    val = read_slot_count(ocr, cell, before)
                    slots.update(key, val, t)
                    after = slots.count.get(key)
                    if after is not None and glog.seg is not None:
                        seg_pots.setdefault(key, []).append((t, after))
                    if self.debug_dir and before != after:
                        self._log(f"slot {key}: {before} -> {after} (read {val})")

            # ---- mesos ----
            if meso_rect:
                v = read_mesos(ocr, grab.grab(meso_rect))
                if v is None:
                    meso_fails += 1
                    if meso_fails >= 3:            # inventory closed or moved
                        meso_rect = None
                        self._log("mesos: readout lost (inventory closed?)")
                else:
                    meso_fails = 0
                    if mesos.update(v, t) and glog.seg is not None:
                        seg_mesos.setdefault("pts", []).append((t, v))
            elif t - last_meso_scan >= float(self.s.get("mesos_scan_sec", 30)):
                last_meso_scan = t
                meso_rect = self._scan_mesos(grab, ocr)
                meso_fails = 0
                if meso_rect:
                    self._log(f"mesos: readout locked at {meso_rect}")

            # ---- map name ----
            if map_rect and t - last_map_t >= float(self.s.get("map_every_sec", 15)):
                last_map_t = t
                name, _lines = read_map_name(ocr, grab.grab(map_rect))
                if maps.update(name, _lines):
                    self._log(f"map: {maps.current}")
                    glog.set_map(maps.current, t)
                elif maps.current and glog.map is None:
                    glog.set_map(maps.current, t)

            # ---- state for the UI ----
            pots = {}
            cost_per_h = 0.0
            for key in KEY_POS:
                c = slots.count.get(key)
                if c is None:
                    continue
                rate = theil_sen_per_hour(list(slots.hist[key]))
                used_per_h = -rate if rate is not None else None
                cfg = self.s["slots"].get(key, {})
                price = float(cfg.get("price", 0) or 0)
                mpm = (used_per_h * price / 60.0) if (used_per_h and used_per_h > 0 and price) else None
                if mpm:
                    cost_per_h += mpm * 60
                pots[key] = {"name": cfg.get("name", key), "count": c, "used_per_h": used_per_h, "price": price,
                             "mesos_per_min": mpm, "min_left": (c / used_per_h * 60.0) if (used_per_h and used_per_h > 0) else None}
            mesos_per_h = theil_sen_per_hour(list(mesos.hist))
            farm, farm_min = session.farm_rate(float(self.s.get("grind_min_rate", 50.0)), int(self.s.get("farm_window_min", 15)))
            model = None
            if build is not None and maps.current:
                rate_h = (farm or session.avg_rate()) * 60
                key = (maps.current, int(rate_h // 2000))
                if key not in model_cache:
                    try:
                        model_cache[key] = maple_build.model_line(build, maps.current, rate_h if rate_h > 0 else None)
                    except Exception as e:
                        model_cache[key] = f"model error: {e}"
                model = model_cache[key]
            seg_cost = None
            seg_mesos_delta = None
            if glog.seg is not None:
                ex = seg_extras(glog.seg)
                seg_cost, seg_mesos_delta = ex["pots_cost"], ex["mesos"]
            self._set(
                status="tracking",
                t=t, level=session.last[1] if session.last else None,
                exp=session.last[2] if session.last else None,
                pct=session.last[3] if session.last else None,
                need=EXP_TABLE[session.last[1]] if session.last else None,
                elapsed=session.elapsed, gained=session.gained, lost=session.lost, levelups=session.levelups,
                avg=session.avg_rate(), rolling=session.rolling_rate(), farm=farm, farm_min=farm_min,
                ok=session.ok, bad=session.bad,
                ocr=last_text, pots=pots, pots_cost_per_h=cost_per_h,
                mesos=mesos.value, mesos_per_h=mesos_per_h, mesos_locked=bool(meso_rect),
                map=maps.current, seg=glog.current(t), seg_cost=seg_cost, seg_mesos=seg_mesos_delta, model=model,
                grind_recent=glog.recent(40), grind_per_map=glog.per_map(),
            )
            n += 1
            self.stop_flag.wait(max(0.5, interval - (time.time() - t)))

        glog.flush(time.time())
        csv_f.close()
        summ = session.summary()
        summ["current_map"] = maps.current
        summ["mesos_last"] = mesos.value
        summ["maps_this_session"] = glog.session_summary()
        summ["grind_sessions_logged"] = glog.session_rows
        (csv_path.with_suffix(".summary.json")).write_text(json.dumps(summ, indent=2), encoding="utf-8")
        self._set(status="stopped", summary=summ)


# --------------------------------------------------------------------------
# Text lines shared by the overlay and the console
# --------------------------------------------------------------------------
def build_lines(st, settings):
    """Returns [(text, style)] with style in head/normal/dim/ok/warn."""
    L = []
    if st.get("level") is None:
        L.append(("Maple HUD  " + st.get("status", ""), "head"))
        L.append(("waiting for a clean EXP read...", "normal"))
        L.append((f"ocr: {st.get('ocr', '')[:40]!r}", "dim"))
        return L
    lv, exp, pct, need = st["level"], st["exp"], st["pct"], st["need"]
    avg, roll = st["avg"], st["rolling"]
    rem = need - exp
    eta_a = rem / avg if avg > 0 else float("inf")
    L.append((f"Lv {lv}  {exp:,} / {need:,}  {pct:.2f} %   {datetime.now():%H:%M:%S}", "head"))
    farm, fmin = st.get("farm"), st.get("farm_min") or 0
    if farm is not None:
        L.append((f"FARM {farm:,.0f}/min  {k(farm * 60)}/h  (last {fmin} active min, trimmed)    "
                  f"session AVG {avg:,.0f}/min  {fmt_min(st['elapsed'] / 60)}  +{st['gained']:,}"
                  + (f"  lost {st['lost']:,}" if st.get("lost") else ""), "normal"))
    else:
        L.append((f"AVG {avg:,.0f}/min  {k(avg * 60)}/h    last {int(settings.get('window_min', 5))}m {roll:,.0f}/min    "
                  f"{fmt_min(st['elapsed'] / 60)}  +{st['gained']:,}  (farm rate after 2 active minutes)", "normal"))
    base = farm if farm else (roll if roll > 0 else avg)
    eta_f = rem / base if base > 0 else float("inf")
    L.append((f"To Lv {lv + 1}: {rem:,}  ETA {fmt_min(eta_f)} at farm rate / {fmt_min(eta_a)} at session avg"
              + (f"   level-ups {st['levelups']}" if st.get("levelups") else ""), "normal"))
    # pots
    pots = st.get("pots") or {}
    tracked = {k2: p for k2, p in pots.items() if k2 in settings["slots"]}
    others = {k2: p for k2, p in pots.items() if k2 not in settings["slots"]}
    thr = int(settings.get("low_threshold", 30))
    cost_h = st.get("pots_cost_per_h") or 0.0
    if tracked:
        parts = []
        for key2, p in tracked.items():
            s2 = f"{p['name']} {p['count']}"
            r = p["used_per_h"]
            if r is not None and abs(r) >= 0.5:
                s2 += f" ({'-' if r > 0 else '+'}{abs(r):.0f}/h"
                if p["mesos_per_min"]:
                    s2 += f" = {p['mesos_per_min']:,.0f}/min"
                s2 += ")"
            parts.append(s2)
        low = any(p["count"] <= thr for p in tracked.values())
        L.append(("Pots " + " · ".join(parts), "warn" if low else "normal"))
    elif st.get("grid"):
        L.append(("Pots: reading quickslot...", "dim"))
    else:
        L.append(("Pots: quickslot not found (menu > Re-locate, or Set region > Quickslot)", "dim"))
    # mesos / net
    mv = st.get("mesos")
    mph = st.get("mesos_per_h")
    if mv is not None:
        txt = f"Mesos {mv:,}"
        if mph is not None:
            txt += f"  {'+' if mph >= 0 else '-'}{abs(mph) / 60:,.0f}/min gross"
        if cost_h:
            txt += f"  -{cost_h / 60:,.0f}/min pots"
        if mph is not None:
            net = mph - cost_h
            txt += f"  = {'+' if net >= 0 else '-'}{abs(net) / 60:,.0f}/min net ({k(net)}/h)"
        elif cost_h:
            txt += "  (rate: measuring...)"
        if not st.get("mesos_locked"):
            txt += "  [last seen]"
        L.append((txt, "normal"))
    else:
        L.append((f"Mesos: open the inventory (I) once to track  ·  pots -{cost_h / 60:,.0f}/min ({k(cost_h)}/h)"
                  if cost_h else "Mesos: open the inventory (I) once to track", "dim"))
    if others:
        L.append(("Other slots: " + " · ".join(f"{p['name']} {p['count']}" for p in others.values()), "dim"))
    # map / segment
    seg = st.get("seg")
    mp = st.get("map") or "map: ? (minimap closed?)"
    if seg:
        cost, md = st.get("seg_cost"), st.get("seg_mesos")
        txt = f"Farming {mp}  ·  {seg[1]:.0f} min, {seg[3]:,.0f} EXP/min ({k(seg[3] * 60)}/h)"
        if cost:
            txt += f", pots {cost / max(seg[1], 0.1):,.0f}/min"
        if md is not None:
            txt += f", mesos {'+' if md >= 0 else '-'}{abs(md):,}"
        L.append((txt, "ok"))
    else:
        L.append((f"Farming {mp}  ·  idle (no EXP for a while)", "dim"))
    if st.get("model"):
        L.append(("Model (solo, from build.json): " + st["model"], "dim"))
    upd = st.get("update")
    tail = f"   update v{upd['version']} available" if upd and upd.get("newer") else ""
    L.append((f"reads ok {st['ok']} bad {st['bad']}   {st.get('status', '')}   v{__version__}{tail}", "dim"))
    return L


# --------------------------------------------------------------------------
# Console mode
# --------------------------------------------------------------------------
def run_console(settings, debug_dir):
    _make_dpi_aware()
    engine = Engine(settings, None, debug_dir)
    import signal

    def _stop(*_):
        engine.stop_flag.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)
    print(f"{APP_TITLE} v{__version__} - console  (Ctrl+C to stop)   settings: {SETTINGS_PATH}", flush=True)
    engine.start()
    UpdateWorker(settings, lambda info: engine._set(update=info)).start()
    live = LiveBlock(True)
    cols = int(settings.get("columns", 88)) + 20
    while engine.is_alive():
        st = engine.snapshot()
        lines = [t if len(t) <= cols else t[:cols - 1] + "…" for t, _ in build_lines(st, settings)]
        lines.append(f"grind log: {HERE / 'grind_log.csv'}   session csv: {st.get('csv', '-')}")
        live.show("\n".join(lines))
        engine.stop_flag.wait(1.0)
        if engine.stop_flag.is_set():
            engine.join(timeout=8)
            break
    st = engine.snapshot()
    print()
    if st.get("summary"):
        print(json.dumps(st["summary"], indent=2))


# --------------------------------------------------------------------------
# Overlay (tkinter)
# --------------------------------------------------------------------------
def run_ui(settings, debug_dir, open_settings=False):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk

    _make_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    root.title(APP_TITLE)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", float(settings.get("alpha", 0.92)))
    except tk.TclError:
        pass
    BG, FG, DIM, WARN, OK = "#0f1216", "#e8e8e8", "#8a949e", "#ff6b6b", "#8ce99a"
    STYLE = {"head": FG, "normal": FG, "dim": DIM, "ok": OK, "warn": WARN}
    root.configure(bg=BG)
    fs = int(settings.get("font_size", 10))
    mono = ("Consolas", fs) if sys.platform == "win32" else ("DejaVu Sans Mono", fs)
    mono_b = (mono[0], fs, "bold")
    COLS = int(settings.get("columns", 88))

    def fit(txt):
        return txt if len(txt) <= COLS else txt[:COLS - 1] + "…"

    frame = tk.Frame(root, bg=BG, padx=8, pady=5, highlightthickness=1, highlightbackground="#2a3139")
    frame.pack(fill="both", expand=True)
    NLINES = 9
    lines = []
    for i in range(NLINES):
        lbl = tk.Label(frame, text="", bg=BG, fg=FG, font=mono_b if i == 0 else mono, anchor="w", justify="left")
        lbl.pack(fill="x")
        lines.append(lbl)

    ui_queue = queue.Queue()
    engine = Engine(settings, ui_queue, debug_dir)

    # ---- dragging --------------------------------------------------------
    drag = {"x": 0, "y": 0}

    def on_press(e):
        drag["x"], drag["y"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()

    def on_drag(e):
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    def on_release(e):
        settings["pos"] = [root.winfo_x(), root.winfo_y()]
        save_settings(settings)

    def center(win, w_, h_):
        """Place a secondary window mid-screen (never over the minimap at top-left)."""
        sw_, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
        win.geometry(f"{w_}x{h_}+{max(0, (sw_ - w_) // 2)}+{max(0, (sh_ - h_) // 2)}")

    # ---- region picker ---------------------------------------------------
    def pick_region(prompt):
        """Fullscreen translucent window; drag a rectangle; returns (L, T, W, H) or None."""
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        top.geometry(f"{sw}x{sh}+0+0")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        cv = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        cv.create_text(sw // 2, 60, text=prompt + "   (drag a box around it, Esc = cancel)", fill="#ffd166",
                       font=("Segoe UI", 18) if sys.platform == "win32" else ("DejaVu Sans", 18))
        res = {}
        st = {}

        def press(e):
            st["x"], st["y"] = e.x_root, e.y_root

        def dragm(e):
            cv.delete("box")
            cv.create_rectangle(st.get("x", e.x_root), st.get("y", e.y_root), e.x_root, e.y_root,
                                outline="#ffd166", width=3, tags="box")

        def release(e):
            if "x" in st:
                x0, y0 = min(st["x"], e.x_root), min(st["y"], e.y_root)
                w, h = abs(e.x_root - st["x"]), abs(e.y_root - st["y"])
                if w > 5 and h > 5:
                    res["rect"] = (x0, y0, w, h)
            top.destroy()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", dragm)
        cv.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: top.destroy())
        top.focus_force()
        root.wait_window(top)
        return res.get("rect")

    def set_region(which):
        root.withdraw()
        try:
            prompts = {"exp_region": "EXP readout on the status bar (e.g.  11,323 [17.79%] )",
                       "quickslot": "the whole 2x4 quickslot grid (Shift..PgUp / Ctrl..PgDn)",
                       "mesos_region": "the meso number in the inventory window (open it first)"}
            rect = pick_region(prompts[which])
        finally:
            root.deiconify()
            root.attributes("-topmost", True)
        if rect:
            settings[which] = list(rect)
            save_settings(settings)
            engine.cmds.put("relocate")

    def clear_regions():
        for kk in ("exp_region", "quickslot", "mesos_region"):
            settings[kk] = None
        save_settings(settings)
        engine.cmds.put("relocate")

    # ---- settings window -------------------------------------------------
    upd = {"worker": None}

    def on_update_info(info):
        engine._set(update=info)

    def install_update():
        info = engine.snapshot().get("update")
        if not (info and info.get("newer")):
            return
        if not FROZEN:
            import webbrowser
            webbrowser.open(info.get("html_url", f"https://github.com/{settings.get('update_repo') or REPO}/releases"))
            return

        def work():
            import updater
            try:
                engine._set(status=f"downloading v{info['version']}...")
                path = updater.download_installer(info)
                engine._set(status="installing update...")
                updater.run_installer(path)
            except Exception as e:
                engine._set(status=f"update failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def settings_window():
        w = tk.Toplevel(root)
        w.title(f"{APP_TITLE} - settings  (v{__version__})")
        w.attributes("-topmost", True)
        w.configure(bg=BG)
        center(w, 760, 560)
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        style = ttk.Style(w)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        msg = tk.Label(w, text="", bg=BG, fg=OK, font=mono, anchor="w")
        msg.pack(fill="x", padx=12, pady=(0, 8))

        def say(t):
            msg.configure(text=t)

        # General
        g = tk.Frame(nb, bg=BG, padx=12, pady=10)
        nb.add(g, text="General")
        vars_ = {}
        rows = [("interval", "Seconds between EXP reads", 10), ("slots_every", "Read pots every N EXP reads", 3),
                ("map_every_sec", "Map name read interval (s)", 15), ("window_min", "Rolling EXP window (min)", 5),
                ("farm_window_min", "Farm rate: active minutes used", 15), ("pots_window_min", "Pot usage window (min)", 15),
                ("low_threshold", "Pots turn red at", 30), ("monitor", "Monitor index (1 = primary; auto-tries others)", 1),
                ("font_size", "Overlay font size", 10), ("alpha", "Overlay opacity (0.5-1)", 0.92), ("columns", "Overlay width (chars)", 88)]
        for i, (key, label, default) in enumerate(rows):
            tk.Label(g, text=label, bg=BG, fg=FG, font=mono).grid(row=i, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(settings.get(key, default)))
            tk.Entry(g, textvariable=v, width=10, font=mono).grid(row=i, column=1, padx=10, pady=2)
            vars_[key] = v
        sw = tk.BooleanVar(value=get_start_with_windows())
        tk.Checkbutton(g, text="Start with Windows", variable=sw, bg=BG, fg=FG, selectcolor="#2a3139", activebackground=BG, font=mono).grid(row=len(rows), column=0, sticky="w", pady=(8, 2))

        def save_general():
            for key, v in vars_.items():
                raw = v.get().strip()
                try:
                    settings[key] = float(raw) if ("." in raw or key == "alpha") else int(raw)
                except ValueError:
                    say(f"bad value for {key}: {raw!r}")
                    return
            settings["start_with_windows"] = bool(sw.get())
            note = set_start_with_windows(bool(sw.get()))
            save_settings(settings)
            say(f"saved (restart the app to apply font/opacity/width). {note}")
        tk.Button(g, text="Save", command=save_general, font=mono).grid(row=len(rows) + 1, column=0, sticky="w", pady=10)

        # Pots
        pf = tk.Frame(nb, bg=BG, padx=12, pady=10)
        nb.add(pf, text="Pots")
        tk.Label(pf, text="Tick the quickslot keys that hold potions, name them, set what you pay per unit.", bg=BG, fg=FG, font=mono).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        for c, h in enumerate(("Key", "Count now", "Item", "Price", "Pot?")):
            tk.Label(pf, text=h, bg=BG, fg=DIM, font=mono).grid(row=1, column=c, padx=6, sticky="w")
        prow = {}
        pots_now = engine.snapshot().get("pots") or {}
        for i, key in enumerate([kk for row in KEYS for kk in row]):
            cfg = settings["slots"].get(key)
            cnt = pots_now.get(key, {}).get("count")
            tk.Label(pf, text=key, bg=BG, fg=FG, font=mono).grid(row=2 + i, column=0, padx=6, sticky="w")
            tk.Label(pf, text=str(cnt) if cnt is not None else "-", bg=BG, fg=DIM, font=mono).grid(row=2 + i, column=1, padx=6)
            name = tk.StringVar(value=cfg["name"] if cfg else "")
            price = tk.StringVar(value=str(int(cfg["price"])) if cfg and cfg.get("price") else "")
            on = tk.BooleanVar(value=bool(cfg))
            tk.Entry(pf, textvariable=name, width=16, font=mono).grid(row=2 + i, column=2, padx=6, pady=2)
            tk.Entry(pf, textvariable=price, width=8, font=mono).grid(row=2 + i, column=3, padx=6, pady=2)
            tk.Checkbutton(pf, variable=on, bg=BG, activebackground=BG, selectcolor="#2a3139").grid(row=2 + i, column=4)
            prow[key] = (name, price, on)

        def save_pots():
            new = {}
            for key, (name, price, on) in prow.items():
                if on.get():
                    try:
                        pr = float(price.get() or 0)
                    except ValueError:
                        pr = 0.0
                    new[key] = {"name": name.get().strip() or key, "price": pr}
            settings["slots"] = new
            save_settings(settings)
            say("pots saved: " + (", ".join(f"{kk}={v['name']}@{v['price']:.0f}" for kk, v in new.items()) or "none"))
        tk.Button(pf, text="Save", command=save_pots, font=mono).grid(row=11, column=0, sticky="w", pady=10)

        # meowdb
        mf = tk.Frame(nb, bg=BG, padx=12, pady=10)
        nb.add(mf, text="meowdb")
        tk.Label(mf, text="Sign in to meowdb (Google) once; the tracker uses the session cookie only for\nGET /api/me/export (your builds) - nothing else. Stored locally. Sessions expire;\nsign in again if sync fails.",
                 bg=BG, fg=FG, font=mono, justify="left", anchor="w").grid(row=0, column=0, columnspan=3, sticky="w")
        cookie_path = data("meowdb_cookie.txt")
        cv = tk.StringVar(value=cookie_path.read_text(encoding="utf-8").strip() if cookie_path.exists() else "")
        status_var = tk.StringVar(value="not checked")
        tk.Label(mf, text="status:", bg=BG, fg=DIM, font=mono).grid(row=1, column=0, sticky="w", pady=(6, 2))
        tk.Label(mf, textvariable=status_var, bg=BG, fg=FG, font=mono).grid(row=1, column=1, sticky="w", pady=(6, 2))

        def set_status(s: str):
            status_var.set(s)

        def sign_in():
            def work():
                root.after(0, lambda: set_status("opening meowdb..."))
                try:
                    import meowdb
                    detail = meowdb.login_via_webview()
                    if cookie_path.exists():
                        new_cookie = cookie_path.read_text(encoding="utf-8").strip()
                        root.after(0, lambda: cv.set(new_cookie))
                    root.after(0, lambda: set_status(detail))
                    root.after(0, lambda: say("meowdb: " + detail))
                except Exception as e:
                    err = str(e)
                    root.after(0, lambda: set_status(err))
                    root.after(0, lambda: say("meowdb sign-in failed: " + err))
            threading.Thread(target=work, daemon=True).start()

        def test_cookie():
            def work():
                root.after(0, lambda: set_status("testing..."))
                try:
                    import meowdb
                    ok, detail = meowdb.verify(cv.get().strip() or None)
                    root.after(0, lambda: set_status(detail))
                    root.after(0, lambda: say("meowdb: " + detail))
                except Exception as e:
                    err = str(e)
                    root.after(0, lambda: set_status(err))
            threading.Thread(target=work, daemon=True).start()

        def save_cookie():
            cookie_path.write_text(cv.get().strip(), encoding="utf-8")
            say("cookie saved to " + str(cookie_path))

        def sync_now():
            def work():
                try:
                    import meowdb
                    summary, _b = meowdb.export(cv.get().strip() or None)
                    root.after(0, lambda: say("synced: " + summary))
                except Exception as e:
                    err = str(e)
                    root.after(0, lambda: say(f"sync failed: {err}"))
            say("syncing...")
            threading.Thread(target=work, daemon=True).start()

        tk.Button(mf, text="Sign in to meowdb (Google)", command=sign_in, font=mono).grid(row=2, column=0, sticky="w", pady=6)
        tk.Button(mf, text="Test cookie", command=test_cookie, font=mono).grid(row=2, column=1, sticky="w", pady=6)
        tk.Button(mf, text="Sync now (export -> build.json)", command=sync_now, font=mono).grid(row=2, column=2, sticky="w", pady=6, padx=(8, 0))

        tk.Label(mf, text="Fallback: paste the full Cookie header from DevTools (meowdb.com request).",
                 bg=BG, fg=DIM, font=mono, justify="left").grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 2))
        tk.Entry(mf, textvariable=cv, width=70, font=mono, show="•").grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        tk.Button(mf, text="Save pasted cookie", command=save_cookie, font=mono).grid(row=4, column=2, sticky="w", pady=4, padx=(8, 0))
        tk.Label(mf, text=f"build.json: {data('build.json')}", bg=BG, fg=DIM, font=mono).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        # Updates
        uf = tk.Frame(nb, bg=BG, padx=12, pady=10)
        nb.add(uf, text="Updates")
        au = tk.BooleanVar(value=bool(settings.get("auto_update", True)))
        tk.Checkbutton(uf, text="Install new releases automatically (installed build)", variable=au, bg=BG, fg=FG, selectcolor="#2a3139", activebackground=BG, font=mono).grid(row=0, column=0, sticky="w")
        rv = tk.StringVar(value=settings.get("update_repo") or REPO)
        tk.Label(uf, text="GitHub repo (owner/name)", bg=BG, fg=FG, font=mono).grid(row=1, column=0, sticky="w", pady=4)
        tk.Entry(uf, textvariable=rv, width=48, font=mono).grid(row=1, column=1, padx=8)
        info = engine.snapshot().get("update")
        ul = tk.Label(uf, text=(f"installed v{__version__}; latest v{info['version']}" + (" - update available" if info.get("newer") else " - up to date")) if info else f"installed v{__version__}; not checked yet",
                      bg=BG, fg=DIM, font=mono)
        ul.grid(row=2, column=0, columnspan=2, sticky="w", pady=6)

        def save_updates():
            settings["auto_update"] = bool(au.get())
            settings["update_repo"] = rv.get().strip() or REPO
            save_settings(settings)
            say("update settings saved")

        def check_now():
            import updater
            inf = updater.check_latest(rv.get().strip() or REPO)
            engine._set(update=inf)
            ul.configure(text=(f"installed v{__version__}; latest v{inf['version']}" + (" - update available" if inf.get("newer") else " - up to date")) if inf else "could not reach GitHub")
        tk.Button(uf, text="Save", command=save_updates, font=mono).grid(row=3, column=0, sticky="w", pady=6)
        tk.Button(uf, text="Check now", command=check_now, font=mono).grid(row=3, column=1, sticky="w", pady=6)
        tk.Button(uf, text="Install update", command=install_update, font=mono).grid(row=4, column=0, sticky="w", pady=6)
        tk.Label(uf, text=f"data folder: {DATA_DIR}", bg=BG, fg=DIM, font=mono).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

    # ---- pots setup dialog -----------------------------------------------
    def pots_dialog():
        w = tk.Toplevel(root)
        w.title(f"{APP_TITLE} - pots setup")
        w.attributes("-topmost", True)
        w.configure(bg=BG)
        center(w, 620, 420)
        st = engine.snapshot()
        pots = st.get("pots") or {}
        tk.Label(w, text="Tick the slots that are potions, name them and set what you pay per unit (NPC price).",
                 bg=BG, fg=FG, font=mono).grid(row=0, column=0, columnspan=5, padx=10, pady=(10, 6), sticky="w")
        for c, h in enumerate(("Key", "Count now", "Item name", "Price (mesos)", "Pot?")):
            tk.Label(w, text=h, bg=BG, fg=DIM, font=mono).grid(row=1, column=c, padx=8, sticky="w")
        rows = {}
        for i, key in enumerate([kk for row in KEYS for kk in row]):
            cfg = settings["slots"].get(key)
            cnt = pots.get(key, {}).get("count")
            tk.Label(w, text=key, bg=BG, fg=FG, font=mono).grid(row=2 + i, column=0, padx=8, sticky="w")
            tk.Label(w, text=str(cnt) if cnt is not None else "-", bg=BG, fg=DIM, font=mono).grid(row=2 + i, column=1, padx=8)
            name = tk.StringVar(value=cfg["name"] if cfg else "")
            price = tk.StringVar(value=str(int(cfg["price"])) if cfg and cfg.get("price") else "")
            on = tk.BooleanVar(value=bool(cfg))
            tk.Entry(w, textvariable=name, width=16, font=mono).grid(row=2 + i, column=2, padx=8, pady=2)
            tk.Entry(w, textvariable=price, width=10, font=mono).grid(row=2 + i, column=3, padx=8, pady=2)
            tk.Checkbutton(w, variable=on, bg=BG, activebackground=BG, selectcolor="#2a3139").grid(row=2 + i, column=4)
            rows[key] = (name, price, on)
        msg = tk.Label(w, text="", bg=BG, fg=OK, font=mono)
        msg.grid(row=11, column=0, columnspan=5, padx=10, sticky="w")

        def save():
            new = {}
            for key, (name, price, on) in rows.items():
                if on.get():
                    try:
                        pr = float(price.get() or 0)
                    except ValueError:
                        pr = 0.0
                    new[key] = {"name": name.get().strip() or key, "price": pr}
            settings["slots"] = new
            save_settings(settings)
            msg.configure(text=f"saved: {', '.join(f'{kk}={v['name']}@{v['price']:.0f}' for kk, v in new.items()) or 'no pots'}")

        tk.Button(w, text="Save", command=save, font=mono).grid(row=12, column=0, columnspan=2, padx=10, pady=10, sticky="w")
        tk.Button(w, text="Close", command=w.destroy, font=mono).grid(row=12, column=3, columnspan=2, padx=10, pady=10, sticky="e")

    # ---- grind log window ------------------------------------------------
    grind_win = {"win": None}

    def open_grind():
        if grind_win["win"] and grind_win["win"].winfo_exists():
            grind_win["win"].lift()
            return
        w = tk.Toplevel(root)
        w.title(f"{APP_TITLE} - grind log (per map)")
        w.attributes("-topmost", True)
        w.configure(bg=BG)
        center(w, 1000, 560)
        style = ttk.Style(w)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background="#161b21", fieldbackground="#161b21", foreground=FG, rowheight=22, font=mono)
        style.configure("Treeview.Heading", background="#232a33", foreground=FG, font=mono_b)
        live = tk.Label(w, text="", bg=BG, fg=OK, font=mono, anchor="w")
        live.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(w, text="Per map. Farm EXP/h = active minutes only, arrival burst and idle trimmed; Raw = whole segment. Logged: >= 1 min at >= 50 EXP/min.", bg=BG, fg=DIM, font=mono, anchor="w").pack(fill="x", padx=8)
        cols = ("map", "sessions", "minutes", "farm_exp_per_hour", "exp_per_hour", "last", "pots_per_hour", "mesos_per_hour", "net_per_hour")
        heads = ("Map", "Sess", "Min", "Farm EXP/h", "Raw EXP/h", "Last EXP/h", "Pots/h", "Mesos/h", "Net/h")
        widths = (300, 50, 60, 100, 100, 100, 90, 100, 90)
        tm = ttk.Treeview(w, columns=cols, show="headings", height=6)
        for c, txt, wd in zip(cols, heads, widths):
            tm.heading(c, text=txt)
            tm.column(c, width=wd, anchor="e" if c != "map" else "w")
        tm.pack(fill="x", padx=8, pady=4)
        tk.Label(w, text="Recent sessions", bg=BG, fg=DIM, font=mono, anchor="w").pack(fill="x", padx=8)
        cols2 = ("start", "map", "minutes", "exp", "farm_exp_per_hour", "exp_per_hour", "levels", "pots_cost", "mesos", "net_per_hour")
        heads2 = ("Start", "Map", "Min", "EXP", "Farm EXP/h", "Raw EXP/h", "Lv", "Pots cost", "Mesos", "Net/h")
        widths2 = (140, 260, 50, 80, 90, 90, 60, 90, 90, 90)
        tr = ttk.Treeview(w, columns=cols2, show="headings", height=12)
        for c, txt, wd in zip(cols2, heads2, widths2):
            tr.heading(c, text=txt)
            tr.column(c, width=wd, anchor="e" if c not in ("start", "map") else "w")
        tr.pack(fill="both", expand=True, padx=8, pady=4)
        tk.Label(w, text=f"log file: {HERE / 'grind_log.csv'}", bg=BG, fg=DIM, font=mono, anchor="w").pack(fill="x", padx=8, pady=(0, 6))
        grind_win.update(win=w, tree_map=tm, tree_recent=tr, live=live)
        refresh_grind()

    def fmt_or_dash(v):
        try:
            return f"{int(float(v)):,}" if v not in ("", None) else "-"
        except ValueError:
            return "-"

    def refresh_grind():
        w = grind_win.get("win")
        if not (w and w.winfo_exists()):
            return
        st = engine.snapshot()
        seg = st.get("seg")
        if seg:
            cost, md = st.get("seg_cost"), st.get("seg_mesos")
            grind_win["live"].configure(
                text=f"LIVE  {seg[0]}: {seg[1]:.1f} min, {seg[2]:,} EXP, {seg[3]:,.0f} EXP/min ({k(seg[3] * 60)}/h)"
                     + (f", pots ~{cost:,.0f} mesos" if cost else "") + (f", mesos {md:+,}" if md is not None else ""))
        else:
            grind_win["live"].configure(text="LIVE  no active grind segment (needs EXP coming in)")
        tm, tr = grind_win["tree_map"], grind_win["tree_recent"]
        tm.delete(*tm.get_children())
        for a in st.get("grind_per_map", []):
            tm.insert("", "end", values=(a["map"], a["sessions"], f"{a['minutes']:.0f}", f"{a.get('farm_exp_per_hour', 0):,}",
                                         f"{a['exp_per_hour']:,}", fmt_or_dash(a["last"]), fmt_or_dash(a.get("pots_per_hour")),
                                         fmt_or_dash(a.get("mesos_per_hour")), fmt_or_dash(a.get("net_per_hour"))))
        tr.delete(*tr.get_children())
        for r in st.get("grind_recent", []):
            tr.insert("", "end", values=(r["start"].replace("T", " "), r["map"], r["minutes"], fmt_or_dash(r["exp"]),
                                         fmt_or_dash(r.get("farm_exp_per_hour")), fmt_or_dash(r["exp_per_hour"]), f"{r['level_start']}-{r['level_end']}",
                                         fmt_or_dash(r.get("pots_cost")), fmt_or_dash(r.get("mesos")), fmt_or_dash(r.get("net_per_hour"))))

    # ---- menu ------------------------------------------------------------
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Grind log (per map)", command=open_grind)
    menu.add_command(label="Settings (general, pots, meowdb sync, updates)...", command=settings_window)
    menu.add_command(label="Pots setup (slots, names, prices)...", command=pots_dialog)
    sub = tk.Menu(menu, tearoff=0)
    sub.add_command(label="EXP bar", command=lambda: set_region("exp_region"))
    sub.add_command(label="Quickslot grid", command=lambda: set_region("quickslot"))
    sub.add_command(label="Mesos (inventory number)", command=lambda: set_region("mesos_region"))
    sub.add_separator()
    sub.add_command(label="Clear all (auto-detect)", command=clear_regions)
    menu.add_cascade(label="Set region", menu=sub)
    menu.add_separator()
    menu.add_command(label="Pause / resume", command=lambda: engine.cmds.put("pause"))
    menu.add_command(label="Reset session", command=lambda: engine.cmds.put("reset"))
    menu.add_command(label="Re-locate (EXP bar / quickslot / mesos)", command=lambda: engine.cmds.put("relocate"))
    menu.add_separator()
    menu.add_command(label="Open data folder", command=lambda: os.startfile(DATA_DIR) if sys.platform == "win32" else None)
    menu.add_command(label="Install update (when one is available)", command=install_update)
    menu.add_command(label="Quit", command=lambda: quit_app())

    def popup(e):
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    for w_ in [frame] + lines:
        w_.bind("<ButtonPress-1>", on_press)
        w_.bind("<B1-Motion>", on_drag)
        w_.bind("<ButtonRelease-1>", on_release)
        w_.bind("<Double-Button-1>", lambda e: open_grind())
        w_.bind("<Button-3>", popup)

    def quit_app():
        engine.stop_flag.set()
        deadline = time.time() + 8

        def _wait():
            if engine.is_alive() and time.time() < deadline:
                root.after(100, _wait)
            else:
                root.destroy()
        _wait()

    # ---- render ----------------------------------------------------------
    def render():
        st = engine.snapshot()
        out = build_lines(st, settings)
        out.append(("drag me · right-click: menu · dbl-click: grind log", "dim"))
        for i in range(NLINES):
            if i < len(out):
                txt, style = out[i]
                lines[i].configure(text=fit(txt), fg=STYLE.get(style, FG))
            else:
                lines[i].configure(text="")
        try:
            engine.hud_rect = (root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height())
        except tk.TclError:
            pass
        root.after(1000, render)

    # ---- UI requests from the engine (hide/show during locate) -----------
    def poll_ui():
        try:
            while True:
                what, ev = ui_queue.get_nowait()
                if what == "hide":
                    root.withdraw()
                    root.update()
                elif what == "show":
                    root.deiconify()
                    root.attributes("-topmost", True)
                ev.set()
        except queue.Empty:
            pass
        root.after(50, poll_ui)

    def tick_grind():
        refresh_grind()
        root.after(5000, tick_grind)

    # ---- place the window ------------------------------------------------
    lines[0].configure(text="Maple HUD  starting...")
    lines[1].configure(text="loading OCR models (first time: a few seconds)")
    lines[NLINES - 1].configure(text="drag me · right-click: menu", fg=DIM)
    root.update_idletasks()
    w = tkfont.Font(font=mono).measure("0") * COLS + 22
    h = root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    pos = settings.get("pos")
    if pos and 0 <= pos[0] < sw - 50 and 0 <= pos[1] < sh - 50:
        x, y = int(pos[0]), int(pos[1])
    else:
        x, y = sw - w - 16, sh - h - 16
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.deiconify()

    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: root.after(0, quit_app))
        except Exception:
            pass
    engine.start()
    upd["worker"] = UpdateWorker(settings, on_update_info)
    upd["worker"].start()
    root.after(200, render)
    root.after(50, poll_ui)
    root.after(5000, tick_grind)
    if open_settings:
        root.after(800, settings_window)
    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()
    engine.stop_flag.set()


def selftest() -> int:
    """No screen needed: OCR engine loads, a rendered status bar parses, maps/build load."""
    from PIL import Image, ImageDraw, ImageFont
    from maple_slots import known_maps, snap_map
    t0 = time.time()
    ocr = Ocr()
    img = Image.new("RGB", (420, 36), (38, 38, 42))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
    d.text((8, 8), "EXP 21,066 [52.00%]", fill=(255, 255, 255), font=font)
    r = read_exp_from_line(ocr, img, None, None)
    exact = bool(r and r[0] == 24 and r[1] == 21066)
    read_something = bool(r and (r[3] or "").strip())
    maps = len(known_maps())
    snap = snap_map(["Kerning City Subway", "TTansfer Area"])
    print(f"{APP_TITLE} {__version__} selftest: ocr={'ok' if exact else ('read ' + repr(r[3]) if r else 'NOTHING')} "
          f"maps={maps} snap={snap!r} data={DATA_DIR} ({time.time() - t0:.1f}s)")
    return 0 if (read_something and maps > 400 and snap) else 1


def main():
    # Fast-path for the webview-login subprocess: the main app re-execs itself with this
    # flag to open the meowdb Sign-in window in a child process (keeps pywebview off the
    # Tk mainloop). Handle before argparse so unknown-arg errors don't kill the login flow.
    if "--meowdb-login" in sys.argv:
        i = sys.argv.index("--meowdb-login")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        import webview_login
        sys.exit(webview_login.run(out))
    ap = argparse.ArgumentParser(description=f"{APP_TITLE}: EXP/min, farm rate, potions + cost, mesos, per-map grind log")
    ap.add_argument("--console", action="store_true", help="run in the terminal instead of the overlay")
    ap.add_argument("--version", action="version", version=f"{APP_TITLE} {__version__}")
    ap.add_argument("--selftest", action="store_true", help="load the OCR engine, parse a synthetic status bar, exit")
    ap.add_argument("--settings", action="store_true", help="open the settings window on start")
    ap.add_argument("--debug", action="store_true", help="engine log on stderr, debug crops in maple_exp_debug/")
    ap.add_argument("--interval", type=float, help="seconds between EXP reads (overrides settings)")
    ap.add_argument("--monitor", type=int, help="mss monitor index (overrides settings)")
    ap.add_argument("--pots", help='pot slots, e.g. "Del=Meat:80,End=Lemon:150" (saved to settings)')
    args = ap.parse_args()
    if not args.console and "console" in Path(sys.argv[0]).stem.lower():
        args.console = True          # MapleFarmTracker-console.exe
    if args.selftest:
        sys.exit(selftest())
    settings = load_settings()
    if args.interval:
        settings["interval"] = args.interval
    if args.monitor:
        settings["monitor"] = args.monitor
    if args.pots:
        settings["slots"] = parse_pots(args.pots)
        save_settings(settings)
    if not SETTINGS_PATH.exists():
        save_settings(settings)
    debug_dir = None
    if args.debug:
        debug_dir = HERE / "maple_exp_debug"
        debug_dir.mkdir(exist_ok=True)
    if args.console:
        run_console(settings, debug_dir)
    else:
        run_ui(settings, debug_dir, open_settings=args.settings)


if __name__ == "__main__":
    main()
