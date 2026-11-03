"""
maple_slots.py - quickslot item counts, minimap map name, and the per-map grind log
for MapleStory Classic World. Pure screen-pixel reading (OCR); no game access.

Quickslot: the 2x4 key grid (Shift Ins Home PgUp / Ctrl Del End PgDn). Each
cell shows the key label top-left and, for stackable items, the count
bottom-left in white digits with a dark outline.
"""

from __future__ import annotations

import csv
import difflib
import re
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from maple_exp import Ocr, farm_rate

try:
    import cv2
except ImportError:  # opencv comes with rapidocr_onnxruntime; guard anyway
    cv2 = None

KEYS = [["Shift", "Ins", "Home", "PgUp"], ["Ctrl", "Del", "End", "PgDn"]]
KEY_POS = {k: (c, r) for r, row in enumerate(KEYS) for c, k in enumerate(row)}
# OCR spellings of the labels as the client draws them (and common misreads)
LABEL_ALIASES = {
    "shift": "Shift", "shif": "Shift", "shlft": "Shift",
    "ins": "Ins", "lns": "Ins",
    "hm": "Home", "home": "Home", "hrn": "Home",
    "pup": "PgUp", "pgup": "PgUp", "pgu": "PgUp",
    "ctrl": "Ctrl", "ctr": "Ctrl", "attack": "Ctrl",
    "del": "Del", "dei": "Del",
    "end": "End", "enrd": "End", "emd": "End",
    "pdn": "PgDn", "pgdn": "PgDn", "pan": "PgDn", "pdm": "PgDn",
}


def match_label(text: str):
    t = re.sub(r"[^a-z]", "", text.lower())
    if not t or len(t) > 7:
        return None
    if t in LABEL_ALIASES:
        return LABEL_ALIASES[t]
    best, score = None, 0.0
    for alias, key in LABEL_ALIASES.items():
        if len(alias) < 3:
            continue
        r = difflib.SequenceMatcher(None, t, alias).ratio()
        if r > score:
            best, score = key, r
    return best if score >= 0.8 else None


# --------------------------------------------------------------------------
# Quickslot grid detection
# --------------------------------------------------------------------------
def _light_cells(img: Image.Image):
    """Bounding boxes of light square blobs (cell backgrounds) in img pixels."""
    if cv2 is None:
        return []
    arr = np.array(img.convert("RGB"))
    mn = arr.min(axis=2)
    m = (mn >= 195).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if 22 <= w <= 150 and 22 <= h <= 150 and 0.7 <= w / h <= 1.4 and area >= 0.22 * w * h:
            out.append((int(x), int(y), int(w), int(h)))
    return out


def _lattice_pitch(cells):
    """Most common horizontal/vertical spacing between similar-size blobs."""
    ds = []
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            sa, sb = max(a[2], a[3]), max(b[2], b[3])
            if abs(sa - sb) > 0.35 * max(sa, sb):
                continue
            dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
            s = max(sa, sb)
            if dy < 0.3 * s and 0.9 * s <= dx <= 1.6 * s:
                ds.append(dx)
            if dx < 0.3 * s and 0.9 * s <= dy <= 1.6 * s:
                ds.append(dy)
    if not ds:
        return None
    ds.sort()
    return float(np.median(ds))


