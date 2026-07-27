import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_cache.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS obituary_names (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    posted_date TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    owner_name TEXT,
    parcel_id TEXT,
    cleaned_full_name TEXT,
    cleaned_owner_name TEXT,
    confirmed INTEGER DEFAULT 0,
    matched_keywords TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    posted_date TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS obituary_url (
    full_name TEXT PRIMARY KEY,
    obituary_url TEXT
);
"""

# Characters the old NAME_SEPERATOR.xlsx formula stripped before splitting.
_STRIP_CHARS = '(),"\'.!?;'


def clean_name(raw_name):
    """Strip punctuation and drop words under 4 characters.

    Mirrors NAME_SEPERATOR.xlsx's TEXTSPLIT/FILTER formula: it discards
    short suffixes/initials/connectors (JR, TR, TRS, H, &, AND, ...) and
    keeps the words that are actually likely to be a first/last name.
    """
    if not raw_name:
        return ""
    cleaned = raw_name
    for ch in _STRIP_CHARS:
        cleaned = cleaned.replace(ch, "")
    words = [w for w in cleaned.split() if len(w) >= 4]
    return " ".join(words)


def _ensure_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(matches)").fetchall()}
    if "cleaned_full_name" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN cleaned_full_name TEXT")
    if "cleaned_owner_name" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN cleaned_owner_name TEXT")
    if "confirmed" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN confirmed INTEGER DEFAULT 0")
    if "matched_keywords" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN matched_keywords TEXT")
    if "address" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN address TEXT")
    if "city" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN city TEXT")
    if "state" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN state TEXT")
    if "zip_code" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN zip_code TEXT")
    if "created_at" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN created_at TEXT")
    if "posted_date" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN posted_date TEXT")

    obituary_cols = {row[1] for row in conn.execute("PRAGMA table_info(obituary_names)").fetchall()}
    if "posted_date" not in obituary_cols:
        conn.execute("ALTER TABLE obituary_names ADD COLUMN posted_date TEXT")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def replace_obituary_names(entries):
    """Overwrite the cached name list with (name, posted_date) pairs from the
    latest scrape. posted_date is an ISO 'YYYY-MM-DD' string."""
    with connect() as conn:
        conn.execute("DELETE FROM obituary_names")
        conn.executemany(
            "INSERT OR IGNORE INTO obituary_names (name, posted_date) VALUES (?, ?)",
            list(entries),
        )


def get_obituary_names():
    with connect() as conn:
        rows = conn.execute("SELECT name FROM obituary_names ORDER BY id").fetchall()
    return [r[0] for r in rows]


def get_obituary_names_with_dates():
    with connect() as conn:
        rows = conn.execute("SELECT name, posted_date FROM obituary_names ORDER BY id").fetchall()
    return [(r[0], r[1]) for r in rows]


def clear_matches():
    """Start a fresh cache of matches for the current SLK$ run."""
    with connect() as conn:
        conn.execute("DELETE FROM matches")


def add_match(full_name, owner_name, parcel_id, address="", city="", state="", zip_code="", posted_date=None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO matches
               (full_name, owner_name, parcel_id, cleaned_full_name, cleaned_owner_name,
                address, city, state, zip_code, posted_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (full_name, owner_name, parcel_id, clean_name(full_name), clean_name(owner_name),
             address, city, state, zip_code, posted_date, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def get_matches():
    with connect() as conn:
        rows = conn.execute(
            """SELECT full_name, owner_name, parcel_id, cleaned_full_name, cleaned_owner_name,
                      confirmed, matched_keywords, address, city, state, zip_code, created_at
               FROM matches ORDER BY id"""
        ).fetchall()
    return rows


def get_matches_full():
    """All match rows with their id, for DC.py to confirm and write back to."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, full_name, owner_name, parcel_id, cleaned_full_name, cleaned_owner_name,
                      posted_date, address, city, state, zip_code
               FROM matches ORDER BY id"""
        ).fetchall()
    return rows


def set_match_confirmation(match_id, confirmed, matched_keywords):
    with connect() as conn:
        conn.execute(
            "UPDATE matches SET confirmed = ?, matched_keywords = ? WHERE id = ?",
            (1 if confirmed else 0, matched_keywords, match_id),
        )


def set_obituary_url(full_name, url):
    with connect() as conn:
        conn.execute(
            """INSERT INTO obituary_url (full_name, obituary_url) VALUES (?, ?)
               ON CONFLICT(full_name) DO UPDATE SET obituary_url = excluded.obituary_url""",
            (full_name, url),
        )


def get_address_by_parcel_id(parcel_id):
    """DC.py's exported xlsx only carries Full Name/Owner Name/Parcel ID/etc,
    not the address - pricer.py needs to look it back up here by Parcel ID."""
    with connect() as conn:
        row = conn.execute(
            "SELECT address, city, state, zip_code FROM matches WHERE parcel_id = ?",
            (parcel_id,),
        ).fetchone()
    return row if row else ("", "", "", "")


def resolve_mode(argv):
    """'daily' or 'weekly' - taken from argv[1] when the caller passed one
    (e.g. master.py invoking this script as a subprocess), otherwise asked
    for interactively so the script is still usable stand-alone."""
    if len(argv) > 1:
        mode = argv[1].strip().lower()
        return mode if mode in ("daily", "weekly") else "daily"
    while True:
        choice = input("Weekly or daily mode? [weekly/daily]: ").strip().lower()
        if choice in ("weekly", "w"):
            return "weekly"
        if choice in ("daily", "d"):
            return "daily"
        print("Please enter 'weekly' or 'daily'.")


def get_obituary_url(full_name):
    with connect() as conn:
        row = conn.execute(
            "SELECT obituary_url FROM obituary_url WHERE full_name = ?", (full_name,)
        ).fetchone()
    return row[0] if row else None
