"""Fake game v2 for an Xvfb live test: the REAL screenshot as background, with the EXP
number, two quickslot counts and (later) the map name re-drawn over time.
usage: fake_game2.py <shot.png> <out.png> <seconds>"""
import sys, time, random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maple_exp as M  # noqa: E402

shot, out, secs = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3])
base = Image.open(shot).convert("RGB")
f20 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
f18b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
f19 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 19)
bar_bg = base.getpixel((1600, 1392))
grid = {"x0": 1762.2, "y0": 1177.1, "pitch": 65.5, "size": 61.6}
cells = {"Del": (1828, 1243), "End": (1893, 1243)}
lv, exp = 27, 11323
counts = {"Del": 179, "End": 224}
rnd = random.Random(2)
t0 = time.time()
tick = 0
while time.time() - t0 < secs:
    el = time.time() - t0
    img = base.copy()
    d = ImageDraw.Draw(img)
    # phases: 0-100 s grind, 100-135 idle, 135+ grind on another map
    grinding = el < 100 or el >= 135
    if el >= 135:
        d.rectangle((90, 76, 430, 106), fill=img.getpixel((300, 110)))
        d.text((96, 78), "Ant Tunnel I", fill=(235, 235, 245), font=f19)
    need = M.EXP_TABLE[lv]
    pct = int(exp / need * 10000) / 100.0
    d.rectangle((1400, 1372, 1610, 1410), fill=bar_bg)
    d.text((1404, 1380), f"{exp:,} [{pct:.2f}%]", fill=(255, 255, 255), font=f20)
    for key, (x, y) in cells.items():
        d.rectangle((x + 1, y + 34, x + 46, y + 61), fill=(226, 228, 232))
        d.text((x + 3, y + 36), str(counts[key]), fill=(255, 255, 255), font=f18b, stroke_width=2, stroke_fill=(10, 10, 10))
    tmp = out.with_suffix(".tmp.png")
    img.save(tmp)
    tmp.replace(out)
    print(f"{el:5.1f}s lv {lv} exp {exp} [{pct:.2f}%] Del {counts['Del']} End {counts['End']} {'grind' if grinding else 'IDLE'}", flush=True)
    time.sleep(3)
    tick += 1
    if grinding:
        exp += rnd.randint(100, 260)
        if exp >= need:
            exp -= need
            lv += 1
        if tick % 2 == 0:
            counts["Del"] -= 1
        if tick % 3 == 0:
            counts["End"] -= 1