def find_quickslot(ocr: Ocr, img: Image.Image, origin=(0, 0), debug=None):
    """
    Locate the 2x4 quickslot grid. img is a screen region (the bottom half
    of the monitor works). Returns dict(x0, y0, pitch, size, keys_seen) in
    screen coordinates, or None.
    """
    log = debug if debug is not None else []
    anchors = []   # (key, x, y) of label boxes
    for scale, mode in ((2, "min"), (2, "gray"), (3, "gray")):
        for b in ocr.read_boxes(img, scale=scale, invert=False, mode=mode):
            key = match_label(b[4])
            h = b[3] - b[1]
            if key and 8 <= h <= 60 and (b[2] - b[0]) <= 8 * h:
                anchors.append((key, b[0], b[1], h))
    blobs = _light_cells(img)
    pitch = _lattice_pitch(blobs)
    log.append(f"anchors={[(a[0], round(a[1]), round(a[2])) for a in anchors]} blobs={len(blobs)} pitch_blob={pitch}")
    # pitch from anchors: only pairs close enough to belong to one 2x4 panel
    # (text elsewhere on screen - a console, chat - also OCRs as "Ctrl"/"End")
    pd = []
    for i, a in enumerate(anchors):
        for b in anchors[i + 1:]:
            ca, ra = KEY_POS[a[0]]
            cb, rb = KEY_POS[b[0]]
            if a[0] == b[0]:
                continue
            hmax = max(a[3], b[3])
            if abs(a[1] - b[1]) > 16 * hmax or abs(a[2] - b[2]) > 8 * hmax:
                continue
            if ra == rb and ca != cb:
                pd.append(abs(a[1] - b[1]) / abs(ca - cb))
            if ca == cb and ra != rb:
                pd.append(abs(a[2] - b[2]) / abs(ra - rb))
    if pitch is None and pd:
        pitch = float(np.median(pd))
    if pitch is None:
        # last resort: label height -> pitch (label ~ 0.3 of a cell)
        if not anchors:
            return None
        pitch = float(np.median([a[3] for a in anchors])) * 3.3
    if not (25 <= pitch <= 200):
        return None
    size = pitch * 0.94
    # origin hypotheses from anchors: label sits ~0.07*pitch right / ~0.05*pitch below the cell corner
    origins = []
    for key, x, y, h in anchors:
        c, r = KEY_POS[key]
        origins.append((x - 0.07 * pitch - c * pitch, y - 0.05 * pitch - r * pitch))
    # snap with blob lattice: blob top-left = cell corner + border (~0.05*pitch)
    if blobs and origins:
        ox, oy = float(np.median([o[0] for o in origins])), float(np.median([o[1] for o in origins]))
        snapped = []
        for bx, by, bw, bh in blobs:
            cx = round((bx - ox) / pitch)
            cy = round((by - oy) / pitch)
            if 0 <= cx <= 3 and 0 <= cy <= 1:
                ex, ey = ox + cx * pitch, oy + cy * pitch
                if abs(bx - 0.05 * pitch - ex) < 0.25 * pitch and abs(by - 0.05 * pitch - ey) < 0.25 * pitch:
                    snapped.append((bx - 0.05 * pitch - cx * pitch, by - 0.05 * pitch - cy * pitch))
        if snapped:
            origins = origins + snapped * 2  # blobs are geometrically precise: weight them
    if not origins:
        return None
    # consensus: largest cluster of origins within 0.25*pitch
    best = None
    for o in origins:
        members = [p for p in origins if abs(p[0] - o[0]) < 0.25 * pitch and abs(p[1] - o[1]) < 0.25 * pitch]
        if best is None or len(members) > len(best):
            best = members
    x0 = float(np.median([m[0] for m in best]))
    y0 = float(np.median([m[1] for m in best]))
    grid = {"x0": origin[0] + x0, "y0": origin[1] + y0, "pitch": pitch, "size": size}
    # verify by reading labels cell by cell
    seen = verify_quickslot(ocr, img, {"x0": x0, "y0": y0, "pitch": pitch, "size": size})
    grid["keys_seen"] = seen
    log.append(f"grid={ {k: round(v, 1) for k, v in grid.items() if k != 'keys_seen'} } seen={seen}")
    if len(seen) < 3:
        return None
    return grid


def cell_rect(grid, key):
    c, r = KEY_POS[key]
    s = int(round(grid["size"]))
    return (int(round(grid["x0"] + c * grid["pitch"])), int(round(grid["y0"] + r * grid["pitch"])), s, s)


def verify_quickslot(ocr: Ocr, img: Image.Image, grid) -> list:
    seen = []
    for key in KEY_POS:
        x, y, s, _ = cell_rect(grid, key)
        if x < 0 or y < 0 or x + s > img.width or y + s > img.height:
            continue
        cell = img.crop((x, y, x + s, y + s))
        for b in ocr.read_boxes(cell, scale=4, invert=False, mode="gray"):
            if b[1] < 0.5 * s and match_label(b[4]) == key:
                seen.append(key)
                break
    return seen


