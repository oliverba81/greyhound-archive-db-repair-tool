"""Erststart-Bootstrap: installiert fehlende Abhaengigkeiten automatisch.

Analog zum Website-Scraper-Projekt. Wird VOR dem Import der GUI aufgerufen,
damit ``customtkinter`` beim ersten Start automatisch nachinstalliert werden
kann. Nutzt nur die Standardbibliothek.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

from . import __version__
from .settings import load_settings, save_settings

# Modulname -> pip-Paketname
REQUIRED_PACKAGES = {
    "customtkinter": "customtkinter",
}


def _check_missing() -> list[str]:
    missing = []
    for mod_name, pkg_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(mod_name)
        except ImportError:
            missing.append(pkg_name)
    return missing


def _refresh_sys_path() -> None:
    """Fuegt user-/site-packages zu sys.path hinzu (nach pip install noetig)."""
    import site

    importlib.invalidate_caches()
    for getter in (lambda: site.getusersitepackages(),
                   lambda: __import__("sysconfig").get_paths().get("purelib", "")):
        try:
            path = getter()
            if path and path not in sys.path:
                sys.path.insert(0, path)
        except Exception:  # noqa: BLE001
            pass


def _mark_setup_done() -> None:
    s = load_settings()
    s["setup_done"] = True
    s["setup_version"] = __version__
    save_settings(s)


def ensure_dependencies() -> None:
    """Prueft und installiert fehlende Pakete beim ersten Start."""
    settings = load_settings()
    if settings.get("setup_done") and settings.get("setup_version") == __version__:
        _refresh_sys_path()
        if not _check_missing():
            return

    missing = _check_missing()
    if not missing:
        _mark_setup_done()
        return

    _run_setup_window(missing)


def _run_setup_window(missing: list[str]) -> None:
    """Kleines Setup-Fenster (stdlib tkinter) mit pip-Installation im Thread."""
    import threading
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Erstmaliges Setup – GREYHOUND Archive Repair Tool")
    root.geometry("520x230")
    root.resizable(False, False)
    root.lift()
    root.focus_force()

    frm = ttk.Frame(root, padding=20)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Installiere benötigte Pakete …",
              font=("Segoe UI", 11)).pack(anchor="w")
    prog = ttk.Progressbar(frm, length=480, mode="indeterminate")
    prog.pack(pady=10)
    prog.start(15)
    status = tk.StringVar(value="Vorbereitung …")
    ttk.Label(frm, textvariable=status, foreground="#555",
              wraplength=480).pack(anchor="w")
    ttk.Label(frm, text="(nur beim ersten Start nötig)",
              foreground="#888", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

    def _set(msg: str) -> None:
        root.after(0, status.set, msg)

    def _install() -> None:
        try:
            _set(f"pip install {' '.join(missing)}")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
                timeout=300,
            )
            _refresh_sys_path()
            _mark_setup_done()
            root.after(0, root.destroy)
        except Exception as exc:  # noqa: BLE001
            def _err() -> None:
                prog.stop()
                status.set(f"FEHLER: {exc}")
                messagebox.showerror(
                    "Setup-Fehler",
                    f"Installation fehlgeschlagen:\n{exc}\n\n"
                    "Bitte manuell ausführen:\n"
                    "    py -m pip install -r requirements.txt",
                    parent=root,
                )
            root.after(0, _err)

    threading.Thread(target=_install, daemon=True).start()
    root.mainloop()
