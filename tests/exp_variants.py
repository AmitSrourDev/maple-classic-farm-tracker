"""Compare crop preprocessing variants for the single-line read path."""
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import maple_exp as M  # noqa: E402
from test_synth import FONTS, font_of, render_bar, cases  # noqa: E402

from rapidocr_onnxruntime import RapidOCR

eng = RapidOCR()
eng.use_angle_cls = False
eng.use_text_det = False


def rec(arr):
    res, _ = eng(arr)
    return " ".join(r[1] for r in res) if res else ""


def v_gray(img, scale=4, invert=False):
    g = ImageOps.grayscale(img)
    if invert:
        g = ImageOps.invert(g)
    g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    g = ImageOps.expand(g, border=12, fill=255 if invert else 0)
    return np.array(g.convert("RGB"))


def v_bin(img, scale=4, thr=170, invert=True, blur=0.0, resample=Image.LANCZOS):
    g = ImageOps.grayscale(img)
    b = g.point(lambda p: 255 if p >= thr else 0)
    if invert:
        b = ImageOps.invert(b)
    b = b.resize((b.width * scale, b.height * scale), resample)
    if blur:
        b = b.filter(ImageFilter.GaussianBlur(blur))
    b = ImageOps.expand(b, border=12, fill=255 if invert else 0)
    return np.array(b.convert("RGB"))


def v_bin_white(img, scale=4, thr=170):
    """keep only near-white pixels (text), everything else -> black; then invert."""
    arr = np.array(img).astype(np.int32)
    mn = arr.min(axis=2)  # white text has all channels high
    mask = (mn >= thr)
    out = np.where(mask, 0, 255).astype(np.uint8)
    b = Image.fromarray(out, "L").resize((img.width * scale, img.height * scale), Image.LANCZOS)
    b = ImageOps.expand(b, border=12, fill=255)
    return np.array(b.convert("RGB"))


VARIANTS = {
    "gray4": lambda im: v_gray(im, 4),
    "gray4inv": lambda im: v_gray(im, 4, True),
    "bin4inv": lambda im: v_bin(im, 4),
    "bin4": lambda im: v_bin(im, 4, invert=False),
    "bin5inv": lambda im: v_bin(im, 5),
    "bin6inv": lambda im: v_bin(im, 6),
    "bin4inv_near_blur": lambda im: v_bin(im, 4, resample=Image.NEAREST, blur=1.2),
    "white4": lambda im: v_bin_white(im, 4),
    "white5": lambda im: v_bin_white(im, 5),
}


def crop_for(img, font):
    # ground-truth crop: EXP label through end of bar (layout from render_bar)
    x = 230 + 2 * 150
    return img.crop((x - 2, 4, x + 26 + 154, 24))


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    names = list(VARIANTS)
    print("font/variant".ljust(14) + "".join(v.rjust(18) for v in names))
    tot = {v: 0 for v in names}
    cnt = 0
    for fname, spec in FONTS.items():
        font = font_of(spec)
        row = []
        for v in names:
            ok = 0
            t0 = time.time()
            cs = cases(n, seed=7)
            for lv, exp, pct in cs:
                img = render_bar(lv, exp, pct, font)
                crop = crop_for(img, font)
                text = rec(VARIANTS[v](crop))
                hit = any(c == (exp, pct) for c in M.parse_readout(text))
                ok += hit
            tot[v] += ok
            row.append(f"{ok}/{n} {1000*(time.time()-t0)/n:.0f}ms")
        cnt += n
        print(fname.ljust(14) + "".join(r.rjust(18) for r in row))
    print("TOTAL".ljust(14) + "".join(f"{tot[v]}/{cnt}".rjust(18) for v in names))


if __name__ == "__main__":
    main()
