import json
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

CREATE TABLE IF NOT EXISTS letters_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    owner_name TEXT,
    parcel_id TEXT,
    template_id TEXT,
    message TEXT,
    lob_letter_id TEXT,
    status TEXT,
    expected_delivery_date TEXT,
    preview_url TEXT,
    test_mode INTEGER DEFAULT 1,
    error TEXT,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS pdf_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    template_name TEXT NOT NULL,
    filename TEXT NOT NULL,
    uploaded_at TEXT
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

    pdf_template_cols = {row[1] for row in conn.execute("PRAGMA table_info(pdf_templates)").fetchall()}
    if "field_positions" not in pdf_template_cols:
        conn.execute("ALTER TABLE pdf_templates ADD COLUMN field_positions TEXT")


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


def record_letter_sent(full_name, owner_name, parcel_id, template_id, message,
                        lob_letter_id="", status="", expected_delivery_date="",
                        preview_url="", test_mode=True, error=""):
    """One row per send attempt - letters_sent is never wiped by a pipeline
    run (unlike matches), so this is the durable record of what was mailed."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO letters_sent
               (full_name, owner_name, parcel_id, template_id, message, lob_letter_id,
                status, expected_delivery_date, preview_url, test_mode, error, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (full_name, owner_name, parcel_id, template_id, message, lob_letter_id,
             status, expected_delivery_date, preview_url, 1 if test_mode else 0, error,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def add_pdf_template(company_name, template_name, filename):
    """filename is the on-disk name under dashboard.py's pdf_templates/ dir,
    not a path - the caller owns where the actual bytes live."""
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO pdf_templates (company_name, template_name, filename, uploaded_at)
               VALUES (?, ?, ?, ?)""",
            (company_name, template_name, filename, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return cursor.lastrowid


def _row_to_pdf_template(row):
    try:
        field_positions = json.loads(row[4]) if row[4] else {}
    except (ValueError, TypeError):
        field_positions = {}
    return {
        "id": row[0], "companyName": row[1], "templateName": row[2], "filename": row[3],
        "fieldPositions": field_positions, "uploadedAt": row[5],
    }


def get_pdf_templates():
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, company_name, template_name, filename, field_positions, uploaded_at
               FROM pdf_templates ORDER BY id DESC"""
        ).fetchall()
    return [_row_to_pdf_template(r) for r in rows]


def get_pdf_template(template_id):
    with connect() as conn:
        row = conn.execute(
            """SELECT id, company_name, template_name, filename, field_positions, uploaded_at
               FROM pdf_templates WHERE id = ?""",
            (template_id,),
        ).fetchone()
    return _row_to_pdf_template(row) if row else None


def set_pdf_template_field_position(template_id, field, x_ratio, y_ratio):
    """Records where on the page (as a 0-1 ratio of width/height, not
    absolute points) a merge field should be stamped for this PDF template.
    Ratio-based so it's independent of whatever size the click-to-place
    preview image was rendered at."""
    template = get_pdf_template(template_id)
    if not template:
        return False
    positions = template["fieldPositions"]
    positions[field] = {"x": x_ratio, "y": y_ratio}
    with connect() as conn:
        conn.execute(
            "UPDATE pdf_templates SET field_positions = ? WHERE id = ?",
            (json.dumps(positions), template_id),
        )
    return True


def delete_pdf_template(template_id):
    with connect() as conn:
        conn.execute("DELETE FROM pdf_templates WHERE id = ?", (template_id,))


def get_latest_letters_sent():
    """Latest send attempt per (full_name, parcel_id), keyed the same way
    dashboard.py keys its merged confirmed-leads list."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT full_name, parcel_id, template_id, lob_letter_id, status,
                      expected_delivery_date, preview_url, test_mode, error, sent_at
               FROM letters_sent ORDER BY id"""
        ).fetchall()
    latest = {}
    for row in rows:
        full_name, parcel_id = row[0], row[1]
        latest[(full_name, parcel_id)] = {
            "templateId": row[2],
            "lobLetterId": row[3],
            "status": row[4],
            "expectedDeliveryDate": row[5],
            "previewUrl": row[6],
            "testMode": bool(row[7]),
            "error": row[8],
            "sentAt": row[9],
        }
    return latest
