"""Central mailing-state DB shared by every county's confirmed-leads view in
dashboard.py - deliberately separate from each county's own pipeline_cache.db
(SLKobi.py/SLK$.py/DC.py/pricer.py's obituary-scraping and matching state,
which stays put per county folder). PDF templates and send history are the
same across counties (one business, one set of letter designs, one Lob
account), so they live here once instead of duplicated per county.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mailing.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS letters_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    county TEXT,
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
    field_positions TEXT,
    uploaded_at TEXT
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


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


def record_letter_sent(county, full_name, owner_name, parcel_id, template_id, message,
                        lob_letter_id="", status="", expected_delivery_date="",
                        preview_url="", test_mode=True, error=""):
    """One row per send attempt - letters_sent is never wiped, so this is the
    durable record of what was mailed. Keyed for lookups by
    (county, full_name, parcel_id) since parcel numbering isn't guaranteed
    unique across different counties' assessor systems."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO letters_sent
               (county, full_name, owner_name, parcel_id, template_id, message, lob_letter_id,
                status, expected_delivery_date, preview_url, test_mode, error, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (county, full_name, owner_name, parcel_id, template_id, message, lob_letter_id,
             status, expected_delivery_date, preview_url, 1 if test_mode else 0, error,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def get_latest_letters_sent():
    """Latest send attempt per (county, full_name, parcel_id), keyed the same
    way dashboard.py keys its merged, multi-county confirmed-leads list."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT county, full_name, parcel_id, template_id, lob_letter_id, status,
                      expected_delivery_date, preview_url, test_mode, error, sent_at
               FROM letters_sent ORDER BY id"""
        ).fetchall()
    latest = {}
    for row in rows:
        county, full_name, parcel_id = row[0], row[1], row[2]
        latest[(county, full_name, parcel_id)] = {
            "templateId": row[3],
            "lobLetterId": row[4],
            "status": row[5],
            "expectedDeliveryDate": row[6],
            "previewUrl": row[7],
            "testMode": bool(row[8]),
            "error": row[9],
            "sentAt": row[10],
        }
    return latest
