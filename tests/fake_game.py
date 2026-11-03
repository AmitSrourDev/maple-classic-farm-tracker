"""Writes a fake MapleStory window image with EXP rising over time (for an Xvfb live test)."""
import sys, time, random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_synth import FONTS, font_of, render_bar
import maple_exp as M
from PIL import Image, ImageDraw
out = Path(sys.argv[1]); secs = float(sys.argv[2]); font = font_of(FONTS["pil-bitmap"])
lv, exp = 24, 40200; rnd = random.Random(1); t0 = time.time(); W, H = 1024, 768
while time.time() - t0 < secs:
    need = M.EXP_TABLE[lv]; pct = int(exp / need * 10000) / 100.0
    img = Image.new("RGB", (W, H), (90, 140, 200)); d = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = rnd.randint(0, W), rnd.randint(0, H - 80)
        d.rectangle((x, y, x + 40, y + 20), fill=(rnd.randint(0,255), rnd.randint(0,255), rnd.randint(0,255)))
    bar = render_bar(lv, exp, pct, font, width=W); img.paste(bar, (0, H - bar.height))
    tmp = out.with_suffix(".tmp.png"); img.save(tmp); tmp.replace(out)
    print(f"{time.time()-t0:5.1f}s  truth lv {lv} exp {exp} [{pct:.2f}%]", flush=True)
    time.sleep(3)
    exp += rnd.randint(60, 140)
    if exp >= need: exp -= need; lv += 1
