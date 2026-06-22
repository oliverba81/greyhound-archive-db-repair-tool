# GREYHOUND Archive Repair Tool

Repariert eine einzelne defekte `archive.db3` und führt mehrere
GREYHOUND-Archive **verlustfrei** zu einem einzigen zusammen – damit kein
ständiges Wechseln zwischen mehreren Archiven mehr nötig ist.

## Hintergrund

Das GREYHOUND Archive Tool erzeugt je Archiv folgende Struktur:

```
GREYHOUND Archive/
├── 008/                  ← Bucket-Ordner = (Item-ID mod 4096), 3-stellig
│   └── 4104.eml          ← Mail-Datei, benannt nach Item-ID
├── archive.db3           ← SQLite-Metadatenbank
└── logs/                 ← Tageslogs
```

Geht die `archive.db3` kaputt (meist der Index), legt man eine neue an –
und hat danach zwei getrennte Archive. Dieses Tool führt sie wieder zusammen
bzw. repariert ein defektes Archiv.

## Start

Voraussetzung: **Python 3.10+** (Windows: „py“-Launcher, in Python enthalten;
keine zusätzlichen Pakete nötig – nur die Standardbibliothek).

- **Doppelklick** auf `Tool starten.bat`, **oder**
- im Terminal: `py -m gh_repair`

## Funktionen

### Reparieren (eine DB)
Erzeugt eine reparierte **Kopie** des gewählten Archivs:
- **DB wiederherstellen:** liest auch aus beschädigten DBs so viel wie möglich;
  unlesbare Zeilen werden übersprungen statt den Vorgang abzubrechen. Ist die DB
  gar nicht mehr lesbar, wird komplett aus den `.eml` rekonstruiert.
- **FTS-Volltextindex neu aufbauen** (das Hauptproblem bei Index-Schäden).
- **Statistik neu berechnen** (`count`/`size`).
- **Datei↔DB abgleichen:** Mails ohne DB-Eintrag (verwaiste `.eml`) werden aus
  den Mail-Headern als Item rekonstruiert; DB-Einträge ohne `.eml` bleiben
  erhalten und werden gemeldet.

### Zusammenführen (mehrere DBs)
Erzeugt ein neues Gesamtarchiv aus zwei oder mehr Quellen:
- Alle Items, Protokolle, Notizen, Benutzerfelder und `.eml` werden übernommen.
- **ID-Kollisionen** (gleiche `i_id` in mehreren Archiven) werden automatisch
  neu nummeriert: die `.eml` wird in den korrekten Ziel-Bucket umbenannt und
  alle Verweise (`i_item_r`) werden mitgezogen.
- FTS-Index und Statistik werden frisch aufgebaut, Logs zusammengeführt.

## Sicherheit / Verlustfreiheit

- **Quellen werden nie verändert** – es wird ausschließlich in ein neues
  Zielarchiv geschrieben (geöffnet wird die Quelle schreibgeschützt).
- Der Zielordner darf keine bestehende `archive.db3` enthalten (Überschreibschutz).
- Am Ende läuft eine Integritätsprüfung des Zielarchivs.

> Hinweis: `PRAGMA integrity_check` meldet bei GREYHOUND-Archiven immer
> „unable to validate the inverted index for FTS4 table“. Das ist **kein
> Defekt**, sondern eine Eigenheit der contentless-FTS4-Tabelle und tritt auch
> bei völlig intakten Original-Archiven auf – das Tool wertet diese Meldung
> deshalb als unkritisch.

## Headless / Automatisierung

```
py -m gh_repair repair  QUELLE         ZIEL
py -m gh_repair merge   ZIEL  Q1 Q2 [Q3 ...]
```

## Auto-Update & Changelog

Das Tool aktualisiert sich selbst über **GitHub Releases** (gleiches Prinzip
wie das Website-Scraper-Projekt), angepasst an die Paketstruktur:

- Beim Start (nach 3 Sek.) wird im Hintergrund auf eine neuere Version geprüft.
  Gibt es eine, wird ein Update angeboten, das Release-Asset `gh_repair.zip`
  heruntergeladen, über das Paket entpackt und das Tool neu gestartet.
- Der **Changelog-Tab** zeigt die letzten 10 Releases (mit „Auf Updates prüfen").
- Dependency-frei (nur Standardbibliothek; urllib mit Auth-Strip beim Redirect,
  damit auch private Repos funktionieren).

### Einrichtung (einmalig)

1. `GITHUB_REPO` in [gh_repair/updater.py](gh_repair/updater.py) ist gesetzt auf
   `oliverba81/greyhound-archive-db-repair-tool`.
2. Für **private** Repos: eine Datei `_token.py` neben dem Ordner `gh_repair/`
   anlegen (wird per `.gitignore` nie committet):
   ```python
   GITHUB_UPDATE_TOKEN = "github_pat_…"   # read-only, scope: Contents
   ```
   Bei öffentlichen Repos ist kein Token nötig.
3. Die GitHub Action [version-bump.yml](.github/workflows/version-bump.yml)
   erhöht bei jedem Merge nach `main` die Version (`__version__` in
   `gh_repair/__init__.py`), taggt, baut `gh_repair.zip` und legt ein Release
   mit auto-generierten Release-Notes an. Bump-Typ über PR-Label steuerbar:
   `version:major` / `version:minor` (Standard: patch).

## Aufbau

| Datei | Inhalt |
|---|---|
| `gh_repair/schema.py` | Archivstruktur, Bucket-Schema (`mod 4096`), Tabellen-Schema |
| `gh_repair/core.py` | Robuster DB-Lesezugriff, `.eml`-Textextraktion, FTS-Texte |
| `gh_repair/engine.py` | Gemeinsame Rebuild-Engine (Reparatur = 1 Quelle, Merge = n Quellen) |
| `gh_repair/updater.py` | Auto-Update & Changelog über GitHub Releases |
| `gh_repair/gui.py` | tkinter-Oberfläche (Tabs: Reparieren, Zusammenführen, Changelog) |
| `gh_repair/__main__.py` | Einstiegspunkt (GUI bzw. CLI) |
| `.github/workflows/version-bump.yml` | Auto-Versionierung + Release-Build |
