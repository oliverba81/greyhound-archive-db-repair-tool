"""Struktur eines GREYHOUND-Archivs.

Die ``CREATE``-Anweisungen entsprechen exakt dem Schema, das das GREYHOUND
Archive Tool erzeugt (ermittelt aus ``sqlite_master`` echter Archive). Beim
Aufbau eines Zielarchivs wird, wenn moeglich, das Schema einer lesbaren Quelle
1:1 uebernommen; diese Konstanten dienen als Fallback, falls die Quelle so
defekt ist, dass ihr Schema nicht mehr gelesen werden kann.
"""

from __future__ import annotations

# --- Bucket-Schema --------------------------------------------------------
# GREYHOUND legt die .eml einer Mail im Unterordner  (i_id mod 4096)  ab,
# der Ordnername ist mindestens dreistellig nullaufgefuellt:
#   i_id 4104 -> 4104 mod 4096 = 8   -> "008"
#   i_id 4101 -> 4101 mod 4096 = 5   -> "005"
# (Vom Anwender / GREYHOUND bestaetigtes Schema.)
BUCKET_MODULUS = 4096


def bucket_name(item_id: int) -> str:
    """Ordnername fuer die ``.eml`` eines Items (z.B. 4104 -> '008')."""
    return f"{item_id % BUCKET_MODULUS:03d}"


# --- Tabellen-Schema (Fallback) ------------------------------------------
SCHEMA: dict[str, str] = {
    "items": (
        "CREATE TABLE items ("
        "i_id integer NOT NULL PRIMARY KEY,"
        "c_from varchar(256) NOT NULL,"
        "c_to varchar(1024) NOT NULL,"
        "c_subject varchar(256) NOT NULL,"
        "c_grouppath varchar(256) NOT NULL,"
        "c_username varchar(32) NOT NULL,"
        "c_topicpath varchar(256) NOT NULL,"
        "c_colorname varchar(64) NOT NULL,"
        "c_tasknumber varchar(32) NOT NULL,"
        "i_state integer NOT NULL,"
        "i_kind integer NOT NULL,"
        "d_startdate integer NOT NULL,"
        "d_enddate integer NOT NULL,"
        "d_created integer NOT NULL,"
        "d_modified integer NOT NULL,"
        "d_archived integer NOT NULL)"
    ),
    "protocols": (
        "CREATE TABLE protocols ("
        "i_id integer NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "i_item_r integer NOT NULL,"
        "c_username varchar(32) NOT NULL,"
        "c_rulename varchar(64) NOT NULL,"
        "c_text text NOT NULL,"
        "d_created integer NOT NULL)"
    ),
    "remarks": (
        "CREATE TABLE remarks ("
        "i_id integer NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "i_item_r integer NOT NULL,"
        "c_username varchar(32) NOT NULL,"
        "c_text text NOT NULL,"
        "d_created integer NOT NULL)"
    ),
    "userfields": (
        "CREATE TABLE userfields ("
        "i_id integer NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "i_item_r integer NOT NULL,"
        "c_name varchar(32) NOT NULL,"
        "c_description varchar(64) NOT NULL,"
        "i_datatype integer NOT NULL,"
        "c_value varchar(1024) NOT NULL)"
    ),
    "statistics": (
        "CREATE TABLE statistics ("
        "i_count integer NOT NULL,"
        "i_size bigint NOT NULL)"
    ),
    "fts": (
        'CREATE VIRTUAL TABLE fts USING fts4('
        "fromto, subject, body, remarks, attributes, "
        'content="", tokenize=unicode61)'
    ),
}

# Reihenfolge, in der Tabellen angelegt werden (fts zuletzt).
TABLE_ORDER = ["items", "protocols", "remarks", "userfields", "statistics", "fts"]

# Datentabellen, die beim Rebuild Zeilen liefern (ohne statistics/fts, die
# werden neu berechnet bzw. neu aufgebaut).
DATA_TABLES = ["items", "protocols", "remarks", "userfields"]

# Child-Tabellen, deren ``i_item_r`` auf ``items.i_id`` verweist.
CHILD_TABLES = ["protocols", "remarks", "userfields"]

# Spalten der items-Tabelle (fuer Rekonstruktion verwaister .eml).
SCHEMA_COLS_ITEMS = [
    "i_id", "c_from", "c_to", "c_subject", "c_grouppath", "c_username",
    "c_topicpath", "c_colorname", "c_tasknumber", "i_state", "i_kind",
    "d_startdate", "d_enddate", "d_created", "d_modified", "d_archived",
]
