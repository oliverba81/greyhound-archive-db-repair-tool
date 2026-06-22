"""Persistente Einstellungen (reine Standardbibliothek).

Bewusst ohne Drittabhaengigkeiten, damit auch der Erststart-Bootstrap
(``bootstrap.py``) diese Funktionen vor der Installation von customtkinter
nutzen kann.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = Path.home() / ".gh_archive_repair_settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
