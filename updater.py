"""
updater.py - self-update from GitHub Releases.

Checks https://api.github.com/repos/<repo>/releases/latest, compares the tag
(vX.Y.Z) with version.__version__, downloads the installer asset
(MapleFarmTracker-Setup-X.Y.Z.exe), verifies its SHA-256 against the
SHA256SUMS.txt asset of the same release, and runs the installer silently
(Inno Setup: /VERYSILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS), which
closes the running app, installs over it and restarts it.

Only does anything when the app is the installed build (FROZEN). From source,
`python updater.py` just prints what it would do.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from version import REPO, __version__

UA = f"MapleFarmTracker/{__version__} (+https://github.com/{REPO})"
INSTALLER_RE = re.compile(r"^MapleFarmTracker-Setup-.*\.exe$", re.I)


def parse_version(tag: str):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _get(url, timeout=20, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json" if not binary else "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")


def check_latest(repo: str = REPO):
    """-> dict(tag, version, newer, installer_url, sums_url, notes, html_url) or None on error."""
    try:
        rel = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest"))
    except Exception:
        return None
    tag = rel.get("tag_name", "")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    installer = next((u for n, u in assets.items() if INSTALLER_RE.match(n)), None)
    return {
        "tag": tag, "version": ".".join(map(str, parse_version(tag))),
        "newer": parse_version(tag) > parse_version(__version__),
        "installer_url": installer, "sums_url": assets.get("SHA256SUMS.txt"),
        "zip_url": next((u for n, u in assets.items() if n.lower().endswith(".zip")), None),
        "notes": rel.get("body", ""), "html_url": rel.get("html_url", f"https://github.com/{repo}/releases"),
    }


def _expected_sha(sums_text: str, filename: str):
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def download_installer(info, progress=None) -> Path:
    """Download + verify; returns the installer path. Raises on any mismatch."""
    url = info["installer_url"]
    if not url:
        raise RuntimeError("release has no installer asset")
    name = url.rsplit("/", 1)[-1]
    dest = Path(tempfile.gettempdir()) / name
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if info.get("sums_url"):
        expected = _expected_sha(_get(info["sums_url"]), name)
        if not expected:
            raise RuntimeError("installer not listed in SHA256SUMS.txt")
        actual = hashlib.sha256(dest.read_bytes()).hexdigest()
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for {name}")
    return dest


def run_installer(path: Path, silent=True):
    """Start the installer; it closes this app (CloseApplications) and restarts it after."""
    args = [str(path)]
    if silent:
        args += ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    if sys.platform == "win32":
        subprocess.Popen(args, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=True)
    else:
        subprocess.Popen(args)


def main():
    info = check_latest()
    print(f"installed {__version__}; latest: {info}")
    if info and info["newer"] and "--install" in sys.argv:
        p = download_installer(info, lambda d, t: print(f"\r{d / 1e6:.1f} / {t / 1e6:.1f} MB", end=""))
        print("\nverified", p)
        if os.name == "nt":
            run_installer(p)


if __name__ == "__main__":
    main()