# --------------------------------------------------------------------------
# Count reading
# --------------------------------------------------------------------------
def read_slot_count(ocr: Ocr, cell: Image.Image, prev: int | None = None):
    """
    Item count drawn bottom-left of a quickslot cell, or None.
    prev = last accepted count for this cell: used to drop junk digits that
    the icon adds after the number ("1792" when the count is 179) and to pick
    the closest candidate when the OCR variants disagree.
    """
    s = cell.height
    d = cell.crop((0, int(s * 0.5), int(cell.width * 0.74), s))
    cands = []
    eng = ocr.engine
    old_score = eng.text_score
    try:
        eng.text_score = 0.3
        for scale, mode in ((6, "gray"), (8, "gray"), (6, "min"), (5, "gray")):
            text = ocr.read_line(d, scale=scale, invert=False, mode=mode)
            m = re.match(r"\s*(\d{1,4})\b", text)
            if m:
                cands.append(int(m.group(1)))
        for b in ocr.read_boxes(cell, scale=4, invert=False, mode="gray"):
            if b[1] >= 0.4 * s and re.fullmatch(r"\d{1,4}", b[4].strip()):
                cands.append(int(b[4].strip()))
    finally:
        eng.text_score = old_score
    if not cands:
        return None
    # a candidate that is another candidate plus a trailing digit is icon junk
    cands = [c for c in cands if not (len(str(c)) > 1 and int(str(c)[:-1]) in cands)]
    if prev is not None:
        fixed = []
        lp = len(str(prev))
        for c in cands:
            sc = str(c)
            if len(sc) > lp and abs(int(sc[:lp]) - prev) <= 25:
                c = int(sc[:lp])          # trailing junk digit(s) from the icon
            fixed.append(c)
        cands = fixed
        cnt = Counter(cands)
        return min(cands, key=lambda c: (abs(c - prev), -cnt[c], len(str(c))))
    cnt = Counter(cands)
    return min(cands, key=lambda c: (-cnt[c], len(str(c))))


class SlotTracker:
    """Validated per-key counts with usage rate (per hour) and time-to-empty."""

    def __init__(self, keys, window_sec=1800):
        self.keys = list(keys)
        self.window = window_sec
        self.count = {}         # key -> accepted count
        self.pending = {}       # key -> (value, times seen)
        self.hist = {k: deque() for k in self.keys}   # (t, count)
        self.last_t = {}

    def update(self, key, value, t):
        if value is None:
            return
        last = self.count.get(key)
        ok = False
        if last is None or abs(value - last) <= 25:
            if last is None:
                p = self.pending.get(key)
                if p and p[0] == value:
                    ok = True
                else:
                    self.pending[key] = (value, 1)
            else:
                ok = True
        else:
            p = self.pending.get(key)
            if p and abs(p[0] - value) <= 2:
                ok = True
            else:
                self.pending[key] = (value, 1)
        if ok:
            self.pending.pop(key, None)
            self.count[key] = value
            self.last_t[key] = t
            h = self.hist[key]
            h.append((t, value))
            while h and h[0][0] < t - self.window:
                h.popleft()

    def rate_per_hour(self, key):
        h = self.hist.get(key)
        if not h or len(h) < 2:
            return None
        (t0, c0), (t1, c1) = h[0], h[-1]
        if t1 - t0 < 120:
            return None
        used = c0 - c1   # positive when consumed; buying makes it negative
        return used / (t1 - t0) * 3600.0

    def minutes_left(self, key):
        r = self.rate_per_hour(key)
        c = self.count.get(key)
        if not r or r <= 0 or c is None:
            return None
        return c / r * 60.0


# --------------------------------------------------------------------------
# Map name (minimap title, top-left)
# --------------------------------------------------------------------------
_MAP_JUNK = re.compile(r"mini\s*map|world|^\W+$", re.I)


