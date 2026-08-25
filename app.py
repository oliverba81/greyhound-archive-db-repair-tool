"""Einstiegspunkt fuer die gebaute .exe (PyInstaller).

Startet dieselbe Logik wie ``python -m gh_repair``: ohne Argumente die GUI,
mit Argumenten den Headless-Modus.
"""

from gh_repair.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
