"""Gemeinsame Rebuild-Engine fuer Reparatur und Merge.

Beide Funktionen schreiben IMMER in ein neues Zielarchiv und veraendern die
Quellen nie. Reparatur ist ein Rebuild aus genau einer Quelle, Merge ein
Rebuild aus mehreren Quellen.

Verlustfreiheit:
* Items mit kollidierender ID werden neu nummeriert (.eml umbenannt,
  alle ``i_item_r``-Verweise mitgezogen).
* Eine ``.eml`` ohne DB-Eintrag (verwaiste Datei, z.B. nach DB-Verlust) wird
  aus den Mail-Headern als Item rekonstruiert, statt verloren zu gehen.
* Ein DB-Eintrag ohne ``.eml`` bleibt erhalten (nur die Maildatei fehlt).
* Der defekte FTS-Index und die Statistik werden frisch neu aufgebaut.
"""

from __future__ import annotations

import email
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from . import core, schema
from .core import Archive, Report

Logger = Callable[[str], None]


# --------------------------------------------------------------------------
# ID-Vergabe
# --------------------------------------------------------------------------
class IdAllocator:
    """Vergibt eindeutige Item-IDs; behaelt Original-IDs wenn moeglich."""

    def __init__(self) -> None:
        self._used: set[int] = set()
        self._max = 0

    def allocate(self, preferred: int) -> tuple[int, bool]:
        """Gibt (id, renumbered) zurueck."""
        if preferred not in self._used and preferred > 0:
            self._used.add(preferred)
            self._max = max(self._max, preferred)
            return preferred, False
        new_id = self._max + 1
        self._used.add(new_id)
        self._max = new_id
        return new_id, True


# --------------------------------------------------------------------------
# Quelle einlesen
# --------------------------------------------------------------------------
def _default_for(col: str):
    return 0 if col.startswith(("i_", "d_")) else ""


def _scan_eml(archive: Archive, log: Logger) -> tuple[dict[int, Path], list[Path]]:
    """Indexiert alle .eml im Archiv: {item_id: pfad}. Liefert auch ungueltige."""
    index: dict[int, Path] = {}
    bad: list[Path] = []
    for path in archive.root.rglob("*.eml"):
        try:
            index[int(path.stem)] = path
        except ValueError:
            bad.append(path)
    if bad:
        log(f"  ! {len(bad)} .eml mit unerwartetem Dateinamen gefunden")
    return index, bad


def _reconstruct_item(item_id: int, eml_path: Path) -> dict:
    """Baut aus einer verwaisten .eml ein minimales Item (Header-basiert)."""
    item = {col: _default_for(col) for col in schema.SCHEMA_COLS_ITEMS}
    item["i_id"] = item_id
    try:
        msg = email.message_from_bytes(eml_path.read_bytes(), policy=policy.default)
    except Exception:  # noqa: BLE001
        return item
    item["c_from"] = str(msg.get("From", "") or "")[:256]
    item["c_to"] = str(msg.get("To", "") or "")[:1024]
    item["c_subject"] = str(msg.get("Subject", "") or "")[:256]
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt is not None:
            ts = int(dt.timestamp())
            item["d_startdate"] = item["d_enddate"] = ts
            item["d_created"] = item["d_modified"] = item["d_archived"] = ts
    except Exception:  # noqa: BLE001
        pass
    return item


class SourceData:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.children: dict[str, dict[int, list[dict]]] = {
            t: defaultdict(list) for t in schema.CHILD_TABLES
        }
        self.disk: dict[int, Path] = {}
        self.schema_sql: dict[str, str] | None = None
        self.missing: list[int] = []
        self.orphaned: list[int] = []


