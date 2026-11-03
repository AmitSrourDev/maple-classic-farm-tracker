#!/usr/bin/env python3
"""
meowdb.py - pull data from meowdb.com for the Maple HUD.

Public (no login):
  python meowdb.py price Meba              market summary (24h/7d median, cheapest, buy offer)
  python meowdb.py mob Stirge              monster stats from the search API + monster page
  python meowdb.py search "Claw Attack"    raw search hits (items, monsters, maps, npcs)
Logged in (your own account data):
  python meowdb.py login                   open a WebView2 window to meowdb, sign in with
                                           Google in-app, save the session cookie, exit
  python meowdb.py verify                  probe /api/me/export with the saved cookie
  python meowdb.py export                  GET /msclassic/api/me/export -> meowdb-export.json,
                                           then refreshes build.json (python maple_build.py --from-export)

Login: meowdb signs you in with Google OAuth, which a script cannot do. The in-app Sign-in
button opens an embedded Chromium window (WebView2) pointed at meowdb; when you finish the
Google flow, the window closes on its own and the session cookie lands in meowdb_cookie.txt.
As a fallback you can still paste the whole "cookie:" value from DevTools. Sessions expire;
when sync says 401/403, sign in again. No password is stored or used anywhere.
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


def _http_detail(e: urllib.error.HTTPError) -> str:
    if e.code in (401, 403):
        return f"not signed in (HTTP {e.code}) - click Sign in to meowdb"
    if 500 <= e.code < 600:
        return f"meowdb server error (HTTP {e.code}) - try later"
    return f"HTTP {e.code}"


def _load_cookie(cookie: str | None) -> str:
    if cookie is None:
        if not COOKIE_PATH.exists():
            raise RuntimeError("no meowdb cookie saved - click Sign in, or paste one")
        cookie = COOKIE_PATH.read_text(encoding="utf-8").strip()
    if not cookie:
        raise RuntimeError("meowdb cookie is empty - click Sign in, or paste one")
    return cookie


def verify(cookie: str | None = None) -> tuple[bool, str]:
    """Probe /api/me/export with the cookie. Returns (ok, detail)."""
    try:
        cookie = _load_cookie(cookie)
    except RuntimeError as e:
        return False, str(e)
    try:
        body = _get("/msclassic/api/me/export", cookie=cookie, timeout=10)
    except urllib.error.HTTPError as e:
        return False, _http_detail(e)
    except Exception as e:
        return False, f"network: {e}"
    try:
        d = json.loads(body)
    except Exception:
        return True, "signed in (non-JSON response)"
    name = ((d.get("user") or {}).get("name") or (d.get("user") or {}).get("username")
            or d.get("username") or d.get("name") or "signed in")
    return True, f"signed in as {name}"


def login_via_webview(timeout_s: int = 600) -> str:
    """Spawn a subprocess that opens a WebView2 window to meowdb, waits for the user to
    finish Google OAuth, captures the session cookie, writes it to COOKIE_PATH. Returns
    verify()'s detail string. Raises on timeout, cancellation or verify failure."""
    import os
    import subprocess
    import tempfile
    out = Path(tempfile.gettempdir()) / f"meowdb_login_{os.getpid()}.txt"
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--meowdb-login", str(out)]
        else:
            cmd = [sys.executable, str(HERE / "webview_login.py"), str(out)]
        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        try:
            p = subprocess.run(cmd, timeout=timeout_s + 30, creationflags=creationflags)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("login timed out") from e
        if p.returncode != 0 or not out.exists() or not out.read_text(encoding="utf-8").strip():
            raise RuntimeError("login window closed before signing in")
        cookie = out.read_text(encoding="utf-8").strip()
        COOKIE_PATH.write_text(cookie, encoding="utf-8")
        ok, detail = verify(cookie)
        if not ok:
            raise RuntimeError(f"cookie saved but verify failed: {detail}")
        return detail
    finally:
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass


def export(cookie: str | None = None):
    """Pull the account export; returns (summary text, build dict or None). Raises on failure."""
    cookie = _load_cookie(cookie)
    try:
        body = _get("/msclassic/api/me/export", cookie=cookie)
    except urllib.error.HTTPError as e:
        raise RuntimeError(_http_detail(e)) from e
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
    if len(sys.argv) < 2 or sys.argv[1] not in ("price", "mob", "search", "export", "verify", "login"):
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
    elif cmd == "verify":
        ok, detail = verify()
        print(detail)
        sys.exit(0 if ok else 1)
    elif cmd == "login":
        try:
            print(login_via_webview())
        except RuntimeError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
