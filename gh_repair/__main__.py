"""Einstiegspunkt.

Ohne Argumente startet die grafische Oberflaeche. Mit Argumenten laeuft das
Tool headless (nuetzlich fuer Skripte/Tests):

    python -m gh_repair                              # GUI
    python -m gh_repair repair  QUELLE ZIEL          # Reparatur
    python -m gh_repair merge   ZIEL Q1 Q2 [Q3 ...]  # Zusammenfuehren
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import engine


def _cli(argv: list[str]) -> int:
    cmd = argv[0]
    log = print
    if cmd == "repair" and len(argv) == 3:
        report = engine.rebuild_archive([Path(argv[1])], Path(argv[2]), log)
    elif cmd == "merge" and len(argv) >= 4:
        target = Path(argv[1])
        sources = [Path(p) for p in argv[2:]]
        report = engine.rebuild_archive(sources, target, log)
    else:
        print(__doc__)
        return 2
    print()
    print(f"Fertig. Items: {report.items_total}, "
          f"umnummeriert: {report.items_renumbered}, "
          f".eml: {report.eml_copied}, "
          f"Integritaet: {'OK' if report.integrity_ok else 'FEHLER'}")
    return 0 if report.integrity_ok else 1


def main() -> int:
    if len(sys.argv) > 1:
        return _cli(sys.argv[1:])
    from .gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