def read_source(archive: Archive, log: Logger) -> SourceData:
    data = SourceData()
    log(f"Lese Quelle: {archive.root}")

    disk, _bad = _scan_eml(archive, log)
    data.disk = disk

    db_items: dict[int, dict] = {}
    if archive.db_path.is_file():
        try:
            conn = core.open_readonly(archive.db_path)
        except sqlite3.Error as exc:
            log(f"  ! archive.db3 nicht lesbar ({exc}) – rekonstruiere aus .eml")
            conn = None
        if conn is not None:
            try:
                data.schema_sql = core.get_schema_sql(conn)
                for row in core.safe_read_rows(conn, "items", log):
                    if "i_id" in row:
                        db_items[row["i_id"]] = row
                for t in schema.CHILD_TABLES:
                    for row in core.safe_read_rows(conn, t, log):
                        ref = row.get("i_item_r")
                        if ref is not None:
                            data.children[t][ref].append(row)
                log(f"  {len(db_items)} Items, "
                    + ", ".join(
                        f"{sum(len(v) for v in data.children[t].values())} {t}"
                        for t in schema.CHILD_TABLES
                    ))
            finally:
                conn.close()
    else:
        log("  ! keine archive.db3 vorhanden – rekonstruiere aus .eml")

    db_ids = set(db_items)
    disk_ids = set(disk)

    data.missing = sorted(db_ids - disk_ids)
    data.orphaned = sorted(disk_ids - db_ids)
    if data.missing:
        log(f"  ! {len(data.missing)} Item(s) ohne .eml-Datei")
    if data.orphaned:
        log(f"  ! {len(data.orphaned)} verwaiste .eml ohne DB-Eintrag – "
            "werden rekonstruiert")

    items = list(db_items.values())
    for oid in data.orphaned:
        items.append(_reconstruct_item(oid, disk[oid]))
    items.sort(key=lambda it: it["i_id"])
    data.items = items
    return data


# --------------------------------------------------------------------------
# Zielarchiv schreiben
# --------------------------------------------------------------------------
def _create_target_db(db_path: Path, schema_sql: dict[str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for table in schema.TABLE_ORDER:
        conn.execute(schema_sql.get(table, schema.SCHEMA[table]))
    conn.commit()
    return conn


def _insert_item(conn: sqlite3.Connection, cols: list[str], item: dict) -> None:
    values = [item.get(c, _default_for(c)) for c in cols]
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders})", values
    )


def _insert_child(
    conn: sqlite3.Connection, table: str, cols: list[str], row: dict, new_ref: int
) -> None:
    # i_id ist AUTOINCREMENT -> nicht setzen, SQLite vergibt neue eindeutige ID.
    use_cols = [c for c in cols if c != "i_id"]
    values = []
    for c in use_cols:
        values.append(new_ref if c == "i_item_r" else row.get(c, _default_for(c)))
    placeholders = ",".join("?" * len(use_cols))
    conn.execute(
        f"INSERT INTO {table} ({','.join(use_cols)}) VALUES ({placeholders})", values
    )


def _backup_sources(sources: list[Path], log: Logger, report: Report) -> None:
    """Legt vor jeder Aktion eine vollstaendige Kopie jeder Quelle an."""
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    for src in sources:
        src = Path(src)
        dst = src.parent / f"{src.name} - Backup {ts}"
        n = 1
        while dst.exists():
            dst = src.parent / f"{src.name} - Backup {ts} ({n})"
            n += 1
        log(f"Backup der Quelle anlegen: {dst}")
        shutil.copytree(src, dst)
        report.backups.append(str(dst))


