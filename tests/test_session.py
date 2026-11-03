"""Pure-logic tests for Session guard rails and the parser (no OCR)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maple_exp as M  # noqa: E402

T = M.EXP_TABLE


def pct(lv, exp):
    return int(exp / T[lv] * 10000) / 100.0


def feed(s, seq):
    acc = []
    for i, (lv, exp) in enumerate(seq):
        acc.append(s.add(i * 10.0, lv, exp, pct(lv, exp)))
    return acc


def test_plain_grind():
    s = M.Session()
    seq = [(24, 21000 + 120 * i) for i in range(12)]
    acc = feed(s, seq)
    assert acc == [False] + [True] * 11, acc
    assert s.gained == 120 * 11
    assert abs(s.avg_rate() - 120 * 6) < 1e-6  # 120 per 10 s = 720/min


def test_levelup_confirmed():
    s = M.Session()
    seq = [(24, 40300), (24, 40420), (25, 30), (25, 150), (25, 270)]
    acc = feed(s, seq)
    assert acc == [False, True, False, True, True], acc  # (25,30) pending, then confirmed by (25,150)
    assert s.levelups == 1
    assert s.gained == (40512 - 40300) + 270, s.gained


def test_spurious_levelup_rejected():
    s = M.Session()
    # one bogus 'level 25' read in the middle of level 24 must not stick
    seq = [(24, 30000), (24, 30120), (25, 300), (24, 30360), (24, 30480)]
    acc = feed(s, seq)
    assert acc == [False, True, False, True, True], acc
    assert s.levelups == 0 and s.gained == 480


def test_bogus_first_sample_discarded():
    s = M.Session()
    seq = [(18, 414), (24, 30000), (24, 30120), (24, 30240)]
    acc = feed(s, seq)
    assert acc == [False, False, True, True], acc
    assert s.level0 == 24 and s.exp0 == 30000 and s.gained == 240


def test_big_spike_needs_confirmation():
    s = M.Session()
    seq = [(24, 10000), (24, 10100), (24, 30000), (24, 10300), (24, 10400)]
    acc = feed(s, seq)
    assert acc == [False, True, False, True, True], acc
    assert s.gained == 400


def test_quest_turnin_confirmed():
    s = M.Session()
    # +2750 EXP quest (6.8% of Lv24) is under the 25% threshold: accepted immediately
    seq = [(24, 10000), (24, 10100), (24, 12850), (24, 12970)]
    acc = feed(s, seq)
    assert acc == [False, True, True, True], acc
    assert s.gained == 2970


def test_death_loss_accepted():
    s = M.Session()
    seq = [(24, 20000), (24, 20100), (24, 18000), (24, 18100)]  # -2100, about 5%
    acc = feed(s, seq)
    assert acc == [False, True, True, True], acc
    assert s.lost == 2100 and s.gained == -1900


def test_level_down_dropped():
    s = M.Session()
    seq = [(25, 5000), (25, 5100), (24, 5200), (25, 5300)]
    acc = feed(s, seq)
    assert acc == [False, True, False, True], acc


def test_parser_strict_variants():
    for txt, want in [
        ("EXP 21066[52.00%]", (21066, 52.00)),
        ("EXP21066(52.00%)", (21066, 52.00)),
        ("EXP 21066l52.00%", (21066, 52.00)),
        ("EXP 21066[52 00%]", (21066, 52.00)),
        ("21O66[52.OO%]", (21066, 52.00)),
        ("EXP 2457[4.47% ", (2457, 4.47)),
        ("EXP 1104[0.41%]", (1104, 0.41)),
    ]:
        c = M.parse_readout(txt)
        assert want in c, (txt, c)
    assert M.resolve("EXP 21066[52.00%]", None, None)[:3] == (24, 21066, 52.00)
    assert M.resolve("HP [750/750] MP [584/584] EXP 21066[52.00%]", None, None)[:3] == (24, 21066, 52.00)


def test_parser_brute_force():
    # separators lost entirely; level known
    assert M.resolve("EXP 246751139%", 37, None, 24600)[:3] == (37, 24675, 11.39)
    assert M.resolve("EXP 76387170.41%", 31, None, 76300)[:3] == (31, 76387, 70.41)   # '[' read as '1'
    assert M.resolve("EXP 85319[69.503]", 32, None, 85300)[:3] == (32, 85319, 69.50)  # '%' read as '3'
    # digit lost from exp: must NOT produce a confident wrong answer near a tiny pct
    r = M.resolve("EXP 600[3.38%]", 25, None, 1590, 460)
    assert r is None, r
    # without a level, no brute force
    assert M.resolve("EXP 246751139%", None, None) is None


def test_level_inference_ambiguity():
    # 0.01% of Lv24 = 4 EXP: consistent with many levels -> needs prev/hint
    assert M.infer_level(4, 0.01, None, None) is None
    assert M.infer_level(4, 0.01, 24, None) == 24
    assert M.infer_level(21066, 52.00, None, None) == 24


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("failures:", fails)
    sys.exit(1 if fails else 0)
