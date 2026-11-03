#!/usr/bin/env python3
"""
maple_build.py - damage / hit-chance / casts-per-kill model for a Classic World
thief (Lucky Seven), and what it costs in MP potions per kill.

Inputs
  build.json          your stats (created from a meowdb export with --from-export,
                      or edited by hand). Gear totals come from the meowdb
                      builder page because the export only carries AP + item ids.
  embedded MOBS       HP / EXP / avoid / DEF / level per mob (meowdb, 2026-11-03)
  embedded L7 table   MP cost and damage % per Lucky Seven level (meowdb)

Usage
  python maple_build.py                       table for every known mob
  python maple_build.py --mob Stirge          one mob, with "what to one-shot" advice
  python maple_build.py --map "Transfer Area" mobs on that map
  python maple_build.py --exp-per-hour 36000  adds kills/h, MP/h, pot cost/h at that rate
  python maple_build.py --from-export meowdb-export.json   refresh build.json AP/level/skills
  python maple_build.py --watk 40 --luk 150   what-if overrides

Formulas (meowdb /formulas, /formulas/damage - see the tracker doc):
  per star  MIN = S*WATK*(0.8 + (1.5*LUK + STR+DEX)/100)   MAX = S*WATK*(1.0 + (3*LUK + STR+DEX)/100)
  S = L7 damage% / 100; two stars per cast; crit (rate c, bonus d) multiplies by 1+d on a crit
  defended = raw * 100 / (DEF + 100);  level gap scale 1/(1+gap^2*0.005) for gap 1-9, 1/(1+gap*0.05) for 10+
  hit: A = ACC*100/(10*L+255), L = max(0, mobLv - lv); f = 0.15 + 0.20/(1+exp((A-avoid)/12));
       hit if uniform(1-f, 1+f)*A >= avoid.  Thief ACC = floor((1.2*DEX + 2*Lv + 0.6*LUK)/4 + 15) + gear
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

from app_paths import data, resource

HERE = Path(__file__).resolve().parent
BUILD_PATH = data("build.json")

# meowdb skill table (thief/lucky-seven): level -> (MP, damage %); always 2 stars
L7 = {1: (8, 60), 2: (8, 64), 3: (8, 68), 4: (8, 72), 5: (9, 76), 6: (9, 80), 7: (9, 84), 8: (10, 88),
      9: (10, 92), 10: (11, 96), 11: (11, 100), 12: (12, 104), 13: (12, 108), 14: (13, 112), 15: (13, 116),
      16: (14, 120), 17: (14, 124), 18: (15, 128), 19: (15, 132), 20: (16, 140)}

# meowdb monster pages, fetched 2026-11-03. maps: {map name: spawns}
MOBS = {
    "Stirge": {"lv": 20, "hp": 296, "exp": 37, "avoid": 13, "def": 0,
               "maps": {"Transfer Area": 38, "Dangerous Steam": 38, "Dark Cave": 35, "Line 1 <Area 2>": 21, "Line 1 <Area 3>": 18}},
    "Jr. Necki": {"lv": 21, "hp": 272, "exp": 39, "avoid": 14, "def": 0, "maps": {}},
    "Horny Mushroom": {"lv": 22, "hp": 403, "exp": 41, "avoid": 13, "def": 10,
                       "maps": {"Ant Tunnel IV": 41, "Ant Tunnel III": 38, "The Swamp of Despair II": 20, "Deep Ant Tunnel I": 16,
                                "Ant Tunnel I": 15, "Ant Tunnel II": 15, "Deep Ant Tunnel II": 9, "Ant Tunnel Park": 4}},
    "Zombie Mushroom": {"lv": 24, "hp": 436, "exp": 45, "avoid": 14, "def": 15,
                        "maps": {"Ant Tunnel III": 61, "Ant Tunnel II": 32, "Deep Ant Tunnel I": 29, "Ant Tunnel IV": 23,
                                 "Ant Tunnel Park": 23, "The Grave of Mushmom": 13, "Ant Tunnel I": 11, "Deep Ant Tunnel II": 9}},
    "Evil Eye": {"lv": 27, "hp": 606, "exp": 50, "avoid": 16, "def": 10,
                 "maps": {"The Cave of Evil Eye III": 38, "The Cave of Evil Eye II": 36, "Ant Tunnel Park": 31,
                          "The Cave of Evil Eye IV": 23, "Deep Ant Tunnel I": 21, "The Cave of Evil Eye I": 19, "Deep Ant Tunnel II": 16}},
    "Ligator": {"lv": 31, "hp": 853, "exp": 58, "avoid": 21, "def": 10,
                "maps": {"The Swamp of Despair I": 41, "The Swamp of Despair II": 36, "The Swamp of Despair III": 28,
                         "The Murky Swamp I": 17, "Dangerous Croko I": 14, "The Murky Swamp II": 14}},
    "Curse Eye": {"lv": 33, "hp": 1001, "exp": 63, "avoid": 22, "def": 30,
                  "maps": {"Sleepy Dungeon V": 23, "Tree Dungeon, Forest Up North IX": 20, "Hunting Ground in the Deep Forest II": 20,
                           "Sleepy Dungeon III": 19, "Tree Dungeon, Forest Up North VIII": 17, "Sleepy Dungeon IV": 14}},
    "Jr. Wraith": {"lv": 34, "hp": 982, "exp": 66, "avoid": 25, "def": 30,
                   "maps": {"Line 2 <Area 1>": 83, "Line 1 <Area 2>": 69, "Line 2 <Area 2>": 32, "Line 1 <Area 3>": 31, "B2 <Subway Depot>": 12}},
    "Lupin": {"lv": 37, "hp": 1336, "exp": 75, "avoid": 27, "def": 0,
              "maps": {"Monkey Swamp I": 28, "A Look-Out Shed Around the Beach": 27, "Sleepy Dungeon V": 23,
                       "Hunting Ground in the Deep Forest II": 15, "Monkey Forest I": 14, "The Burnt Land IV": 14}},
    "Cold Eye": {"lv": 40, "hp": 1894, "exp": 85, "avoid": 25, "def": 0,
                 "maps": {"The Tunnel That Lost Light I": 32, "The Tunnel That Lost Light II": 25, "Another Path": 23,
                          "Drake's Meal Table": 22, "Wild Kargo's Area": 17, "Sanctuary Entrance II": 15}},
    "Zombie Lupin": {"lv": 41, "hp": 1774, "exp": 89, "avoid": 29, "def": 0,
                     "maps": {"Monkey Swamp III": 31, "Tree Dungeon, Monkey Forest I": 26, "Tree Dungeon, Monkey Forest II": 26,
                              "Monkey Forest II": 25, "The Forest of Evil II": 16, "Monkey Swamp II": 13}},
    "Wraith": {"lv": 48, "hp": 2350, "exp": 122, "avoid": 32, "def": 30,
               "maps": {"Line 2 <Area 3>": 68, "Line 1 <Area 4>": 59, "Line 2 <Area 2>": 57, "Line 1 <Area 3>": 48, "B3 <Subway Depot>": 10}},
}

# MP potions: restores, NPC price (meowdb; Lemon's page was unreachable -> value from memory, edit if wrong)
MP_POTS = {"Lemon": {"mp": 50, "price": 150}, "Blue Potion": {"mp": 100, "price": 220}}

DEFAULT_BUILD = {
    "note": "stats WITH gear. From meowdb build RasAQRU5Md (2026-11-03): AP 4/28/4/134 + gear STR+2 DEX+2 LUK+4, Meba 19 + Wolbi 17.",
    "level": 30, "str": 6, "dex": 30, "luk": 138, "watk": 36, "l7_level": 20,
    "acc_gear": 1, "acc_potion": 0, "crit_rate": 0.06, "crit_dmg": 0.21,
    "mp_pot": "Lemon",
}

# item ids from the export -> what they add (only what matters here)
GEAR_BONUS = {688: {"watk": 19, "name": "Meba"}, 295: {"watk": 17, "name": "Wolbi"},
              682: {"watk": 13, "name": "Steel Titans"}, 686: {"watk": 17, "name": "Steel Igor"},
              294: {"watk": 15, "name": "Subi"}, 296: {"watk": 19, "name": "Mokbi"}, 297: {"watk": 21, "name": "Kumbi"},
              1435: {"crit_rate": 0.01, "name": "Work Gloves"}, 889: {"acc": 1, "name": "Old Raggedy Cape"}}


def load_build(path=None):
    path = Path(path) if path else BUILD_PATH
    b = dict(DEFAULT_BUILD)
    default_file = resource("build.default.json")
    if default_file.exists():
        try:
            b.update(json.loads(default_file.read_text(encoding="utf-8")))
        except Exception:
            pass
    if path.exists():
        b.update(json.loads(path.read_text(encoding="utf-8")))
    return b


def build_from_export(export_path, path=None):
    path = Path(path) if path else BUILD_PATH
    d = json.loads(Path(export_path).read_text(encoding="utf-8"))
    builds = d.get("characterBuilder", {}).get("builds", [])
    if not builds:
        sys.exit("no builds in the export")
    bl = builds[0]
    ap = json.loads(bl["apJson"]) if isinstance(bl["apJson"], str) else bl["apJson"]
    eq = json.loads(bl["equipmentJson"]) if isinstance(bl["equipmentJson"], str) else bl["equipmentJson"]
    sk = json.loads(bl["skillsJson"]) if isinstance(bl["skillsJson"], str) else bl["skillsJson"]
    b = load_build(path)
    # keep the gear stat bonuses from build.json (= stats with gear minus previous AP); default +2/+2/+4
    prev_ap = b.get("_ap", {"str": 4, "dex": 28, "luk": 134})
    bonus = {k: b[k] - prev_ap.get(k, 0) for k in ("str", "dex", "luk")}
    b.update(level=int(bl["level"]), _ap=ap,
             str=int(ap["str"]) + bonus["str"], dex=int(ap["dex"]) + bonus["dex"], luk=int(ap["luk"]) + bonus["luk"],
             l7_level=int(sk.get("4001003", b["l7_level"])))
    watk = sum(GEAR_BONUS.get(int(i), {}).get("watk", 0) for i in eq.values())
    if watk:
        b["watk"] = watk
    b["acc_gear"] = sum(GEAR_BONUS.get(int(i), {}).get("acc", 0) for i in eq.values())
    b["crit_rate"] = round(0.05 + sum(GEAR_BONUS.get(int(i), {}).get("crit_rate", 0) for i in eq.values()), 3)
    b["gear_names"] = [GEAR_BONUS.get(int(i), {}).get("name", str(i)) for i in eq.values()]
    b["build_name"] = bl.get("name")
    Path(path).write_text(json.dumps(b, indent=2), encoding="utf-8")
    return b


# --------------------------------------------------------------------------
def thief_acc(b):
    return math.floor((1.2 * b["dex"] + 2 * b["level"] + 0.6 * b["luk"]) / 4 + 15) + b.get("acc_gear", 0) + b.get("acc_potion", 0)


def hit_chance(acc, lv, mob):
    L = max(0, mob["lv"] - lv)
    A = acc * 100.0 / (10 * L + 255)
    D = A - mob["avoid"]
    f = 0.15 + 0.20 / (1 + math.exp(D / 12))
    lo, hi = A * (1 - f), A * (1 + f)
    if lo >= mob["avoid"]:
        return 1.0
    if hi <= mob["avoid"]:
        return 0.0
    return (hi - mob["avoid"]) / (hi - lo)


def star_range(b, mob):
    mp, pct = L7[int(b["l7_level"])]
    S = pct / 100.0
    sec = b["str"] + b["dex"]
    mn = S * b["watk"] * (0.8 + (1.5 * b["luk"] + sec) / 100.0)
    mx = S * b["watk"] * (1.0 + (3.0 * b["luk"] + sec) / 100.0)
    gap = mob["lv"] - b["level"]
    scale = 1.0 if gap <= 0 else (1 / (1 + gap * gap * 0.005) if gap < 10 else 1 / (1 + gap * 0.05))
    dscale = 100.0 / (mob["def"] + 100.0)
    return mn * scale * dscale, mx * scale * dscale, mp


def kill_model(b, mob, trials=20000, seed=7):
    """Monte Carlo: casts to kill, P(1 cast), MP per kill."""
    mn, mx, mp = star_range(b, mob)
    p = hit_chance(thief_acc(b), b["level"], mob)
    crit_r, crit_d = b.get("crit_rate", 0.06), b.get("crit_dmg", 0.21)
    rnd = random.Random(seed)
    hp = mob["hp"]
    tot = 0
    one = two = 0
    for _ in range(trials):
        left = hp
        casts = 0
        while left > 0 and casts < 50:
            casts += 1
            for _s in range(2):
                if rnd.random() < p:
                    d = rnd.uniform(mn, mx)
                    if rnd.random() < crit_r:
                        d *= 1 + crit_d
                    left -= d
        tot += casts
        one += casts == 1
        two += casts <= 2
    casts = tot / trials
    return {"mob": mob, "min": mn, "max": mx, "hit": p, "casts": casts, "p1": one / trials, "p2": two / trials,
            "mp_per_kill": casts * mp, "mp_per_cast": mp,
            "watk_to_oneshot": watk_to_oneshot(b, mob)}


def watk_to_oneshot(b, mob):
    """TotalWATK at which two MIN stars kill (100% one-cast when both hit)."""
    mp, pct = L7[int(b["l7_level"])]
    S = pct / 100.0
    sec = b["str"] + b["dex"]
    gap = mob["lv"] - b["level"]
    scale = 1.0 if gap <= 0 else (1 / (1 + gap * gap * 0.005) if gap < 10 else 1 / (1 + gap * 0.05))
    dscale = 100.0 / (mob["def"] + 100.0)
    per_watk_min = S * (0.8 + (1.5 * b["luk"] + sec) / 100.0) * scale * dscale
    return math.ceil(mob["hp"] / (2 * per_watk_min))


def mobs_on_map(map_name):
    """[(mob name, spawns)] for a map name (the part after 'Street: ' is fine)."""
    name = map_name.split(": ", 1)[-1].strip().lower()
    out = []
    for m, d in MOBS.items():
        for mp, n in d["maps"].items():
            if mp.lower() == name:
                out.append((m, n))
    out.sort(key=lambda x: -x[1])
    return out


def model_line(b, map_name, exp_per_hour=None, mp_pot=None):
    """One-line summary for the HUD, or None if the map's mobs are unknown."""
    on = mobs_on_map(map_name) if map_name else []
    if not on:
        return None
    mob_name, spawns = on[0]
    r = kill_model(b, MOBS[mob_name], trials=4000)
    pot = MP_POTS.get(mp_pot or b.get("mp_pot", "Lemon"), MP_POTS["Lemon"])
    pots_per_kill = r["mp_per_kill"] / pot["mp"]
    s = (f"{mob_name}: {r['p1'] * 100:.0f}% one-cast, {r['casts']:.2f} casts/kill, {r['mp_per_kill']:.0f} MP/kill "
         f"({pots_per_kill * pot['price']:.0f} mesos in {mp_pot or b.get('mp_pot', 'Lemon')})")
    if r["hit"] < 0.999:
        s += f", hit {r['hit'] * 100:.0f}%"
    if r["p1"] < 0.99:
        s += f"; one-cast needs {r['watk_to_oneshot']} WATK (have {b['watk']})"
    if exp_per_hour:
        kills = exp_per_hour / r["mob"]["exp"]
        s += f"; at {exp_per_hour / 1000:.0f}k EXP/h ≈ {kills:.0f} kills/h solo = {kills * r['mp_per_kill'] / 1000:.1f}k MP/h = {kills * pots_per_kill * pot['price'] / 1000:.1f}k mesos/h"
    return s