def rebuild_archive(
    sources: list[Path],
    target_root: Path,
    log: Logger,
    progress: Callable[[float], None] | None = None,
    backup: bool = True,
) -> Report:
    """Baut aus einer oder mehreren Quellen ein neues Zielarchiv auf.

    backup=True legt vorab eine vollstaendige Sicherungskopie jeder Quelle an.
    """
    report = Report()
    target = Archive(target_root)

    if target.db_path.exists():
        raise FileExistsError(
            f"Zielordner enthaelt bereits eine archive.db3: {target.db_path}"
        )

    if backup:
        _backup_sources(sources, log, report)

    target_root.mkdir(parents=True, exist_ok=True)
    target.logs_dir.mkdir(exist_ok=True)

    # --- Quellen einlesen ------------------------------------------------
    source_data: list[tuple[Archive, SourceData]] = []
    schema_sql: dict[str, str] | None = None
    for src in sources:
        arc = Archive(Path(src))
        data = read_source(arc, log)
        source_data.append((arc, data))
        if schema_sql is None and data.schema_sql is not None:
            schema_sql = data.schema_sql
    if schema_sql is None:
        log("! Kein lesbares Quell-Schema – nutze eingebautes Standard-Schema")
        schema_sql = dict(schema.SCHEMA)

    # --- Zielschema -------------------------------------------------------
    log(f"Erzeuge Zielarchiv: {target_root}")
    conn = _create_target_db(target.db_path, schema_sql)
    item_cols = core.columns_of(conn, "items")
    child_cols = {t: core.columns_of(conn, t) for t in schema.CHILD_TABLES}

    allocator = IdAllocator()
    fts_inputs: list[tuple[dict, Path | None, list[dict], list[dict]]] = []

    total_items = sum(len(d.items) for _a, d in source_data) or 1
    done = 0

    try:
        for arc, data in source_data:
            for item in data.items:
                old_id = item["i_id"]
                new_id, renumbered = allocator.allocate(old_id)
                item = dict(item)
                item["i_id"] = new_id
                if renumbered:
                    report.items_renumbered += 1
                    log(f"  Kollision: Item {old_id} -> {new_id} (umnummeriert)")

                _insert_item(conn, item_cols, item)
                report.items_total += 1

                # Child-Zeilen mit neuem Verweis uebernehmen
                remarks_rows = data.children["remarks"].get(old_id, [])
                userfields_rows = data.children["userfields"].get(old_id, [])
                for t in schema.CHILD_TABLES:
                    for row in data.children[t].get(old_id, []):
                        _insert_child(conn, t, child_cols[t], row, new_id)

                # .eml kopieren
                src_eml = data.disk.get(old_id)
                target_eml = target.eml_path(new_id)
                if src_eml and src_eml.is_file():
                    target_eml.parent.mkdir(parents=True, exist_ok=True)
                    target_eml.write_bytes(src_eml.read_bytes())
                    report.eml_copied += 1
                    report.total_size += target_eml.stat().st_size
                else:
                    report.eml_missing.append(f"{arc.root.name}: Item {old_id}")
                    target_eml = None

                fts_inputs.append((item, target_eml, remarks_rows, userfields_rows))

                done += 1
                if progress and done % 25 == 0:
                    progress(done / total_items * 0.8)

            report.eml_orphaned += [f"{arc.root.name}: {i}" for i in data.orphaned]

        conn.commit()

        # --- FTS-Index neu aufbauen --------------------------------------
        log("Baue Volltextindex (fts) neu auf ...")
        _rebuild_fts(conn, fts_inputs)
        report.fts_indexed = len(fts_inputs)
        if progress:
            progress(0.9)

        # --- Statistik neu berechnen -------------------------------------
        log("Berechne Statistik neu ...")
        conn.execute("DELETE FROM statistics")
        conn.execute(
            "INSERT INTO statistics (i_count, i_size) VALUES (?, ?)",
            (report.items_total, report.total_size),
        )
        conn.commit()
    finally:
        conn.close()

    # --- Logs zusammenfuehren --------------------------------------------
    _merge_logs([a for a, _ in source_data], target, log)

    # --- Abschlusspruefung -----------------------------------------------
    report.integrity_ok, report.integrity_msg = core.integrity_check(target.db_path)
    log(
        f"Integritaetspruefung Zielarchiv: "
        f"{'OK' if report.integrity_ok else 'FEHLER: ' + report.integrity_msg}"
    )
    if progress:
        progress(1.0)
    return report


def _rebuild_fts(
    conn: sqlite3.Connection,
    inputs: list[tuple[dict, Path | None, list[dict], list[dict]]],
) -> None:
    # 'fts'-Tabelle ist contentless (content="") -> kein automatisches
    # 'rebuild' moeglich; jede Zeile wird mit ihrer docid manuell eingefuegt.
    for item, eml_path, remarks_rows, userfields_rows in inputs:
        fts = core.build_fts_row(item, eml_path, remarks_rows, userfields_rows)
        conn.execute(
            "INSERT INTO fts(docid, fromto, subject, body, remarks, attributes) "
            "VALUES (?,?,?,?,?,?)",
            (fts.docid, fts.fromto, fts.subject, fts.body, fts.remarks, fts.attributes),
        )
    conn.commit()


def _merge_logs(sources: list[Archive], target: Archive, log: Logger) -> None:
    merged = 0
    for arc in sources:
        if not arc.logs_dir.is_dir():
            continue
        for src_log in sorted(arc.logs_dir.glob("*.log")):
            dst = target.logs_dir / src_log.name
            try:
                content = src_log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            header = f"\n# --- aus {arc.root.name} ---\n"
            with dst.open("a", encoding="utf-8") as fh:
                fh.write(header + content)
            merged += 1
    if merged:
        log(f"{merged} Log-Datei(en) zusammengefuehrt")