def read_map_name(ocr: Ocr, img: Image.Image):
    """
    img = the top-left part of the screen (the minimap panel). The title is
    one to three left-aligned lines: street/region (e.g. 'Victoria Road') and
    the map name, which can wrap. Returns (name, lines) where name joins the
    lines as 'first: rest' (e.g. 'Kerning City Subway: Transfer Area'), or
    (None, None).
    """
    boxes = ocr.read_boxes(img, scale=1, invert=False, mode="gray")
    boxes = [b for b in boxes if not _MAP_JUNK.search(b[4]) and len(b[4].strip()) >= 3 and (b[3] - b[1]) >= 9]
    boxes.sort(key=lambda b: b[1])
    for i, a in enumerate(boxes):
        ha = a[3] - a[1]
        chain = [a]
        last = a
        for b in boxes[i + 1:]:
            if abs(b[0] - a[0]) <= 0.8 * ha and 0 <= b[1] - last[3] <= 1.2 * ha:
                chain.append(b)
                last = b
                if len(chain) == 3:
                    break
        if len(chain) >= 2:
            lines = [re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"\s+", " ", c[4])).strip(" .:") for c in chain]
            return (lines[0] + ": " + " ".join(lines[1:]), lines)
    return (None, None)


_MAPS = None


def known_maps():
    """[(street, map, 'street: map')] from maple_maps.txt next to this file (empty if missing)."""
    global _MAPS
    if _MAPS is None:
        _MAPS = []
        from app_paths import resource
        f = resource("maple_maps.txt")
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if "|" in line and not line.startswith("#"):
                    street, name = [x.strip() for x in line.split("|", 1)]
                    _MAPS.append((street, name, f"{street}: {name}"))
    return _MAPS


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def snap_map(lines):
    """
    Snap OCR'd minimap title lines [street, map...] to the known map list.
    Returns the canonical 'Street: Map' or None if nothing is close enough.
    """
    maps = known_maps()
    if not maps or not lines:
        return None
    street = lines[0]
    name = " ".join(lines[1:]) if len(lines) > 1 else ""
    best, score = None, 0.0
    for st, mp, full in maps:
        if name:
            sc = 0.35 * _ratio(street, st) + 0.65 * _ratio(name, mp)
        else:
            sc = max(_ratio(street, mp), _ratio(street, full))
        if sc > score:
            best, score = full, sc
    return best if score >= 0.82 else None


class MapWatcher:
    """Stable current map: a new name must be read twice in a row to switch."""

    def __init__(self):
        self.current = None
        self.region = None
        self.candidate = None
        self.names = []     # canonical names seen

    def canonical(self, name, lines=None):
        snapped = snap_map(lines) if lines else None
        if snapped:
            return snapped
        if known_maps():
            return None          # with the map list loaded, anything that doesn't snap is not a map (chat, a window)
        for n in self.names:
            if _ratio(n, name) >= 0.86:
                return n
        self.names.append(name)
        return name

    def update(self, name, lines=None):
        """Returns True if the current map changed."""
        if not name:
            return False
        name = self.canonical(name, lines)
        if name is None:
            return False
        if name == self.current:
            self.candidate = None
            return False
        if self.candidate == name:
            self.current, self.candidate = name, None
            return True
        self.candidate = name
        return False


