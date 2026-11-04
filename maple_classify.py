#!/usr/bin/env python3
"""
maple_classify.py - map-name heuristics for the HUD.

Classifies a MapleStory Classic World map as "town", "pq", or "grind":
- town  -> pause session active-time; EXP still read (map changes still matter)
- pq    -> keep active time; track expected EXP/time where known
- grind -> keep active time (default for unknown names that don't look like a town)

Only exact-ish name patterns are used, so a chat/UI OCR misread is unlikely to flip
the session into "town" state. Unknown names stay in whatever state we were in
(the caller preserves the current classification).
"""
from __future__ import annotations

import re

# Exact MSW town / city names that are the whole map label.
TOWN_EXACT = {
    "Amherst", "Southperry", "Mushroom Town",
    "Henesys", "Kerning City", "Perion", "Ellinia", "Lith Harbor", "Sleepywood",
    "Orbis", "El Nath", "Ludibrium", "Aqua Road", "Nautilus Harbor",
    "Florina Beach",
}

# Substrings that reliably mark a town / amenity map (case-insensitive).
TOWN_SUBSTRINGS = (
    "Townstreet",
    "Department Store", "Weapon Store", "Armor Store", "Repair Shop",
    "Hair Salon", "Skin-Care", "Plastic Surgery", "Pharmacy",
    "Town Hall", "Civic Center",
    "Market", "Free Market", "Cash Shop",
    "Ticketing Booth", "Hotel",
    # "Station" often marks a transport hub (Ellinia Station). "Park" is iffy
    # (Orbis Park has monsters) so it is NOT included here.
    "Station",
)

# Known PQ map-name substrings. Keep conservative: if a PQ instance uses a
# different label in Classic World, add it here. Unknown -> "grind", which is
# still "active time", so this only affects PQ-specific display, not pausing.
PQ_SUBSTRINGS = (
    "Party Quest",
    "Waiting Room",       # "<Waiting Room>" is used by several Classic PQs
    "Kerning PQ",
    "Ludibrium PQ",
    "Henesys PQ",
    "Orbis PQ",
    "Ariant PQ",
)

# Expected PQ duration + reward hints (very rough; Classic MSW values vary by
# patch and group). The HUD uses these only to show progress like
# "KPQ ~15m expected · 6:42 elapsed". None means "no estimate".
PQ_INFO = {
    "Kerning PQ":    {"expected_min": 15, "exp_hint": None, "levels": (21, 30)},
    "Ludibrium PQ":  {"expected_min": 15, "exp_hint": None, "levels": (35, 50)},
    "Henesys PQ":    {"expected_min": 10, "exp_hint": None, "levels": (10, 20)},
    "Orbis PQ":      {"expected_min": 20, "exp_hint": None, "levels": (51, 70)},
    "Ariant PQ":     {"expected_min": 10, "exp_hint": None, "levels": (20, 30)},
}


_WS = re.compile(r"\s+")


def _norm(name: str) -> str:
    return _WS.sub(" ", (name or "").strip()).lower()


def classify(name: str) -> str:
    """Return "town", "pq", "grind" or "unknown" for a canonical map name."""
    if not name:
        return "unknown"
    n = _norm(name)
    if n in {_norm(t) for t in TOWN_EXACT}:
        return "town"
    # Check PQ before town so e.g. an unlikely "PQ Lobby in Kerning City" wins.
    for sub in PQ_SUBSTRINGS:
        if _norm(sub) in n:
            return "pq"
    for sub in TOWN_SUBSTRINGS:
        if _norm(sub) in n:
            return "town"
    # The second half of the label after ":" is where meowdb puts the actual
    # map ("Kerning City Subway: Transfer Area"); so just the city prefix is not
    # a town. The checks above already handled real town labels.
    return "grind"


def pq_kind(name: str) -> str | None:
    """Return the PQ key from PQ_INFO if the name clearly names that PQ."""
    n = _norm(name)
    for key in PQ_INFO:
        if _norm(key) in n:
            return key
    return None


def pq_info(name: str) -> dict | None:
    k = pq_kind(name)
    return PQ_INFO.get(k) if k else None