def main():
    ap = argparse.ArgumentParser(description="Lucky Seven kill / MP model")
    ap.add_argument("--mob")
    ap.add_argument("--map")
    ap.add_argument("--exp-per-hour", type=float)
    ap.add_argument("--from-export")
    ap.add_argument("--watk", type=float)
    ap.add_argument("--luk", type=float)
    ap.add_argument("--level", type=int)
    ap.add_argument("--acc-potion", type=int, default=None, help="+10 Sniper / +15 Supreme")
    ap.add_argument("--mp-pot", choices=list(MP_POTS))
    args = ap.parse_args()
    if args.from_export:
        b = build_from_export(args.from_export)
        print("build.json updated:", {k: b[k] for k in ("level", "str", "dex", "luk", "watk", "l7_level")})
    b = load_build()
    for k in ("watk", "luk", "level"):
        v = getattr(args, k)
        if v is not None:
            b[k] = v
    if args.acc_potion is not None:
        b["acc_potion"] = args.acc_potion
    if args.mp_pot:
        b["mp_pot"] = args.mp_pot
    mp, pct = L7[int(b["l7_level"])]
    acc = thief_acc(b)
    print(f"Build: Lv {b['level']}  LUK {b['luk']} DEX {b['dex']} STR {b['str']}  WATK {b['watk']}  L7 {b['l7_level']} "
          f"({pct}% x2, {mp} MP/cast)  ACC {acc} (formula; in-game shows ~+5)  crit {b.get('crit_rate', 0.06) * 100:.0f}%/+{b.get('crit_dmg', 0.21) * 100:.0f}%")
    pot = MP_POTS[b.get("mp_pot", "Lemon")]
    names = [args.mob] if args.mob else ([m for m, _ in mobs_on_map(args.map)] if args.map else list(MOBS))
    if not names:
        sys.exit(f"no known mobs for {args.map!r}; known maps: " + ", ".join(sorted({m for d in MOBS.values() for m in d['maps']})))
    print(f"{'Mob':16s} {'Lv':>3} {'HP':>6} {'EXP':>4} {'hit%':>5} {'star dmg':>11} {'1-cast%':>7} {'casts':>6} {'MP/kill':>7} {'pot mesos/kill':>14} {'WATK to 1-cast':>14}")
    for name in names:
        if name not in MOBS:
            print(f"{name}: unknown mob (add it to MOBS in maple_build.py)")
            continue
        r = kill_model(b, MOBS[name])
        per_kill_mesos = r["mp_per_kill"] / pot["mp"] * pot["price"]
        print(f"{name:16s} {r['mob']['lv']:>3} {r['mob']['hp']:>6} {r['mob']['exp']:>4} {r['hit'] * 100:>5.0f} "
              f"{r['min']:>5.0f}-{r['max']:<5.0f} {r['p1'] * 100:>7.0f} {r['casts']:>6.2f} {r['mp_per_kill']:>7.1f} {per_kill_mesos:>14.1f} {r['watk_to_oneshot']:>14}")
        if args.exp_per_hour:
            kills = args.exp_per_hour / r["mob"]["exp"]
            print(f"{'':16s} at {args.exp_per_hour:,.0f} EXP/h solo: {kills:,.0f} kills/h, {kills * r['mp_per_kill']:,.0f} MP/h = "
                  f"{kills * r['mp_per_kill'] / pot['mp']:,.0f} {b.get('mp_pot', 'Lemon')}/h = {kills * per_kill_mesos:,.0f} mesos/h")


if __name__ == "__main__":
    main()