# --------------------------------------------------------------------------
# Grind log
# --------------------------------------------------------------------------
class GrindLog:
    """
    Splits accepted EXP samples into grinding segments per map and logs the
    ones that are real: >= min_minutes long at >= min_rate EXP/min. A segment
    ends on a map change or after idle_sec without any EXP gain (the idle tail
    is not counted).
    """
    FIELDS = ["start", "end", "map", "minutes", "exp", "exp_per_min", "exp_per_hour", "farm_exp_per_hour",
              "level_start", "level_end", "pots_cost", "mesos", "net_per_hour"]

    def __init__(self, path: Path, min_minutes=1.0, min_rate=50.0, idle_sec=120, extras=None):
        self.path = Path(path)
        self.min_minutes = min_minutes
        self.min_rate = min_rate
        self.idle_sec = idle_sec
        self.extras = extras      # callable(seg) -> {"pots_cost": mesos or None, "mesos": delta or None}
        self.rows = []
        self.seg = None
        self.prev = None     # (t, gained_total, level)
        self.map = None
        self.session_rows = 0
        self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path, newline="", encoding="utf-8") as f:
                self.rows = [r for r in csv.DictReader(f)]

    def set_map(self, name, t):
        if name != self.map:
            self._close(t)
            self.map = name

    def on_sample(self, t, level, gained_total):
        if self.prev is not None:
            gain = gained_total - self.prev[1]
            if gain > 0:
                if self.seg is None:
                    self.seg = {"start": self.prev[0], "g0": self.prev[1], "lv0": self.prev[2],
                                "map": self.map or "unknown", "last_gain": t, "g1": gained_total, "lv1": level,
                                "pts": [self.prev[:2]]}
                else:
                    self.seg.update(last_gain=t, g1=gained_total, lv1=level)
            if self.seg is not None:
                self.seg["pts"].append((t, gained_total))
        self.prev = (t, gained_total, level)
        self.on_tick(t)

    def on_tick(self, t):
        if self.seg and t - self.seg["last_gain"] > self.idle_sec:
            self._close(t)

    def current(self, t):
        """Live view of the open segment: (map, minutes, exp, exp_per_min) or None."""
        if not self.seg:
            return None
        dur = max(1.0, self.seg["last_gain"] - self.seg["start"])
        exp = self.seg["g1"] - self.seg["g0"]
        return (self.seg["map"], dur / 60.0, exp, exp / dur * 60.0)

    def _close(self, t):
        seg, self.seg = self.seg, None
        if not seg:
            return
        dur = seg["last_gain"] - seg["start"]
        exp = seg["g1"] - seg["g0"]
        if dur < self.min_minutes * 60 or exp <= 0:
            return
        rate = exp / dur * 60.0
        if rate < self.min_rate:
            return
        farm = farm_rate(seg.get("pts", []), self.min_rate, last_n=10 ** 6, trim=1 / 6)
        ex = {}
        if self.extras:
            try:
                ex = self.extras(seg) or {}
            except Exception:
                ex = {}
        cost = ex.get("pots_cost")
        mesos = ex.get("mesos")
        net = None
        if mesos is not None:
            net = int(round((mesos - (cost or 0)) / dur * 3600))
        row = {
            "start": datetime.fromtimestamp(seg["start"]).isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(seg["last_gain"]).isoformat(timespec="seconds"),
            "map": seg["map"], "minutes": round(dur / 60.0, 1), "exp": int(exp),
            "exp_per_min": round(rate, 1), "exp_per_hour": int(round(rate * 60)),
            "farm_exp_per_hour": int(round(farm[0] * 60)) if farm and farm[0] is not None else int(round(rate * 60)),
            "level_start": seg["lv0"], "level_end": seg["lv1"],
            "pots_cost": int(round(cost)) if cost is not None else "",
            "mesos": int(round(mesos)) if mesos is not None else "",
            "net_per_hour": net if net is not None else "",
        }
        self.rows.append(row)
        self.session_rows += 1
        new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)

    def flush(self, t):
        self._close(t)

    @staticmethod
    def _num(v):
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    def per_map(self, rows=None):
        agg = {}
        for r in (rows if rows is not None else self.rows):
            key = r["map"]
            for existing in agg:            # group OCR variants of one map name
                if _ratio(existing, key) >= 0.86:
                    key = existing
                    break
            a = agg.setdefault(key, {"map": key, "sessions": 0, "minutes": 0.0, "exp": 0, "last": None,
                                     "farm_w": 0.0, "pots_cost": 0.0, "cost_minutes": 0.0, "mesos": 0.0, "mesos_minutes": 0.0})
            a["sessions"] += 1
            a["minutes"] += float(r["minutes"])
            a["exp"] += int(float(r["exp"]))
            a["last"] = int(float(r["exp_per_hour"]))
            f = self._num(r.get("farm_exp_per_hour"))
            a["farm_w"] += (f if f is not None else float(r["exp_per_hour"])) * float(r["minutes"])
            c = self._num(r.get("pots_cost"))
            if c is not None:
                a["pots_cost"] += c
                a["cost_minutes"] += float(r["minutes"])
            m = self._num(r.get("mesos"))
            if m is not None:
                a["mesos"] += m
                a["mesos_minutes"] += float(r["minutes"])
        out = []
        for a in agg.values():
            a["exp_per_hour"] = int(a["exp"] / a["minutes"] * 60) if a["minutes"] else 0
            a["farm_exp_per_hour"] = int(a["farm_w"] / a["minutes"]) if a["minutes"] else 0
            a["pots_per_hour"] = int(a["pots_cost"] / a["cost_minutes"] * 60) if a["cost_minutes"] else None
            a["mesos_per_hour"] = int(a["mesos"] / a["mesos_minutes"] * 60) if a["mesos_minutes"] else None
            a["net_per_hour"] = (a["mesos_per_hour"] - (a["pots_per_hour"] or 0)) if a["mesos_per_hour"] is not None else None
            out.append(a)
        out.sort(key=lambda a: -a["exp_per_hour"])
        return out

    def session_summary(self):
        rows = self.rows[-self.session_rows:] if self.session_rows else []
        return self.per_map(rows)

    def recent(self, n=30):
        return list(reversed(self.rows[-n:]))


