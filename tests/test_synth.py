"""Synthetic status-bar test: render classic-style bars at tiny font sizes, run the
real locate + read + Session path from maple_exp.py over a simulated grind
(including a level-up), and report exact / missed / WRONG reads per font."""
import random
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maple_exp as M  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

FONTS = {
    "pil-bitmap": None,
    "dejavu-9": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 9),
    "dejavu-10": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10),
    "dejavu-11": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11),
    "dejavu-12": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12),
    "mono-10": ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10),
}


def font_of(spec):
    if spec is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(spec[0], spec[1])
    except OSError:          # font not installed on this machine (CI runners): fall back
        return ImageFont.load_default(size=spec[1]) if hasattr(ImageFont, "load_default") else ImageFont.load_default()


def render_bar(level, exp, pct, font, width=1024):
    """Mimic the pre-BB bottom bar: dark strip, coloured HP/MP/EXP bars with text on top."""
    h = 28
    img = Image.new("RGB", (width, h), (38, 38, 42))
    d = ImageDraw.Draw(img)
    d.text((6, 8), f"LV.{level}", fill=(255, 220, 60), font=font)
    d.text((60, 8), "PostQuantum", fill=(235, 235, 235), font=font)
    d.text((150, 8), "Rogue", fill=(235, 235, 235), font=font)
    x = 230
    for label, cur, mx, col in (("HP", 750, 750, (190, 40, 40)), ("MP", 584, 584, (40, 70, 190))):
        d.text((x, 8), label, fill=(255, 255, 255), font=font)
        bx = x + 22
        d.rectangle((bx, 9, bx + 110, 19), fill=(20, 20, 20))
        d.rectangle((bx, 9, bx + int(110 * cur / mx), 19), fill=col)
        d.text((bx + 4, 8), f"[{cur}/{mx}]", fill=(255, 255, 255), font=font)
        x += 150
    d.text((x, 8), "EXP", fill=(255, 255, 255), font=font)
    bx = x + 26
    d.rectangle((bx, 9, bx + 150, 19), fill=(20, 20, 20))
    d.rectangle((bx, 9, bx + int(150 * pct / 100), 19), fill=(170, 140, 30))
    d.text((bx + 4, 8), f"{exp}[{pct:.2f}%]", fill=(255, 255, 255), font=font)
    for i in range(8):
        qx = bx + 170 + i * 30
        d.rectangle((qx, 4, qx + 24, 24), outline=(120, 120, 120))
        d.text((qx + 6, 8), str(i + 1), fill=(200, 200, 200), font=font)
    return img


def cases(n, seed=1):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        lv = rnd.randint(20, 45)
        need = M.EXP_TABLE[lv]
        exp = rnd.randint(0, need - 1)
        pct = int(exp / need * 10000) / 100.0
        out.append((lv, exp, pct))
    return out


def grind(n, seed=3, level=24, start_frac=0.97):
    """Simulated session: ~10 s frames, 40-180 EXP per frame, crosses a level-up."""
    rnd = random.Random(seed)
    lv = level
    exp = int(M.EXP_TABLE[lv] * start_frac)
    t = 0.0
    out = []
    for _ in range(n):
        need = M.EXP_TABLE[lv]
        pct = int(exp / need * 10000) / 100.0
        out.append((t, lv, exp, pct))
        gain = rnd.randint(40, 180)
        exp += gain
        if exp >= need:
            exp -= need
            lv += 1
        t += 10.0
    return out


def main():
    ocr = M.Ocr()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    grand = {"exact": 0, "miss": 0, "wrong": 0, "n": 0}
    for name, spec in FONTS.items():
        font = font_of(spec)
        frames = grind(n)
        first = render_bar(frames[0][1], frames[0][2], frames[0][3], font)
        first.save(OUT / f"bar_{name}.png")
        t0 = time.time()
        rect, hint = M.locate_exp_region(ocr, first, (0, 0))
        t_loc = time.time() - t0
        if not rect:
            print(f"{name:12s} LOCATE FAILED")
            grand["miss"] += n
            grand["n"] += n
            continue
        s = M.Session(5.0)
        exact = miss = wrong = 0
        wrong_list = []
        t_read = 0.0
        truth_gain = 0
        for i, (t, lv, exp, pct) in enumerate(frames):
            img = render_bar(lv, exp, pct, font)
            crop = img.crop((rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))
            if i == 0:
                crop.save(OUT / f"crop_{name}.png")
            expected = tol = None
            if s.last:
                dt = t - s.last[0]
                per_sec = s.rolling_rate() / 60.0
                expected = s.last[2] + per_sec * dt
                tol = max(0.01 * M.EXP_TABLE[s.last[1]], 3 * per_sec * dt + 100)
            t1 = time.time()
            r = M.read_exp_from_line(ocr, crop, s.last[1] if s.last else None, hint, expected, tol)
            t_read += time.time() - t1
            if r and r[0] is not None:
                if r[0] == lv and r[1] == exp and abs(r[2] - pct) < 1e-6:
                    exact += 1
                else:
                    wrong += 1
                    wrong_list.append(((lv, exp, pct), r))
                s.add(t, r[0], r[1], r[2])
            else:
                miss += 1
        # truth: total EXP gained between first and last frame
        f0, f1 = frames[0], frames[-1]
        truth_gain = (M.EXP_TABLE[f0[1]] - f0[2] + f1[2]) if f1[1] > f0[1] else f1[2] - f0[2]
        print(f"{name:12s} locate {t_loc:.1f}s hint={hint}  exact {exact}/{n}  miss {miss}  WRONG {wrong}  "
              f"read {1000 * t_read / n:.0f}ms/frame  session gained {s.gained} (truth {truth_gain}) "
              f"levelups {s.levelups} avg {s.avg_rate():.0f}/min")
        for w in wrong_list[:3]:
            print("    wrong:", w)
        grand["exact"] += exact
        grand["miss"] += miss
        grand["wrong"] += wrong
        grand["n"] += n
    print(f"TOTAL exact {grand['exact']}/{grand['n']}  miss {grand['miss']}  WRONG {grand['wrong']}")


if __name__ == "__main__":
    main()
