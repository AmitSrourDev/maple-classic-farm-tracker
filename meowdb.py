#!/usr/bin/env python3
"""
meowdb.py - pull data from meowdb.com for the Maple HUD.

Public (no login):
  python meowdb.py price Meba              market summary (24h/7d median, cheapest, buy offer)
  python meowdb.py mob Stirge              monster stats from the search API + monster page
  python meowdb.py search "Claw Attack"    raw search hits (items, monsters, maps, npcs)
Logged in (your own account data):
  python meowdb.py export                  GET /msclassic/api/me/export -> meowdb-export.json,
                                           then refreshes build.json (python maple_build.py --from-export)

Login: meowdb signs you in with Google OAuth, which a script cannot do. Paste your
browser session cookie ONCE into meowdb_cookie.txt next to this file:
  Chrome: F12 > Network > reload meowdb.com > click any request > Request Headers > copy
  the whole "cookie:" value. Keep that file private (it is your login). Sessions expire;
  if export says 401/403, paste a fresh cookie.
No password is stored or used anywhere.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app_paths import data

HERE = Path(__file__).resolve().parent
COOKIE_PATH = data("meowdb_cookie.txt")
EXPORT_PATH = data("meowdb-export.json")
BASE = "https://meowdb.com"
UA = "maple-hud/1.0 (+personal tracker)"


def _get(path, cookie=None, timeout=20):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def search(q):
    return json.loads(_get("/msclassic/api/search?q=" + urllib.parse.quote(q) + "&locale=en"))


def price(name):
    hits = search(name).get("items", [])
    if not hits:
        sys.exit(f"no item named {name!r}")
    item = hits[0]
    data = json.loads(_get(f"/msclassic/api/item-market-summary?itemId={item['id']}&condition=clean"))
    print(json.dumps({"item": item["name"], "id": item["id"], **data}, indent=2))


def mob(name):
    hits = search(name).get("monsters", [])
    if not hits:
        sys.exit(f"no monster named {name!r}")
    m = hits[0]
    html = _get(f"/msclassic/monsters/{m['id']}")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    out = {"name": m["name"], "id": m["id"], "url": f"{BASE}/msclassic/monsters/{m['id']}"}
    for key, pat in (("lv", r"\bLv\.?\s*(\d+)"), ("hp", r"\bHP\s*:?\s*([\d,]+)"), ("exp", r"\bEXP\s*:?\s*([\d,]+)"),
                     ("avoid", r"\bAvoid\s*:?\s*(\d+)"), ("acc", r"\bAccuracy\s*:?\s*(\d+)"),
                     ("def", r"\b(?:P\.DEF|W\.?DEF|Weapon Defense)\s*:?\s*(\d+)")):
        mm = re.search(pat, text, re.I)
        out[key] = int(mm.group(1).replace(",", "")) if mm else None
    print(json.dumps(out, indent=2))
    if out["hp"] is None:
        print("could not parse the page; open the url above and add the mob to MOBS in maple_build.py by hand")


def export(cookie: str | None = None):
    """Pull the account export; returns (summary text, build dict or None). Raises on failure."""
    if cookie is None:
        if not COOKIE_PATH.exists():
            raise RuntimeError("no meowdb cookie saved (Settings > meowdb, or meowdb_cookie.txt)")
        cookie = COOKIE_PATH.read_text(encoding="utf-8").strip()
    try:
        body = _get("/msclassic/api/me/export", cookie=cookie)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"export failed: HTTP {e.code} - cookie expired? paste a fresh one") from e
    EXPORT_PATH.write_text(body, encoding="utf-8")
    d = json.loads(body)
    builds = d.get("characterBuilder", {}).get("builds", [])
    summary = f"saved {EXPORT_PATH.name} ({len(body):,} bytes), {len(builds)} build(s)"
    b = None
    if builds:
        import maple_build
        b = maple_build.build_from_export(EXPORT_PATH)
        summary += f"; build.json: Lv {b['level']} LUK {b['luk']} DEX {b['dex']} WATK {b['watk']} L7 {b['l7_level']}"
    return summary, b


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("price", "mob", "search", "export"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    arg = " ".join(sys.argv[2:])
    if cmd == "price":
        price(arg)
    elif cmd == "mob":
        mob(arg)
    elif cmd == "search":
        print(json.dumps(search(arg), indent=2)[:6000])
    elif cmd == "export":
        try:
            print(export()[0])
        except RuntimeError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
