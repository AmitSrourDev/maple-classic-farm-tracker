#!/usr/bin/env python3
"""
webview_login.py - open an embedded Chromium (Edge WebView2) window pointed at
meowdb.com so the user can finish Google OAuth inside the app. Poll the window's
cookie jar; once a meowdb_session cookie appears AND /msclassic/api/me/export
returns 200 with it, write the full Cookie header to argv[1] and exit 0.

Called from maple_hud --meowdb-login <path> (the overlay's main() routes to here
before any heavy imports). Requires pywebview; on Windows it uses the Edge
WebView2 Runtime (shipped by default on Win11 / modern Win10).
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOGIN_URL = "https://meowdb.com"
PROBE_URL = "https://meowdb.com/msclassic/api/me/export"
TIMEOUT_S = 600
POLL_S = 1.5
UA = "maple-hud-login/1.0"


def _signed_in(cookie_header: str) -> bool:
    req = urllib.request.Request(PROBE_URL, headers={"User-Agent": UA, "Cookie": cookie_header})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _collect(window) -> str:
    jars = window.get_cookies() or []
    pairs: dict[str, str] = {}
    for jar in jars:
        for c in jar:
            dom = (c.domain or "").lstrip(".")
            if dom == "meowdb.com" or dom.endswith(".meowdb.com"):
                pairs[c.name] = c.value
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def run(out_path: str) -> int:
    try:
        import webview  # pywebview
    except ImportError:
        print("pywebview not installed; run: pip install pywebview", file=sys.stderr)
        return 2

    result: dict[str, object] = {"cookie": None}

    def worker(window):
        start = time.monotonic()
        while time.monotonic() - start < TIMEOUT_S:
            try:
                header = _collect(window)
                if header and "meowdb_session" in header and _signed_in(header):
                    result["cookie"] = header
                    try:
                        window.destroy()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            time.sleep(POLL_S)

    window = webview.create_window("Sign in to meowdb", LOGIN_URL, width=960, height=720)
    kwargs = {"gui": "edgechromium"} if sys.platform == "win32" else {}
    webview.start(worker, window, **kwargs)

    if not result["cookie"]:
        print("login window closed before signing in", file=sys.stderr)
        return 1
    Path(out_path).write_text(str(result["cookie"]), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: webview_login.py <output-path>", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(sys.argv[1]))
