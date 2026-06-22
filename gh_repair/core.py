"""Lesezugriff auf GREYHOUND-Archive und Hilfsfunktionen.

Alle Lesezugriffe sind moeglichst tolerant gegenueber Beschaedigungen:
Eine kaputte ``archive.db3`` (typisch: defekter Index) soll so weit wie
moeglich ausgelesen werden, ohne dass eine einzelne unlesbare Zeile den
gesamten Vorgang abbricht.
"""

from __future__ import annotations

import email
import re
import sqlite3
from dataclasses import dataclass, field
from email import policy
from html.parser import HTMLParser
from pathlib import Path

from . import schema


# --------------------------------------------------------------------------
# Archiv-Repraesentation
# --------------------------------------------------------------------------
@dataclass
class Archive:
    """Ein GREYHOUND-Archivordner (enthaelt archive.db3, Bucket-Ordner, logs)."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "archive.db3"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def eml_path(self, item_id: int) -> Path:
        """Erwarteter Pfad der .eml fuer ein Item gemaess Bucket-Schema."""
        return self.root / schema.bucket_name(item_id) / f"{item_id}.eml"

    def is_archive(self) -> bool:
        return self.db_path.is_file()


# --------------------------------------------------------------------------
# Robuster DB-Zugriff
# --------------------------------------------------------------------------
def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Oeffnet eine DB schreibgeschuetzt (veraendert die Quelle nie)."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# PRAGMA integrity_check kann die contentless FTS4-Tabelle ('content=""') von
# GREYHOUND grundsaetzlich nicht validieren und meldet das immer – auch bei
# voellig intakten Original-Archiven. Diese Meldung ist daher KEIN Defekt.
_FTS_BENIGN = "unable to validate the inverted index"


def integrity_check(db_path: Path) -> tuple[bool, str]:
    """PRAGMA integrity_check. Gibt (ok, meldung) zurueck.

    Die bei GREYHOUND-Archiven generell auftretende FTS4-Meldung wird als
    unkritisch gewertet (sie erscheint auch bei intakten Originalen). Kann die
    DB gar nicht geoeffnet werden, gilt sie als defekt.
    """
    try:
        conn = open_readonly(db_path)
    except sqlite3.Error as exc:
        return False, f"DB kann nicht geoeffnet werden: {exc}"
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        return False, f"integrity_check fehlgeschlagen: {exc}"
    finally:
        conn.close()

    real_problems = [
        r for r in rows
        if r.strip().lower() != "ok" and _FTS_BENIGN not in r.lower()
    ]
    if not real_problems:
        return True, "ok"
    return False, "; ".join(real_problems)


def get_schema_sql(conn: sqlite3.Connection) -> dict[str, str]:
    """Liest die CREATE-Anweisungen aus sqlite_master (fuer 1:1-Treue).

    Fehlende/unlesbare Definitionen werden aus :data:`schema.SCHEMA` ergaenzt.
    """
    found: dict[str, str] = {}
    try:
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table','view') AND sql IS NOT NULL"
        ):
            if name in schema.SCHEMA:
                found[name] = sql
    except sqlite3.Error:
        pass
    return {name: found.get(name, schema.SCHEMA[name]) for name in schema.SCHEMA}


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def safe_read_rows(conn: sqlite3.Connection, table: str, log) -> list[dict]:
    """Liest eine Tabelle komplett, faengt Beschaedigungen ab.

    Schlaegt ein voller Scan fehl (z.B. wegen einer kaputten Seite), wird
    versucht, Zeile fuer Zeile ueber den Primaerschluessel zu lesen, damit
    moeglichst viele intakte Datensaetze gerettet werden.
    """
    try:
        cols = columns_of(conn, table)
    except sqlite3.Error:
        log(f"  ! Tabelle '{table}' nicht lesbar (Struktur defekt) – uebersprungen")
        return []
    if not cols:
        return []
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    except sqlite3.Error as exc:
        log(f"  ! '{table}' nicht am Stueck lesbar ({exc}) – versuche zeilenweise")
        return _read_rows_one_by_one(conn, table, cols, log)


def _read_rows_one_by_one(
    conn: sqlite3.Connection, table: str, cols: list[str], log
) -> list[dict]:
    pk = "i_id" if "i_id" in cols else "rowid"
    try:
        ids = [r[0] for r in conn.execute(f"SELECT {pk} FROM {table}")]
    except sqlite3.Error:
        log(f"  ! Schlusselliste von '{table}' unlesbar – Tabelle verloren")
        return []
    rows: list[dict] = []
    lost = 0
    for rid in ids:
        try:
            r = conn.execute(
                f"SELECT * FROM {table} WHERE {pk}=?", (rid,)
            ).fetchone()
            if r is not None:
                rows.append(dict(r))
        except sqlite3.Error:
            lost += 1
    if lost:
        log(f"  ! {lost} unlesbare Zeile(n) in '{table}' verloren")
    return rows


# --------------------------------------------------------------------------
# E-Mail-Body fuer den Volltextindex
# --------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(self._parts)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - kaputtes HTML soll nie crashen
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text


def extract_body_text(eml_path: Path) -> str:
    """Extrahiert den lesbaren Klartext einer .eml (Text bevorzugt, sonst HTML)."""
    try:
        raw = eml_path.read_bytes()
    except OSError:
        return ""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001
        return ""
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001
        body = None
    if body is None:
        return ""
    try:
        content = body.get_content()
    except Exception:  # noqa: BLE001
        return ""
    subtype = (body.get_content_subtype() or "").lower()
    if subtype == "html":
        return _html_to_text(content)
    return content


# --------------------------------------------------------------------------
# Texte fuer die FTS-Spalten zusammensetzen
# --------------------------------------------------------------------------
@dataclass
class FtsRow:
    docid: int
    fromto: str = ""
    subject: str = ""
    body: str = ""
    remarks: str = ""
    attributes: str = ""


def build_fts_row(
    item: dict,
    eml_path: Path | None,
    remarks: list[dict],
    userfields: list[dict],
) -> FtsRow:
    """Stellt eine FTS-Indexzeile fuer ein Item zusammen.

    Die Spaltenzuordnung bildet das GREYHOUND-Suchverhalten nach: Absender/
    Empfaenger, Betreff, Mailtext, Notizen sowie zusaetzliche Attribute
    (Aufgabennummer, Thema, Gruppe, Farbe, Benutzerfelder).
    """
    fromto = f"{item.get('c_from', '')} {item.get('c_to', '')}".strip()
    subject = item.get("c_subject", "") or ""
    body = extract_body_text(eml_path) if eml_path and eml_path.is_file() else ""
    remarks_text = " ".join(r.get("c_text", "") or "" for r in remarks).strip()
    attr_parts = [
        item.get("c_tasknumber", "") or "",
        item.get("c_topicpath", "") or "",
        item.get("c_grouppath", "") or "",
        item.get("c_colorname", "") or "",
    ]
    attr_parts += [u.get("c_value", "") or "" for u in userfields]
    attributes = " ".join(p for p in attr_parts if p).strip()
    return FtsRow(
        docid=item["i_id"],
        fromto=fromto,
        subject=subject,
        body=body,
        remarks=remarks_text,
        attributes=attributes,
    )


# --------------------------------------------------------------------------
# Ergebnis-/Report-Objekt
# --------------------------------------------------------------------------
@dataclass
class Report:
    items_total: int = 0
    items_renumbered: int = 0
    eml_copied: int = 0
    backups: list[str] = field(default_factory=list)
    eml_missing: list[str] = field(default_factory=list)
    eml_orphaned: list[str] = field(default_factory=list)
    rows_recovered: dict[str, int] = field(default_factory=dict)
    fts_indexed: int = 0
    total_size: int = 0
    integrity_ok: bool = False
    integrity_msg: str = ""
    warnings: list[str] = field(default_factory=list)
