"""Auto-Update ueber GitHub Releases (analog zum Website-Scraper-Projekt).

Ablauf:
* Beim Start prueft die GUI im Hintergrund ``releases/latest``.
* Ist eine neuere Version verfuegbar, wird sie angeboten, heruntergeladen
  (Asset ``gh_repair.zip``), ueber das Paket entpackt und das Tool neu
  gestartet.
* Der Changelog-Tab zeigt die letzten Releases.

Dependency-frei: nutzt ausschliesslich die Standardbibliothek (urllib). Beim
Download eines privaten Release-Assets folgt GitHub einem Redirect auf einen
anderen Host (codeload/S3); der Authorization-Header wird dabei – wie es auch
``requests`` tut – entfernt, sonst antwortet der Speicher mit 400.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import __version__

# --- Konfiguration --------------------------------------------------------
GITHUB_REPO = "oliverba81/greyhound-archive-db-repair-tool"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}"

# Name des Release-Assets (ZIP mit dem gh_repair-Paket).
ASSET_NAME = "gh_repair.zip"

# Wurzelverzeichnis der Installation (enthaelt den Ordner gh_repair/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent


def load_token() -> str:
    """Optionaler read-only PAT aus _token.py (neben gh_repair/, gitignored)."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from _token import GITHUB_UPDATE_TOKEN  # type: ignore

        return GITHUB_UPDATE_TOKEN or ""
    except Exception:  # noqa: BLE001
        return ""


def version_tuple(v: str) -> tuple:
    """'1.2.3' -> (1, 2, 3) fuer korrekten Versionsvergleich."""
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _headers(token: str) -> dict:
    h = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"gh-archive-repair-tool/{__version__}",
    }
    if token:
        h["Authorization"] = f"token {token}"
    return h


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Entfernt den Authorization-Header bei Host-Wechsel (wie requests)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            try:
                old_host = urlparse(req.full_url).hostname
                new_host = urlparse(newurl).hostname
            except Exception:  # noqa: BLE001
                old_host = new_host = None
            if old_host != new_host:
                new.headers.pop("Authorization", None)
        return new


def check_for_update(token: str = "") -> tuple[str, str] | None:
    """Prueft auf eine neuere Version. Gibt (version, asset_url) oder None."""
    req = urllib.request.Request(
        f"{GITHUB_API_BASE}/releases/latest", headers=_headers(token)
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    latest = data.get("tag_name", "").lstrip("v")
    if version_tuple(latest) > version_tuple(__version__):
        asset_url = next(
            (a["browser_download_url"] for a in data.get("assets", [])
             if a["name"] == ASSET_NAME),
            None,
        )
        if asset_url:
            return latest, asset_url
    return None


def download_update(token: str, asset_url: str) -> bytes:
    """Laedt das Release-Asset (ZIP). urllib mit Auth-Strip beim Redirect."""
    opener = urllib.request.build_opener(_StripAuthOnRedirect())
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": f"gh-archive-repair-tool/{__version__}",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(asset_url, headers=headers)
    with opener.open(req, timeout=60) as resp:
        return resp.read()


def fetch_releases(token: str = "", count: int = 10) -> list[dict]:
    """Letzte ``count`` Releases fuer den Changelog."""
    req = urllib.request.Request(
        f"{GITHUB_API_BASE}/releases?per_page={count}", headers=_headers(token)
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def apply_update(zip_bytes: bytes) -> None:
    """Entpackt das ZIP ueber das Paket und startet das Tool neu.

    Validiert das ZIP zunaechst (muss das gh_repair-Paket enthalten), schreibt
    es in eine Temp-Datei und startet einen losgeloesten Helfer, der nach
    kurzer Wartezeit entpackt, das Tool neu startet und sich selbst aufraeumt.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
    if not any(n.startswith("gh_repair/") and n.endswith(".py") for n in names):
        raise ValueError(
            "Ungueltiges Update-Asset – enthaelt kein gh_repair-Paket."
        )

    zip_path = PROJECT_ROOT / "_gh_repair_update.zip"
    zip_path.write_bytes(zip_bytes)
    helper = PROJECT_ROOT / "_gh_repair_updater.py"
    helper.write_text(
        "import time, sys, subprocess, zipfile\n"
        "from pathlib import Path\n"
        "time.sleep(2)\n"
        f"root = Path({repr(str(PROJECT_ROOT))})\n"
        f"zp = Path({repr(str(zip_path))})\n"
        "with zipfile.ZipFile(zp) as z:\n"
        "    z.extractall(root)\n"
        "subprocess.Popen([sys.executable, '-m', 'gh_repair'], cwd=str(root))\n"
        "zp.unlink(missing_ok=True)\n"
        f"Path({repr(str(helper))}).unlink(missing_ok=True)\n",
        encoding="utf-8",
    )
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interp = str(pythonw) if pythonw.exists() else sys.executable
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([interp, str(helper)], creationflags=flags, cwd=str(PROJECT_ROOT))