# --------------------------------------------------------------------------
# Mesos (read from a user-set screen region, e.g. the inventory window)
# --------------------------------------------------------------------------
_MESO_RE = re.compile(r"(\d{1,3}(?:[,.]\d{3})+|\d{1,10})")
_MESO_LABEL = re.compile(r"^\W*m\s*e\s*s\s*[o0]\s*s\W*$", re.I)


def find_mesos(ocr: Ocr, img: Image.Image, origin=(0, 0), exclude=None):
    """
    Find the meso balance on screen: a number immediately left of a 'MESOS'
    label on the same row (the inventory window's bottom bar). Returns
    ((left, top, width, height) screen rect for the number, value) or (None, None).
    exclude = (L, T, W, H) screen rect to ignore (the HUD's own window).
    """
    boxes = ocr.read_boxes(img, scale=1, invert=False, mode="gray")

    def excluded(b):
        if not exclude:
            return False
        L, T, W, H = exclude
        cx, cy = origin[0] + (b[0] + b[2]) / 2, origin[1] + (b[1] + b[3]) / 2
        return L <= cx <= L + W and T <= cy <= T + H

    labels = [b for b in boxes if _MESO_LABEL.match(b[4]) and not excluded(b)]
    for lab in labels:
        lh = lab[3] - lab[1]
        best = None
        for b in boxes:
            if b is lab or excluded(b):
                continue
            same_row = (min(lab[3], b[3]) - max(lab[1], b[1])) > 0.5 * lh
            if not same_row or b[2] > lab[0] + 0.5 * lh or lab[0] - b[2] > 4 * lh:
                continue
            m = _MESO_RE.search(b[4])
            if not m:
                continue
            if best is None or b[2] > best[2]:
                best = b
        if best is None:
            continue
        m = _MESO_RE.search(best[4])
        val = int(re.sub(r"\D", "", m.group(1)))
        w = best[2] - best[0]
        h = best[3] - best[1]
        per_char = w / max(1, len(m.group(1)))
        left = origin[0] + best[0] - 5 * per_char            # balance can grow to the left
        top = origin[1] + best[1] - 0.3 * h
        width = (origin[0] + lab[0] - 0.2 * lh) - left        # up to the label
        return (int(left), int(top), int(width), int(h * 1.6)), val
    return None, None


def read_mesos(ocr: Ocr, img: Image.Image):
    """Largest comma-grouped number in the region, or None."""
    best = None
    for scale, mode in ((4, "gray"), (6, "gray"), (4, "min")):
        text = ocr.read_line(img, scale=scale, invert=False, mode=mode)
        for m in _MESO_RE.finditer(text):
            raw = m.group(1)
            if "," in raw or "." in raw:
                v = int(re.sub(r"\D", "", raw))
            else:
                v = int(raw)
            if best is None or v > best:
                best = v
        if best is not None:
            break
    return best


class MesoTracker:
    """Validated meso balance with a robust mesos/hour slope."""

    def __init__(self, window_sec=1800):
        self.window = window_sec
        self.value = None
        self.pending = None
        self.hist = deque()   # (t, mesos)

    def update(self, v, t):
        if v is None or v < 0 or v > 2_147_483_647:
            return False
        ok = False
        if self.value is None:
            if self.pending == v:
                ok = True
            else:
                self.pending = v
        elif abs(v - self.value) <= max(0.2 * self.value, 5000):
            ok = True
        elif self.pending == v:
            ok = True            # big jump (sale / purchase) confirmed twice
        else:
            self.pending = v
        if ok:
            self.pending = None
            self.value = v
            self.hist.append((t, v))
            while self.hist and self.hist[0][0] < t - self.window:
                self.hist.popleft()
        return ok
