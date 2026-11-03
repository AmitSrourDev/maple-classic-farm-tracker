"""Fake game v3: the second real screenshot (ultrawide, cropped to 2560x1440: game + part of the
console window) with EXP, two quickslot counts and the inventory meso number animated.
usage: fake_game3.py <shot2.png> <out.png> <seconds>"""
import sys, time, random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maple_exp as M  # noqa: E402

shot, out, secs = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3])
base = Image.open(shot).convert("RGB").crop((0, 0, 2560, 1440))
f20 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
f18b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
bar_bg = base.getpixel((1600, 1392))
cells = {"Del": (1828, 1244), "End": (1894, 1244)}
lv, exp = 30, 94760
counts = {"Del": 142, "End": 60}
mesos = 25562
rnd = random.Random(4)
t0 = time.time(); tick = 0
while time.time() - t0 < secs:
    el = time.time() - t0
    img = base.copy(); d = ImageDraw.Draw(img)
    grinding = el < 110 or el >= 140
    need = M.EXP_TABLE[lv]; pct = int(exp / need * 10000) / 100.0
    d.rectangle((1400, 1372, 1610, 1410), fill=bar_bg)
    d.text((1404, 1380), f"{exp:,} [{pct:.2f}%]", fill=(255, 255, 255), font=f20)
    for key, (x, y) in cells.items():
        d.rectangle((x + 1, y + 34, x + 46, y + 61), fill=(226, 228, 232))
        d.text((x + 3, y + 36), str(counts[key]), fill=(255, 255, 255), font=f18b, stroke_width=2, stroke_fill=(10, 10, 10))
    # inventory meso bar: number right-aligned before the MESOS label (label at x=2493)
    d.rectangle((2300, 960, 2488, 992), fill=(236, 236, 240))
    txt = f"{mesos:,}"; tw = d.textlength(txt, font=f20)
    d.text((2484 - tw, 964), txt, fill=(30, 30, 30), font=f20)
    tmp = out.with_suffix(".tmp.png"); img.save(tmp); tmp.replace(out)
    print(f"{el:5.1f}s lv {lv} exp {exp} [{pct:.2f}%] Del {counts['Del']} End {counts['End']} mesos {mesos} {'grind' if grinding else 'IDLE'}", flush=True)
    time.sleep(3); tick += 1
    if grinding:
        exp += rnd.randint(100, 260)
        if exp >= need: exp -= need; lv += 1
        if tick % 2 == 0: counts["Del"] -= 1
        if tick % 3 == 0: counts["End"] -= 1
        mesos += rnd.randint(40, 160)
