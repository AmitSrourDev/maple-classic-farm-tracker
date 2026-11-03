#!/usr/bin/env python3
"""
maple_exp.py - MapleStory Classic World EXP/min tracker (screenshot + OCR).

Screenshots the MapleStory window every N seconds, OCRs the EXP readout in
the bottom status bar (the "21066[52.00%]" text), and prints:
  - average EXP/min since start
  - rolling EXP/min over the last few minutes
  - EXP and minutes to next level (at the rolling rate)
Every sample is appended to a CSV; a JSON summary is written on Ctrl+C.

Usage
  python maple_exp.py                   track (scans the primary monitor)
  python maple_exp.py --interval 5      sample every 5 s (default 10)
  python maple_exp.py --once            one read, print it, exit
  python maple_exp.py --image shot.png  parse a saved screenshot, exit
  python maple_exp.py --debug           save crops to ./maple_exp_debug/
  python maple_exp.py --region L,T,W,H  fixed screen rect of the EXP text
                                        (skips auto-locate)
  python maple_exp.py --level 24        your level, if the bar's level digits
                                        don't OCR (speeds up the initial lock)
  python maple_exp.py --plain           one line per sample (for logging)

Install
  pip install rapidocr_onnxruntime==1.2.3 onnxruntime mss pillow numpy

Notes
  - Plain desktop screen capture of the whole monitor (no window hooks, no
    process access). Run the game windowed / borderless: exclusive
    fullscreen usually captures as a black frame.
  - Windows: the script makes itself DPI-aware so screen coordinates match
    the screenshot pixels at 125%/150% scaling.
  - macOS: give your terminal Screen Recording permission.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# --------------------------------------------------------------------------
# EXP to next level, MapleStory Classic World (meowdb EXP table, 2026-10-30).
# Lv99 is the last level with a requirement.
# --------------------------------------------------------------------------
EXP_TABLE = {
    1: 15, 2: 34, 3: 57, 4: 92, 5: 135, 6: 372, 7: 560, 8: 840, 9: 1242,
    10: 1716, 11: 2360, 12: 3216, 13: 4200, 14: 5460, 15: 7050, 16: 8840,
    17: 11040, 18: 13716, 19: 16680, 20: 20216, 21: 24402, 22: 28980,
    23: 34320, 24: 40512, 25: 47216, 26: 54900, 27: 63666, 28: 73080,
    29: 83720, 30: 95700, 31: 108480, 32: 122760, 33: 138666, 34: 155540,
    35: 174216, 36: 194832, 37: 216600, 38: 240500, 39: 266682, 40: 294216,
    41: 324240, 42: 356916, 43: 391160, 44: 428280, 45: 468450, 46: 510420,
    47: 555680, 48: 604416, 49: 655200, 50: 709716, 51: 748608, 52: 789631,
    53: 832902, 54: 878545, 55: 926689, 56: 977471, 57: 1031036,
    58: 1087536, 59: 1147132, 60: 1209994, 61: 1276301, 62: 1346242,
    63: 1420016, 64: 1497832, 65: 1579913, 66: 1666492, 67: 1757815,
    68: 1854143, 69: 1955750, 70: 2062925, 71: 2175973, 72: 2295216,
    73: 2420993, 74: 2553663, 75: 2693603, 76: 2841212, 77: 2996910,
    78: 3161140, 79: 3334370, 80: 3517093, 81: 3709829, 82: 3913127,
    83: 4127566, 84: 4353756, 85: 4592341, 86: 4844001, 87: 5109452,
    88: 5389449, 89: 5684790, 90: 11938059, 91: 25069923, 92: 52646838,
    93: 110558359, 94: 232172553, 95: 487562361, 96: 1023880958,
    97: 2150150011, 98: 4515315023, 99: 9482161548,
}

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
_OPEN = r"[\[\(\{lI|]"
_CLOSE = r"[\]\)\}lI|]"
_NUM = r"(\d[\d,]*)"
_PCT = r"(\d{1,3}(?:[.,\s]\d{1,2})?|\d{3,5})"
# strict: number, bracket, pct, %, optional closing bracket
EXP_RE = re.compile(_NUM + r"\s*" + _OPEN + r"\s*" + _PCT + r"\s*%\s*" + _CLOSE + "?")
# loose: % missing but closing bracket present and pct has a decimal part
EXP_RE_NOPCT = re.compile(_NUM + r"\s*" + _OPEN + r"\s*(\d{1,3}[.,\s]\d{1,2})\s*" + _CLOSE)
LEVEL_RE = re.compile(r"L[vV]?[\.\s]*(\d{1,3})")

# common OCR confusions inside digit runs
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"})


def _normalize(text: str) -> str:
    # fix confusable letters that sit in digit context ("21O66" -> "21066",
    # "52.OO" -> "52.00"); a few passes so runs like "OO" resolve
    out = list(text)
    for _ in range(4):
        changed = False
        for i, ch in enumerate(out):
            if ch in "OoSsBZz":
                left = out[i - 1] if i > 0 else ""
                right = out[i + 1] if i + 1 < len(out) else ""
                if (left.isdigit() or left in ".,") or (right.isdigit() or right in ".,"):
                    out[i] = ch.translate(_DIGIT_FIX)
                    changed = True
        if not changed:
            break
    return "".join(out)


def parse_readout(text: str):
    """Return list of candidate (exp, pct) tuples found in OCR text, best first."""
    text = _normalize(text)
    cands = []
    for rx in (EXP_RE, EXP_RE_NOPCT):
        for m in rx.finditer(text):
            try:
                exp = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            raw = re.sub(r"[,\s]", ".", m.group(2).strip())
            if "." in raw:
                pct = float(raw)
                pcts = [pct]
            else:
                # decimal point lost: "5200" -> 52.00, but "52" may be 52.00 too
                v = int(raw)
                pcts = [v / 100.0] if v > 100 else [float(v), v / 100.0]
            for pct in pcts:
                if 0 <= pct < 100:
                    cands.append((exp, pct))
    # de-dup, keep order
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def level_hint(text: str):
    m = LEVEL_RE.search(text)
    if m:
        lv = int(m.group(1))
        if 1 <= lv <= 99:
            return lv
    return None


def _tol(need: int) -> float:
    # pct is shown to 0.01% -> exp known to need*0.0001; add OCR slack
    return need * 0.00015 + 2


def consistent(level: int, exp: int, pct: float) -> bool:
    need = EXP_TABLE.get(level)
    if need is None or exp > need:
        return False
    return abs(exp - need * pct / 100.0) <= _tol(need)


def infer_level(exp: int, pct: float, prev_level: int | None, hint: int | None):
    """
    Level whose EXP table entry makes exp and pct agree. None if ambiguous.
    With a known previous level only prev and prev+1 are considered (a read
    that only fits some other level is a misread, not a level change).
    """
    if prev_level is not None:
        pool = [lv for lv in (prev_level, prev_level + 1) if lv in EXP_TABLE]
    else:
        pool = list(EXP_TABLE)
    matches = [lv for lv in pool if consistent(lv, exp, pct)]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    for pref in (prev_level, (prev_level + 1) if prev_level else None, hint):
        if pref in matches:
            return pref
    return None


# alternatives for glyphs the recognizer confuses with digits
_AMBIG = {"O": "0", "o": "0", "Q": "0", "D": "0", "S": "5", "s": "5", "B": "83", "Z": "2", "z": "2",
          "I": "1", "l": "1", "|": "1", "G": "6", "b": "6", "q": "9", "g": "9", "A": "4", "T": "7"}


def _digit_strings(text: str, cap: int = 16):
    """
    Digit-string variants of the EXP readout. Text after the last 'P' of the
    EXP label (if any) up to the last '%'. Each confusable glyph expands to
    its digit alternatives (or nothing). Returns (variants, pct_seen).
    """
    t = text
    i = max(t.rfind("P"), t.rfind("p"))
    if i >= 0 and re.search(r"\d", t[i:]):
        t = t[i + 1:]
    j = t.rfind("%")
    pct_seen = j >= 0
    if pct_seen:
        t = t[:j]
    variants = [""]
    for ch in t:
        if ch.isdigit():
            variants = [v + ch for v in variants]
        elif ch in _AMBIG:
            alts = [""] + list(_AMBIG[ch])
            variants = [v + a for v in variants for a in alts][:cap]
    out, seen = [], set()
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out, pct_seen


def brute_candidates(text: str, levels):
    """
    Separator-free recovery: the OCR'd digit string must be
    EXP + PCT(int part + 2 decimals) [+ junk from a misread '%' / ']'] with a
    possible '1' from a '[' before or between. Every split is checked against
    the EXP table for the allowed levels, so only self-consistent pairs return.
    """
    variants, pct_seen = _digit_strings(_normalize(text))
    tails = (0,) if pct_seen else (0, 1, 2)   # junk can only trail when '%' was lost
    out = []
    for digits in variants:
        for drop_tail in tails:
            dg = digits[: len(digits) - drop_tail] if drop_tail else digits
            for k in (3, 4):
                if len(dg) <= k:
                    continue
                pct = int(dg[-k:]) / 100.0
                if pct >= 100:
                    continue
                rest = dg[:-k]
                rests = {rest}
                if rest.endswith("1") and len(rest) > 1:
                    rests.add(rest[:-1])
                for r in list(rests):
                    if r.startswith("1") and len(r) > 1:
                        rests.add(r[1:])
                for r in rests:
                    exp = int(r)
                    for lv in levels:
                        if consistent(lv, exp, pct):
                            out.append((lv, exp, pct))
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def resolve(text: str, prev_level: int | None, hint: int | None, expected_exp: float | None = None,
            expected_tol: float | None = None):
    """
    Turn OCR text into (level, exp, pct, how) or None.
    Strict parse first (separators present). If that fails, brute-force the
    digits against the EXP table for {level, level+1}, where level is the
    locked previous level or, before lock, the level read off the bar / given
    with --level. Brute-force hits must sit near the expected EXP when one is
    known; before lock the Session's two-sample agreement rule guards them.
    """
    strict = []
    for exp, pct in parse_readout(text):
        lv = infer_level(exp, pct, prev_level, hint or level_hint(text))
        if lv is not None:
            strict.append((lv, exp, pct))
    cands, how = strict, "strict"
    if not cands:
        base = prev_level if prev_level is not None else hint
        if base is None:
            return None
        levels = [lv for lv in (base, base + 1) if lv in EXP_TABLE]
        cands, how = brute_candidates(text, levels), "digits"
        if expected_exp is not None and prev_level is not None:
            need = EXP_TABLE[prev_level]
            lim = expected_tol if expected_tol is not None else max(0.01 * need, 300)
            cands = [c for c in cands if c[0] == prev_level and abs(c[1] - expected_exp) <= lim
                     or c[0] == prev_level + 1 and expected_exp >= 0.9 * need and c[1] <= lim]
    if not cands:
        return None
    if len(cands) > 1 and expected_exp is not None:
        cands.sort(key=lambda c: abs(c[1] - expected_exp))
    lv, exp, pct = cands[0]
    return lv, exp, pct, how


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
class Ocr:
    def __init__(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            sys.exit("rapidocr_onnxruntime missing: pip install rapidocr_onnxruntime==1.2.3 onnxruntime")
        self.engine = RapidOCR()
        self.engine.use_angle_cls = False  # never rotate; the bar is upright
        # The detector upsamples until the SHORT side is 736 px, which turns a
        # thin status-bar strip into a 7x blow-up and loses small text. Cap the
        # long side instead; we already upscale the strip ourselves.
        try:
            for op in self.engine.text_detector.preprocess_op:
                if hasattr(op, "limit_side_len"):
                    op.limit_type = "max"
                    op.limit_side_len = 4096
        except Exception:
            pass

    @staticmethod
    def _prep(img: Image.Image, scale: int, invert: bool, pad: int = 12, mode: str = "min") -> np.ndarray:
        """
        mode "min": grayscale = min(R, G, B). White text stays bright while the
        saturated HP/MP/EXP bar fills (red / blue / yellow) drop to background,
        so the fill edge can't merge with a digit. mode "gray": luminance.
        """
        if mode == "min":
            g = Image.fromarray(np.array(img.convert("RGB")).min(axis=2).astype(np.uint8), "L")
        else:
            g = ImageOps.grayscale(img)
        if invert:
            g = ImageOps.invert(g)
        g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
        g = ImageOps.expand(g, border=pad, fill=255 if invert else 0)
        return np.array(g.convert("RGB"))

    def read_line(self, img: Image.Image, scale: int = 6, invert: bool = False, mode: str = "min") -> str:
        """Recognize a single text line (no detection). Returns the text."""
        self.engine.use_text_det = False
        res, _ = self.engine(self._prep(img, scale, invert, mode=mode))
        self.engine.use_text_det = True
        if not res:
            return ""
        return " ".join(r[1] for r in res)

    def read_boxes(self, img: Image.Image, scale: int = 1, invert: bool = False, mode: str = "min"):
        """Detect + recognize. Returns [(x0, y0, x1, y1, text, score)] in img pixels."""
        self.engine.use_text_det = True
        old_ratio = self.engine.width_height_ratio
        self.engine.width_height_ratio = -1  # force detection even on wide strips
        pad = 12
        try:
            res, _ = self.engine(self._prep(img, scale, invert, pad, mode))
        finally:
            self.engine.width_height_ratio = old_ratio
        out = []
        for box, text, score in (res or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append((
                (min(xs) - pad) / scale, (min(ys) - pad) / scale,
                (max(xs) - pad) / scale, (max(ys) - pad) / scale,
                text, float(score),
            ))
        return out


def tight_rows(img: Image.Image, margin: int = 2, thr: int = 110) -> Image.Image:
    """Trim the crop to the rows that contain bright (text) pixels, so the text
    fills the recognizer's input height and 1-px glyphs like '[' survive."""
    a = np.array(img.convert("RGB")).min(axis=2)
    rows = np.where(a.max(axis=1) > thr)[0]
    if len(rows) == 0:
        return img
    y0, y1 = max(0, rows[0] - margin), min(img.height, rows[-1] + margin + 1)
    if y1 - y0 < 6:
        return img
    return img.crop((0, y0, img.width, y1))


# (upscale, invert, channel mode) - tried in order until one parses
READ_ATTEMPTS = [(6, False, "min"), (6, False, "gray"), (4, False, "min"), (6, True, "min"),
                 (5, False, "gray"), (8, False, "min")]


def read_exp_from_line(ocr: Ocr, crop: Image.Image, prev_level, hint, expected_exp=None,
                       expected_tol=None, debug_dir: Path | None = None):
    """
    OCR a locked crop with a few upscale/invert variants.
    Returns (level, exp, pct, text) on success, (None, None, None, text) when
    the bar was read but not parsed, or None when OCR returned nothing.
    Strict parses win; separator-free recovery is used only once a level is known.
    """
    texts = []
    fallback = None
    tight = tight_rows(crop)
    variants = [(tight, a) for a in READ_ATTEMPTS]
    if tight.size != crop.size:
        variants += [(crop, a) for a in READ_ATTEMPTS[:2]]
    for img, (scale, invert, mode) in variants:
        text = ocr.read_line(img, scale=scale, invert=invert, mode=mode)
        if not text:
            continue
        texts.append(text)
        r = resolve(text, prev_level, hint or level_hint(text), expected_exp, expected_tol)
        if r is None:
            continue
        lv, exp, pct, how = r
        if how == "strict":
            return lv, exp, pct, text
        if fallback is None:
            fallback = (lv, exp, pct, text)
    if fallback:
        return fallback
    if debug_dir:
        crop.save(debug_dir / f"fail_{int(time.time())}.png")
    return None if not texts else (None, None, None, texts[0])


# --------------------------------------------------------------------------
# Locating the EXP text
# --------------------------------------------------------------------------
def locate_exp_region(ocr: Ocr, img: Image.Image, origin=(0, 0), debug_dir: Path | None = None):
    """
    Find the EXP readout in an image (a status-bar strip or a full screenshot).
    Returns ((left, top, width, height) in screen coordinates, level hint) or (None, None).
    """
    # the detector finds small text best at native size (its recognizer
    # rescales each crop itself); the locked-region reader upscales 6x later
    for scale, invert, mode in ((1, False, "min"), (1, False, "gray"), (1, True, "min"),
                                (2, False, "min"), (2, True, "gray"), (3, False, "gray")):
        boxes = ocr.read_boxes(img, scale=scale, invert=invert, mode=mode)
        if debug_dir:
            with open(debug_dir / "locate_log.txt", "a", encoding="utf-8") as f:
                f.write(f"scale={scale} invert={invert} mode={mode}\n")
                for b in boxes:
                    f.write(f"  {b}\n")
        hit = None
        for b in boxes:
            if parse_readout(b[4]):
                hit = b
                break
        if hit is None:  # separators lost: accept a '%' box with enough digits
            for b in boxes:
                if "%" in b[4] and len(re.sub(r"\D", "", b[4])) >= 5 and "/" not in b[4]:
                    hit = b
                    break
        if hit is None:
            continue

        def row_hint(bxs):
            # the level sits on the same row, left of the EXP text (yellow digits,
            # so the luminance pass sees them when the min-channel pass doesn't)
            for b in bxs:
                if b[2] <= hit[0] and (min(hit[3], b[3]) - max(hit[1], b[1])) > 0.5 * (hit[3] - hit[1]):
                    lv = level_hint(b[4])
                    if lv and "%" not in b[4] and "/" not in b[4]:
                        return lv
            return None

        hint = row_hint(boxes)
        if hint is None and mode != "gray":
            hint = row_hint(ocr.read_boxes(img, scale=scale, invert=invert, mode="gray"))
        x0, y0, x1, y1, _, _ = hit
        h = y1 - y0
        # pull in an "EXP" label box sitting just left on the same row
        for b in boxes:
            if b is hit:
                continue
            same_row = (min(y1, b[3]) - max(y0, b[1])) > 0.5 * h
            if same_row and b[2] <= x0 + 2 and x0 - b[2] < 3 * h and "EX" in b[4].upper():
                x0 = min(x0, b[0])
        w = x1 - x0
        left = int(origin[0] + x0 - 0.15 * h)
        top = int(origin[1] + y0 - 0.35 * h)
        width = int(w + 0.15 * h + 0.30 * w)   # room for the number to grow
        height = int(h * 1.7)
        return (max(0, left), max(0, top), width, height), hint
    return None, None


# --------------------------------------------------------------------------
# Screen / window capture
# --------------------------------------------------------------------------
def _make_dpi_aware():
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class Grabber:
    def __init__(self, monitor_index: int = 1):
        import mss
        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        mons = self.sct.monitors
        self.monitor = mons[monitor_index] if monitor_index < len(mons) else mons[1]

    def grab(self, rect) -> Image.Image:
        left, top, width, height = rect
        shot = self.sct.grab({"left": int(left), "top": int(top), "width": int(width), "height": int(height)})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# --------------------------------------------------------------------------
# Farming rate: per-minute buckets, idle minutes dropped, outliers trimmed
# --------------------------------------------------------------------------
def minute_buckets(samples):
    """samples = [(t, cumulative_gained)] -> {minute_index: exp gained in that minute}."""
    if len(samples) < 2:
        return {}
    t0 = samples[0][0]
    b = {}
    for (ta, ga), (tb, gb) in zip(samples, samples[1:]):
        d = gb - ga
        if tb <= ta or d == 0:
            continue
        ia, ib = int((ta - t0) // 60), int((tb - t0) // 60)
        if ia == ib:
            b[ia] = b.get(ia, 0.0) + d
            continue
        for i in range(ia, ib + 1):          # spread the gain over the minutes it spans
            lo = max(ta, t0 + i * 60)
            hi = min(tb, t0 + (i + 1) * 60)
            if hi > lo:
                b[i] = b.get(i, 0.0) + d * (hi - lo) / (tb - ta)
    return b


def farm_rate(samples, min_rate=50.0, last_n=15, trim=1 / 6):
    """
    EXP/min while actually farming: minutes with < min_rate EXP are ignored
    (standing around, shop, walking), only the last `last_n` active minutes
    count, and the top/bottom `trim` share of those (the fresh-spawn clear
    on arrival, a lag spike) is dropped before averaging.
    Returns (rate_per_min, active_minutes_used) or (None, 0).
    """
    b = minute_buckets(samples)
    if not b:
        return None, 0
    last = max(b)
    active = [(i, v) for i, v in sorted(b.items()) if v >= min_rate and i < last]   # current minute is partial
    if not active:
        return None, 0
    active = active[-last_n:]
    vals = sorted(v for _, v in active)
    cut = int(len(vals) * trim)
    core = vals[cut:len(vals) - cut] if len(vals) - 2 * cut >= 1 else vals
    return sum(core) / len(core), len(active)


# --------------------------------------------------------------------------
# Session statistics
# --------------------------------------------------------------------------
class Session:
    """
    Accumulates EXP over time from OCR samples.

    Guard rails (OCR is never fully trusted):
      - the session only starts once two consecutive samples agree (same
        level, EXP within 5% of a level) - a single garbled first read can't
        seed a bogus baseline;
      - a sample that implies a level change, or a jump of more than 25% of a
        level (or a loss of more than 15%), is held as pending and only
        accepted if the next sample agrees with it;
      - a level going DOWN is impossible, so such a sample is dropped.
    """

    def __init__(self, window_min: float = 5.0):
        self.window = window_min * 60.0
        self.t0 = None
        self.level0 = self.exp0 = self.pct0 = None
        self.last = None          # (t, level, exp, pct) last accepted sample
        self.tentative = None     # first sample, waiting for confirmation
        self.pending = None       # suspicious sample awaiting confirmation
        self.gained = 0
        self.lost = 0
        self.levelups = 0
        self.ok = 0
        self.bad = 0
        self.hist = deque()       # (t, cumulative gained)
        self.samples = []         # (t, cumulative gained), every accepted sample

    def farm_rate(self, min_rate=50.0, last_n=15):
        return farm_rate(self.samples, min_rate, last_n)

    @staticmethod
    def _delta(prev, cur):
        _, pl, pe, _ = prev
        _, cl, ce, _ = cur
        if cl == pl:
            return ce - pe
        if cl > pl:
            d = EXP_TABLE[pl] - pe
            for lv in range(pl + 1, cl):
                d += EXP_TABLE[lv]
            return d + ce
        return None  # level went down: OCR garbage

    @staticmethod
    def _agrees(a, b) -> bool:
        return a is not None and b is not None and a[1] == b[1] and abs(a[2] - b[2]) <= 0.05 * EXP_TABLE[a[1]]

    def add(self, t: float, level: int, exp: int, pct: float) -> bool:
        cur = (t, level, exp, pct)
        if self.last is None:
            if self._agrees(self.tentative, cur):
                t0 = self.tentative
                self.t0, self.level0, self.exp0, self.pct0 = t0[0], t0[1], t0[2], t0[3]
                self.last = t0
                self.hist.append((t0[0], 0))
                self.samples.append((t0[0], 0))
                self.ok += 1
                self.tentative = None
                # fall through: accept cur as the second sample
            else:
                self.tentative = cur
                return False
        d = self._delta(self.last, cur)
        if d is None:
            self.bad += 1
            return False
        need = EXP_TABLE[self.last[1]]
        suspicious = level != self.last[1] or d > 0.25 * need or d < -0.15 * need
        if suspicious:
            if self._agrees(self.pending, cur):
                pass  # two consecutive readings agree: accept
            else:
                self.pending = cur
                self.bad += 1
                return False
        self.pending = None
        if level > self.last[1]:
            self.levelups += level - self.last[1]
        if d < 0:
            self.lost += -d
        self.gained += d
        self.last = cur
        self.ok += 1
        self.hist.append((t, self.gained))
        self.samples.append((t, self.gained))
        cutoff = t - self.window
        while len(self.hist) > 2 and self.hist[1][0] <= cutoff:
            self.hist.popleft()
        return True

    @property
    def elapsed(self) -> float:
        return 0.0 if self.last is None else self.last[0] - self.t0

    def avg_rate(self) -> float:
        e = self.elapsed
        return self.gained / e * 60.0 if e > 0 else 0.0

    def rolling_rate(self) -> float:
        if len(self.hist) < 2:
            return 0.0
        (ta, ga), (tb, gb) = self.hist[0], self.hist[-1]
        return (gb - ga) / (tb - ta) * 60.0 if tb > ta else 0.0

    def to_next(self):
        if self.last is None:
            return None
        _, lv, exp, _ = self.last
        return EXP_TABLE[lv] - exp

    def summary(self) -> dict:
        return {
            "started": datetime.fromtimestamp(self.t0).isoformat(timespec="seconds") if self.t0 else None,
            "elapsed_min": round(self.elapsed / 60.0, 2),
            "start": {"level": self.level0, "exp": self.exp0, "pct": self.pct0},
            "end": {"level": self.last[1], "exp": self.last[2], "pct": self.last[3]} if self.last else None,
            "exp_gained": self.gained,
            "exp_lost": self.lost,
            "levelups": self.levelups,
            "avg_exp_per_min": round(self.avg_rate(), 1),
            "avg_exp_per_hour": round(self.avg_rate() * 60, 0),
            "farm_exp_per_min": round(self.farm_rate()[0], 1) if self.farm_rate()[0] is not None else None,
            "farm_exp_per_hour": round(self.farm_rate()[0] * 60, 0) if self.farm_rate()[0] is not None else None,
            "farm_active_minutes": self.farm_rate()[1],
            "rolling_exp_per_min": round(self.rolling_rate(), 1),
            "samples_ok": self.ok,
            "samples_bad": self.bad,
        }


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
def fmt_min(m: float) -> str:
    if m <= 0 or m == float("inf"):
        return "-"
    h, mm = divmod(int(round(m)), 60)
    return f"{h}h{mm:02d}m" if h else f"{mm}m"


def render(s: Session, rect, last_text: str, csv_path: Path) -> str:
    lines = []
    if s.last:
        _, lv, exp, pct = s.last
        need = EXP_TABLE[lv]
        avg, roll = s.avg_rate(), s.rolling_rate()
        rem = need - exp
        eta_roll = rem / roll if roll > 0 else float("inf")
        eta_avg = rem / avg if avg > 0 else float("inf")
        lines.append(f"Lv {lv}   EXP {exp:,} / {need:,} [{pct:.2f}%]   {datetime.now():%H:%M:%S}")
        lines.append(f"Session {fmt_min(s.elapsed / 60)}   gained {s.gained:,} EXP"
                     + (f"   lost {s.lost:,}" if s.lost else "")
                     + (f"   level-ups {s.levelups}" if s.levelups else ""))
        lines.append(f"AVG {avg:,.0f} EXP/min  ({avg * 60:,.0f}/h)    last {int(s.window // 60)} min: {roll:,.0f} EXP/min")
        lines.append(f"To Lv {lv + 1}: {rem:,} EXP   ETA {fmt_min(eta_roll)} at rolling rate, {fmt_min(eta_avg)} at avg")
    else:
        lines.append("Waiting for a clean read of the EXP bar...")
    lines.append(f"reads ok {s.ok} / bad {s.bad}   region {tuple(int(v) for v in rect) if rect else '-'}   ocr: {last_text[:60]!r}")
    lines.append(f"log: {csv_path}   (Ctrl+C to stop and print the summary)")
    return "\n".join(lines)


class LiveBlock:
    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stdout.isatty()
        self.n = 0
        if self.enabled and sys.platform == "win32":
            os.system("")  # enable ANSI escapes in the Windows console

    def show(self, text: str):
        if not self.enabled:
            print(text.splitlines()[0] if text else "", flush=True)
            return
        if self.n:
            sys.stdout.write(f"\x1b[{self.n}A")
        for line in text.splitlines():
            sys.stdout.write("\x1b[2K" + line + "\n")
        self.n = len(text.splitlines())
        sys.stdout.flush()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def parse_rect(s: str):
    parts = [int(p) for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected L,T,W,H")
    return tuple(parts)


def main():
    ap = argparse.ArgumentParser(description="MapleStory Classic World EXP/min tracker")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between samples (default 10)")
    ap.add_argument("--window-min", type=float, default=5.0, help="rolling window in minutes (default 5)")
    ap.add_argument("--monitor", type=int, default=1, help="mss monitor index to capture (1 = primary)")
    ap.add_argument("--region", type=parse_rect, help="fixed screen rect L,T,W,H of the EXP text")
    ap.add_argument("--level", type=int, help="level hint for ambiguous reads")
    ap.add_argument("--once", action="store_true", help="read once and exit")
    ap.add_argument("--image", help="parse a saved screenshot and exit")
    ap.add_argument("--debug", action="store_true", help="save crops to ./maple_exp_debug")
    ap.add_argument("--plain", action="store_true", help="one line per sample instead of a live block")
    ap.add_argument("--csv", help="CSV path (default maple_exp_<date>.csv next to the script)")
    args = ap.parse_args()

    debug_dir = None
    if args.debug:
        debug_dir = Path("maple_exp_debug")
        debug_dir.mkdir(exist_ok=True)

    ocr = Ocr()

    # ---- offline mode: a saved screenshot -------------------------------
    if args.image:
        img = Image.open(args.image).convert("RGB")
        rect, hint = locate_exp_region(ocr, img, (0, 0), debug_dir)
        if not rect:
            sys.exit("EXP readout not found in the image (try --debug and look at maple_exp_debug/locate_log.txt)")
        crop = img.crop((rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))
        if debug_dir:
            crop.save(debug_dir / "image_crop.png")
        r = read_exp_from_line(ocr, crop, None, args.level or hint, None, None, debug_dir)
        if not r or r[0] is None:
            sys.exit(f"found the bar at {rect} but could not parse it. OCR text: {r[3] if r else ''!r}")
        lv, exp, pct, text = r
        print(json.dumps({"region": rect, "level": lv, "exp": exp, "pct": pct,
                          "to_next": EXP_TABLE[lv] - exp, "level_hint_on_bar": hint, "ocr_text": text}, indent=2))
        return

    # ---- live mode -------------------------------------------------------
    _make_dpi_aware()
    import signal

    def _stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    grab = Grabber(args.monitor)
    rect = args.region
    hint = args.level

    def locate():
        m = grab.monitor
        strip = (m["left"], m["top"] + int(m["height"] * 0.70), m["width"], int(m["height"] * 0.30))
        img = grab.grab(strip)
        if debug_dir:
            img.save(debug_dir / "strip.png")
        return locate_exp_region(ocr, img, (strip[0], strip[1]), debug_dir)

    if rect is None:
        print("Locating the EXP bar on the monitor (scanning the bottom 30% of the screen)...", flush=True)
        for attempt in range(3):
            rect, bar_hint = locate()
            if rect:
                hint = hint or bar_hint
                break
            time.sleep(2)
        if not rect:
            sys.exit("Could not find the EXP readout. Make sure the game is windowed/borderless and visible, "
                     "then retry; or pass --region L,T,W,H (use --debug and check maple_exp_debug/strip.png).")
        print(f"EXP bar locked at {rect}" + (f", level on bar reads {hint}" if hint else ""), flush=True)

    if args.once:
        crop = grab.grab(rect)
        if debug_dir:
            crop.save(debug_dir / "once_crop.png")
        r = read_exp_from_line(ocr, crop, None, hint, None, None, debug_dir)
        if not r or r[0] is None:
            sys.exit(f"bar found at {rect} but not parsed; OCR text: {r[3] if r else ''!r} (try --debug)")
        lv, exp, pct, text = r
        print(json.dumps({"region": rect, "level": lv, "exp": exp, "pct": pct,
                          "to_next": EXP_TABLE[lv] - exp, "ocr_text": text}, indent=2))
        return

    csv_path = Path(args.csv) if args.csv else Path(__file__).resolve().parent / f"maple_exp_{datetime.now():%Y-%m-%d_%H%M}.csv"
    new_file = not csv_path.exists()
    csv_f = open(csv_path, "a", newline="", encoding="utf-8")
    writer = csv.writer(csv_f)
    if new_file:
        writer.writerow(["time", "level", "exp", "pct", "gained_total", "avg_exp_per_min", "rolling_exp_per_min", "ocr_text"])

    s = Session(args.window_min)
    live = LiveBlock(not args.plain)
    fails = 0
    last_text = ""
    try:
        while True:
            t = time.time()
            crop = grab.grab(rect)
            if debug_dir:
                crop.save(debug_dir / "last_crop.png")
            expected = tol = None
            if s.last:
                dt = t - s.last[0]
                per_sec = s.rolling_rate() / 60.0
                expected = s.last[2] + per_sec * dt
                tol = max(0.01 * EXP_TABLE[s.last[1]], 3 * per_sec * dt + 100)
            # after 12 straight failures stop assuming the locked level (wide mode)
            prev_for_read = s.last[1] if (s.last and fails < 12) else None
            r = read_exp_from_line(ocr, crop, prev_for_read, hint, expected, tol, debug_dir)
            if r and r[0] is not None:
                lv, exp, pct, last_text = r
                fails = 0
                if s.add(t, lv, exp, pct):
                    writer.writerow([datetime.fromtimestamp(t).isoformat(timespec="seconds"), lv, exp, pct,
                                     s.gained, round(s.avg_rate(), 1), round(s.rolling_rate(), 1), last_text])
                    csv_f.flush()
            else:
                last_text = r[3] if r else ""
                s.bad += 1
                fails += 1
                if fails % 6 == 0 and args.region is None:
                    # window moved / resized: re-locate
                    new_rect, _ = locate()
                    if new_rect:
                        rect = new_rect
            live.show(render(s, rect, last_text, csv_path))
            time.sleep(max(0.5, args.interval - (time.time() - t)))
    except KeyboardInterrupt:
        pass
    finally:
        csv_f.close()

    print()
    summ = s.summary()
    print(json.dumps(summ, indent=2))
    out = csv_path.with_suffix(".summary.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summ, f, indent=2)
    print(f"summary -> {out}")


if __name__ == "__main__":
    main()
