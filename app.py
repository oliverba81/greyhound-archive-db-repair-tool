"""Einstiegspunkt fuer die gebaute .exe (PyInstaller).

Startet dieselbe Logik wie ``python -m gh_repair``: ohne Argumente die GUI,
mit Argumenten den Headless-Modus.

Die gebaute .exe prueft als allererste Handlung die Lizenz der GREYHOUND
Support Suite (``greyhound-license.json`` neben der .exe). Der Start aus dem
Quelltext bleibt ungeprueft.
"""

import os
import sys

APP_ID = "gh-archiv-repair-tool"
SUPPORT_CONTACT = "support@greyhound-software.com"

LICENSE = None  # bis zum Programmende erreichbar halten
_ended_text = None


def _license_text(message, lid) -> str:
    return (f"{message}\n\nLizenz-ID: {lid or '–'}\n"
            f"Support: {SUPPORT_CONTACT}")


def _show_license_error(text: str) -> None:
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Lizenz ungültig", text)
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _on_license_ended(message, lid) -> None:
    # Laeuft in einem eigenen Thread: hier nichts an der Oberflaeche tun.
    global _ended_text
    _ended_text = _license_text(message, lid)
    if len(sys.argv) > 1:  # Headless-Modus: kein UI-Thread, der nachsieht
        if sys.stderr is not None:
            print(_ended_text, file=sys.stderr)
        os._exit(1)


def license_ended_text():
    """Fuer den UI-Thread: Meldung, sobald die Lizenz geendet hat, sonst None."""
    return _ended_text


def main() -> int:
    global LICENSE
    if getattr(sys, "frozen", False):
        import greyhound_license

        LICENSE = greyhound_license.check(APP_ID)
        if not LICENSE.is_valid:
            _show_license_error(_license_text(LICENSE.message, LICENSE.lid))
            return 1
        LICENSE.on_ended(_on_license_ended)

    from gh_repair.__main__ import main as app_main

    rc = app_main(license_ended=license_ended_text if LICENSE else None)
    if LICENSE is not None and LICENSE.is_ended:
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
