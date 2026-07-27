import html
import http.server
import json
import os
import re
import uuid
import webbrowser
from datetime import datetime

import pandas as pd

import mailer
import pdf_merge
import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "dashboard.html")
PDF_TEMPLATES_DIR = os.path.join(SCRIPT_DIR, "pdf_templates")
PORT = 8765

# Preset letter layouts. {{merge_fields}} are substituted client-side (so the
# user can preview/edit the merged text before sending) and again
# server-side in build_letter_html() as a safety net in case the client sent
# an un-merged body.
LETTER_TEMPLATES = [
    {
        "id": "probate-inquiry",
        "name": "Probate Inquiry",
        "body": (
            "Dear {{owner_name}},\n\n"
            "I'm reaching out regarding the property at {{address}}, {{city}}, {{state}} {{zip}}, "
            "which I understand may be part of an estate following the passing of {{full_name}}. "
            "Please accept my condolences.\n\n"
            "If you are the person handling this property, I'd like to discuss options that might "
            "make this process easier for you, including a straightforward cash purchase with no "
            "repairs or listing required.\n\n"
            "Feel free to reach out whenever is convenient - there's no obligation.\n\n"
            "Sincerely,"
        ),
    },
    {
        "id": "cash-offer",
        "name": "Cash Offer",
        "body": (
            "Dear {{owner_name}},\n\n"
            "I'm interested in purchasing the property at {{address}}, {{city}}, {{state}} {{zip}} "
            "for cash, as-is, on a timeline that works for you.\n\n"
            "No repairs, no showings, no realtor commissions - just a simple, fast closing if it's "
            "something you'd consider.\n\n"
            "I'd welcome the chance to talk whenever you're ready.\n\n"
            "Sincerely,"
        ),
    },
]

LETTER_TEMPLATES_BY_ID = {t["id"]: t for t in LETTER_TEMPLATES}


def build_letter_html(message):
    """Wraps the (already merge-field-substituted) letter body in a minimal
    HTML page for Lob's `file` field. Lob renders supplied HTML to the
    letter's printed page size."""
    paragraphs = "".join(
        f"<p>{html.escape(line)}</p>" for line in message.split("\n\n") if line.strip()
    )
    return f"""<html>
<head><meta charset="utf-8"><style>
  body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 12pt; color: #111; margin: 0.75in; }}
  p {{ margin: 0 0 1em; white-space: pre-wrap; }}
</style></head>
<body>{paragraphs}</body>
</html>"""

def _parse_multipart(content_type, body):
    """Minimal multipart/form-data parser (stdlib only, no cgi module -
    that's deprecated for removal). Returns {field_name: str} for text
    fields and {field_name: {"filename": ..., "content": bytes}} for file
    fields. Good enough for the browser-generated upload form this backs;
    not a general-purpose MIME parser."""
    if not content_type or "multipart/form-data" not in content_type:
        return {}
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
    if not boundary:
        return {}

    delimiter = b"--" + boundary.encode("utf-8")
    fields = {}
    for segment in body.split(delimiter):
        segment = segment.strip(b"\r\n")
        if not segment or segment == b"--":
            continue
        if b"\r\n\r\n" not in segment:
            continue
        header_block, content = segment.split(b"\r\n\r\n", 1)
        content = content[:-2] if content.endswith(b"\r\n") else content
        headers = header_block.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]*)"', headers)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', headers)
        if filename_match:
            fields[name] = {"filename": filename_match.group(1), "content": content}
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    return fields


def handle_upload_pdf_template(fields):
    """Stores a company-submitted PDF letter design on disk and records it
    in pipeline_cache.db, so it shows up as a send option alongside the
    built-in merge-field templates."""
    company_name = str(fields.get("companyName", "")).strip()
    template_name = str(fields.get("templateName", "")).strip()
    file_field = fields.get("file")

    if not company_name or not template_name:
        return 400, {"error": "Company name and template name are required."}
    if not isinstance(file_field, dict) or not file_field.get("content"):
        return 400, {"error": "No PDF file was uploaded."}
    if not file_field.get("filename", "").lower().endswith(".pdf"):
        return 400, {"error": "Only PDF files are accepted."}

    os.makedirs(PDF_TEMPLATES_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.pdf"
    with open(os.path.join(PDF_TEMPLATES_DIR, filename), "wb") as f:
        f.write(file_field["content"])

    template_id = pipeline_db.add_pdf_template(company_name, template_name, filename)
    return 200, {"id": template_id, "filename": filename}


def handle_set_pdf_template_field(payload):
    """Records where on the page (as a ratio, not absolute points) the
    owner/recipient name should be stamped for this PDF template - set via
    the click-to-place preview in the admin panel."""
    try:
        template_id = int(payload.get("id"))
        x_ratio = float(payload.get("xRatio"))
        y_ratio = float(payload.get("yRatio"))
    except (TypeError, ValueError):
        return 400, {"error": "Invalid placement data."}
    field = str(payload.get("field", "")).strip()
    if field != "owner_name":
        return 400, {"error": "Unsupported field."}
    if not (0 <= x_ratio <= 1 and 0 <= y_ratio <= 1):
        return 400, {"error": "Position must be within the page."}

    if not pipeline_db.set_pdf_template_field_position(template_id, field, x_ratio, y_ratio):
        return 404, {"error": "Template not found."}
    return 200, {"ok": True}


def handle_delete_pdf_template(payload):
    try:
        template_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return 400, {"error": "Invalid template id."}

    template = pipeline_db.get_pdf_template(template_id)
    if not template:
        return 404, {"error": "Template not found."}

    pipeline_db.delete_pdf_template(template_id)
    try:
        os.remove(os.path.join(PDF_TEMPLATES_DIR, template["filename"]))
    except OSError:
        pass
    return 200, {"ok": True}


# Every county pipeline folder this dashboard aggregates, each generated by
# template_county/new_county.py (or, for slk, hand-built before that wizard
# existed). Add one entry here whenever a new county folder is created - this
# is the one place that needs a manual update; nothing else auto-discovers
# counties, since importing a county's own scripts here isn't safe (e.g.
# uta/geocode.py pulls in selenium, which this dashboard has no reason to need).
COUNTIES = [
    {
        "folder": "slk",
        "label": "Salt Lake County",
        "parcel_url_base": "https://apps.saltlakecounty.gov/assessor/new/valuationInfoExpanded.cfm?parcel_id=",
    },
    {
        "folder": "uta",
        "label": "Utah County",
        "parcel_url_base": "https://www.utahcounty.gov/LandRecords/ValNotice.asp?av_serial=",
    },
]

# Matches DC.py's get_output_path() naming: "2026-07-21.xlsx" or, on a second
# run the same day, "2026-07-21_14-30-05.xlsx".
DATED_XLSX_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_\d{2}-\d{2}-\d{2})?\.xlsx$")


def assessor_url(parcel_id, parcel_url_base):
    digits = "".join(ch for ch in (parcel_id or "") if ch.isdigit())
    return f"{parcel_url_base}{digits}" if digits else ""


def format_date_added(date_str, fmt="%Y-%m-%d %H:%M:%S"):
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str, fmt).strftime("%b %d, %Y")
    except ValueError:
        return date_str


def _clean_cell(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def discover_dated_excel_files(county_dir):
    """Every DC.py export sitting in one county's folder, named
    YYYY-MM-DD[_HH-MM-SS].xlsx."""
    found = []
    for name in os.listdir(county_dir):
        m = DATED_XLSX_PATTERN.match(name)
        if m:
            found.append((m.group(1), os.path.join(county_dir, name)))
    # Sort by actual mtime, not filename - DC.py can regenerate a fresh
    # "{date}.xlsx" (no suffix) after an earlier same-day conflict already
    # pushed an older run out to "{date}_HH-MM-SS.xlsx", so the bare
    # filename isn't reliably the oldest one on disk. A plain filename sort
    # would then let that older, stale suffixed file overwrite the newer
    # one's rows in load_confirmed_leads_from_excel()'s dedup below.
    found.sort(key=lambda pair: os.path.getmtime(pair[1]))
    return found


def load_confirmed_leads_from_excel(county):
    """Confirmed leads are the durable historical record - a county's own
    pipeline_cache.db matches table gets wiped at the start of every
    SLK$.py run, so anything from a previous day only survives in that day's
    dated export. Read every such file in this county's folder and merge them
    into one list, keyed by (full name, parcel id) so a lead re-confirmed on a
    later date overwrites the earlier entry instead of duplicating it."""
    county_dir = os.path.join(SCRIPT_DIR, county["folder"])
    if not os.path.isdir(county_dir):
        return []

    leads_by_key = {}
    for date_str, path in discover_dated_excel_files(county_dir):
        try:
            df = pd.read_excel(path)
        except Exception as e:
            print(f"Skipping {os.path.basename(path)}: {e}")
            continue

        date_display = format_date_added(date_str, fmt="%Y-%m-%d")
        for _, row in df.iterrows():
            full_name = _clean_cell(row.get("Full Name", ""))
            if not full_name:
                continue
            parcel_id = _clean_cell(row.get("Parcel ID", ""))
            matched_keywords_raw = _clean_cell(row.get("Matched Keywords", ""))

            leads_by_key[(full_name, parcel_id)] = {
                "fullName": full_name,
                "ownerName": _clean_cell(row.get("Owner Name", "")),
                "parcelId": parcel_id,
                "assessorUrl": assessor_url(parcel_id, county["parcel_url_base"]),
                "confirmed": True,
                "matchedKeywords": [k.strip() for k in matched_keywords_raw.split(",") if k.strip()],
                "obituaryUrl": _clean_cell(row.get("Obituary URL", "")),
                "propertyValue": _clean_cell(row.get("Property Value", "")),
                "address": _clean_cell(row.get("Address", "")),
                "city": _clean_cell(row.get("City", "")),
                "state": _clean_cell(row.get("State", "")),
                "zip": _clean_cell(row.get("Zip", "")),
                "dateAdded": date_display,
                "dateAddedRaw": date_str,
                "sourceFile": os.path.basename(path),
                "county": county["label"],
                "countyFolder": county["folder"],
            }
    return list(leads_by_key.values())


def load_all_confirmed_leads():
    """Every confirmed lead across every county in COUNTIES, in one flat list."""
    leads = []
    for county in COUNTIES:
        leads.extend(load_confirmed_leads_from_excel(county))
    return leads


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Probate Property Leads</title>
<style>
  :root {
    --navy: #1f2a3d;
    --navy-light: #2e3d57;
    /* Text color for headings/links/numbers - distinct from --navy because
       --navy also backs the always-dark header bar. Overriding --navy alone
       for dark mode would make the header's own white text unreadable. */
    --heading: #1f2a3d;
    --gold: #b8902e;
    --gold-light: #e9c874;
    --cream: #faf6ee;
    --card: #ffffff;
    --ink: #26303f;
    --muted: #6b7688;
    --border: #e7e1d3;
    --confirmed-bg: #eef6ee;
    --confirmed-ink: #2f6b3a;
    --review-bg: #fbf3e4;
    --review-ink: #93691e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--cream);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }
  header.top {
    background: linear-gradient(135deg, var(--navy), var(--navy-light));
    color: #fff;
    padding: 36px 32px 44px;
  }
  .top-inner {
    max-width: 1180px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 16px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .brand .mark {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, var(--gold-light), var(--gold) 70%);
    flex-shrink: 0;
    box-shadow: 0 0 0 4px rgba(255,255,255,0.08);
  }
  .brand h1 {
    margin: 0;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 26px;
    font-weight: 600;
    letter-spacing: 0.2px;
  }
  .brand p {
    margin: 2px 0 0;
    font-size: 13px;
    color: rgba(255,255,255,0.7);
  }
  .generated {
    font-size: 12px;
    color: rgba(255,255,255,0.55);
    text-align: right;
  }

  main {
    max-width: 1180px;
    margin: -26px auto 60px;
    padding: 0 32px;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
    /* Breaks out of main's 1180px max-width so the stat cards stretch
       edge-to-edge across the full viewport instead of being boxed into the
       centered content column like everything else on the page. */
    margin: 0 calc(50% - 50vw) 28px;
    padding: 0 32px;
  }
  .stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
    box-shadow: 0 8px 20px rgba(31,42,61,0.08);
  }
  .stat-card .num {
    font-family: Georgia, serif;
    font-size: 30px;
    font-weight: 700;
    color: var(--heading);
    line-height: 1;
  }
  .stat-card .label {
    margin-top: 6px;
    font-size: 12.5px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .stat-card.confirmed .num { color: var(--confirmed-ink); }

  .panel {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 10px 24px rgba(31,42,61,0.06);
    overflow: hidden;
    margin-bottom: 24px;
  }
  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 18px 22px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .panel-head h2 {
    margin: 0;
    font-family: Georgia, serif;
    font-size: 18px;
    color: var(--heading);
  }
  .panel-head .sub {
    font-size: 12.5px;
    color: var(--muted);
    margin-top: 2px;
  }
  .controls {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .search-box {
    position: relative;
  }
  .search-box input {
    padding: 9px 14px 9px 34px;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 13.5px;
    width: 240px;
    background: var(--cream);
    color: var(--ink);
  }
  .search-box::before {
    content: "";
    position: absolute;
    left: 13px;
    top: 50%;
    width: 10px;
    height: 10px;
    border: 2px solid var(--muted);
    border-radius: 50%;
    transform: translateY(-60%);
  }
  .search-box::after {
    content: "";
    position: absolute;
    left: 21px;
    top: 62%;
    width: 6px;
    height: 2px;
    background: var(--muted);
    transform: rotate(45deg);
  }
  button.ghost {
    border: 1px solid var(--border);
    background: var(--cream);
    color: var(--heading);
    padding: 8px 14px;
    border-radius: 999px;
    font-size: 13px;
    cursor: pointer;
  }
  button.ghost:hover { background: #f2ecdd; }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left;
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    padding: 10px 22px;
    border-bottom: 1px solid var(--border);
    background: #fbf9f4;
  }
  thead th[data-key] {
    cursor: pointer;
    user-select: none;
    position: relative;
    padding-right: 26px;
  }
  thead th[data-key]:hover { color: var(--heading, var(--navy)); }
  thead th[data-key]::after {
    content: "\\2195";
    position: absolute;
    right: 9px;
    opacity: 0.35;
    font-size: 11px;
  }
  thead th[data-key].sort-asc::after { content: "\\2191"; opacity: 1; color: var(--gold); }
  thead th[data-key].sort-desc::after { content: "\\2193"; opacity: 1; color: var(--gold); }
  tbody td {
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
    vertical-align: top;
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: #fbf9f2; }
  .name-cell .deceased { font-weight: 600; color: var(--heading); }
  .name-cell .owner { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
  .chip {
    display: inline-block;
    background: var(--gold-light);
    color: #6b4f0a;
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11.5px;
    margin: 1px 3px 1px 0;
    font-weight: 600;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
  }
  .badge.confirmed { background: var(--confirmed-bg); color: var(--confirmed-ink); }
  .badge.review { background: var(--review-bg); color: var(--review-ink); }
  a.link {
    color: var(--heading);
    text-decoration: none;
    font-size: 13px;
    border-bottom: 1px solid var(--gold);
    padding-bottom: 1px;
  }
  a.link:hover { color: var(--gold); }
  .links-cell { display: flex; flex-direction: column; gap: 4px; }
  .mailed-cell {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .mailed-cell input[type="checkbox"] {
    width: 17px;
    height: 17px;
    accent-color: var(--confirmed-ink);
    cursor: pointer;
  }
  .mailed-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
  }
  .mailed-label.yes { color: var(--confirmed-ink); }
  .empty {
    padding: 40px 22px;
    text-align: center;
    color: var(--muted);
    font-size: 14px;
  }

  .letter-cell { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
  .badge.sent { background: var(--confirmed-bg); color: var(--confirmed-ink); }
  .badge.failed { background: #fbe4e4; color: #a13a3a; }
  .badge.not-sent { background: var(--review-bg); color: var(--review-ink); }
  button.send-letter-btn {
    border: 1px solid var(--gold);
    background: transparent;
    color: var(--heading);
    padding: 5px 11px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  button.send-letter-btn:hover { background: var(--gold-light); }

  .modal-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(20,24,31,0.55);
    align-items: center;
    justify-content: center;
    padding: 20px;
    z-index: 100;
  }
  .modal-backdrop.open { display: flex; }
  .modal-card {
    background: var(--card);
    border-radius: 14px;
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0,0,0,0.35);
  }
  .modal-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 18px 22px;
    border-bottom: 1px solid var(--border);
  }
  .modal-head h3 { margin: 0; font-family: Georgia, serif; color: var(--heading); }
  .modal-close {
    border: none;
    background: none;
    font-size: 22px;
    line-height: 1;
    color: var(--muted);
    cursor: pointer;
  }
  .modal-body {
    padding: 18px 22px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .modal-body label { font-size: 12px; font-weight: 600; color: var(--muted); margin-top: 8px; }
  .modal-body select, .modal-body textarea {
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 13.5px;
    background: var(--cream);
    color: var(--ink);
    font-family: inherit;
    resize: vertical;
  }
  .letter-warning {
    display: none;
    font-size: 12.5px;
    color: #a13a3a;
    background: #fbe4e4;
    border-radius: 8px;
    padding: 8px 12px;
  }
  .letter-warning.show { display: block; }
  .letter-result {
    display: none;
    font-size: 12.5px;
    border-radius: 8px;
    padding: 8px 12px;
  }
  .letter-result.show.ok { display: block; background: var(--confirmed-bg); color: var(--confirmed-ink); }
  .letter-result.show.error { display: block; background: #fbe4e4; color: #a13a3a; }
  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    padding: 16px 22px;
    border-top: 1px solid var(--border);
  }
  button.primary {
    border: none;
    background: var(--navy);
    color: #fff;
    padding: 9px 18px;
    border-radius: 999px;
    font-size: 13.5px;
    font-weight: 600;
    cursor: pointer;
  }
  button.primary:hover { background: var(--navy-light); }
  button.primary:disabled { opacity: 0.5; cursor: not-allowed; }

  .bulk-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 260px;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 4px;
  }
  .bulk-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 6px;
    font-size: 13px;
  }
  .bulk-row .name { flex: 1; }
  .bulk-row .addr { color: var(--muted); font-size: 12px; }
  .bulk-row .status { font-size: 11.5px; color: var(--muted); min-width: 90px; text-align: right; }
  .bulk-row.sending .status { color: var(--gold); }
  .bulk-row.ok .status { color: var(--confirmed-ink); }
  .bulk-row.err .status { color: #a13a3a; }
  .bulk-empty { padding: 16px; text-align: center; color: var(--muted); font-size: 13px; }

  footer {
    max-width: 1180px;
    margin: 0 auto 40px;
    padding: 0 32px;
    color: var(--muted);
    font-size: 12px;
    text-align: center;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --cream: #14181f;
      --card: #1c212b;
      --ink: #e7e9ee;
      --muted: #93a0b4;
      --border: #2a3140;
      --heading: #dbe2ee;
      --confirmed-bg: #17281c;
      --confirmed-ink: #7bc98a;
      --review-bg: #2b220f;
      --review-ink: #e0b563;
    }
    thead th { background: #191e27; }
    tbody tr:hover { background: #202634; }
    .search-box input { background: #14181f; color: var(--ink); }
    button.ghost { background: #14181f; }
    button.ghost:hover { background: #232a38; }
  }

  @media print {
    header.top { background: var(--navy) !important; -webkit-print-color-adjust: exact; }
    .controls { display: none; }
    .stats { grid-template-columns: repeat(2, 1fr); }
  }

  @media (max-width: 720px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    thead { display: none; }
    tbody td { display: block; padding: 6px 18px; border-bottom: none; }
    tbody tr { border-bottom: 1px solid var(--border); padding: 10px 0; display: block; }
  }

  .login-screen {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--navy), var(--navy-light));
    padding: 24px;
  }
  .login-card {
    background: var(--card);
    border-radius: 16px;
    padding: 36px 34px;
    width: 100%;
    max-width: 380px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.3);
  }
  .login-card .mark {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, var(--gold-light), var(--gold) 70%);
    margin: 0 auto 16px;
  }
  .login-card h1 {
    margin: 0 0 4px;
    font-family: Georgia, serif;
    font-size: 21px;
    color: var(--heading, var(--navy));
    text-align: center;
  }
  .login-card p.sub {
    margin: 0 0 24px;
    font-size: 13px;
    color: var(--muted);
    text-align: center;
  }
  .login-card label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 6px;
    margin-top: 16px;
  }
  .login-card input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    background: var(--cream);
    color: var(--ink);
  }
  .login-card button {
    width: 100%;
    margin-top: 22px;
    padding: 11px;
    border: none;
    border-radius: 999px;
    background: var(--navy);
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .login-card button:hover { background: var(--navy-light); }
  .login-error {
    display: none;
    margin-top: 14px;
    font-size: 12.5px;
    color: #b3261e;
    text-align: center;
  }
  .login-error.show { display: block; }
</style>
</head>
<body>

<div class="login-screen" id="login-screen">
  <form class="login-card" id="login-form">
    <div class="mark"></div>
    <h1>Probate Property Leads</h1>
    <p class="sub">Sign in to view today&rsquo;s report</p>
    <label for="login-email">Email</label>
    <input type="email" id="login-email" autocomplete="username" required>
    <label for="login-password">Password</label>
    <input type="password" id="login-password" autocomplete="current-password" required>
    <button type="submit">Sign In</button>
    <div class="login-error" id="login-error">Incorrect email or password.</div>
  </form>
</div>

<div id="app-content" style="display:none;">

<header class="top">
  <div class="top-inner">
    <div class="brand">
      <div class="mark"></div>
      <div>
        <h1>Probate Property Leads</h1>
        <p>__COUNTIES_SUBTITLE__ &middot; deceased-owner match report</p>
      </div>
    </div>
    <div class="generated">Generated __GENERATED_AT__</div>
  </div>
</header>

<main>
  <div class="panel">
    <div class="panel-head">
      <div>
        <h2>PDF Letter Templates</h2>
        <div class="sub">Upload a company&rsquo;s pre-designed letter PDF to send as-is via Lob</div>
      </div>
    </div>
    <form id="pdf-upload-form" style="display:flex; flex-wrap:wrap; gap:12px; align-items:flex-end; padding:18px 22px; border-bottom:1px solid var(--border);">
      <div style="flex:1; min-width:160px;">
        <label for="pdf-company-name" style="display:block; font-size:12px; font-weight:600; color:var(--muted); margin-bottom:6px;">Company Name</label>
        <input type="text" id="pdf-company-name" required style="width:100%; padding:9px 12px; border:1px solid var(--border); border-radius:8px; background:var(--cream); color:var(--ink); font-size:13.5px;">
      </div>
      <div style="flex:1; min-width:160px;">
        <label for="pdf-template-name" style="display:block; font-size:12px; font-weight:600; color:var(--muted); margin-bottom:6px;">Template Name</label>
        <input type="text" id="pdf-template-name" required style="width:100%; padding:9px 12px; border:1px solid var(--border); border-radius:8px; background:var(--cream); color:var(--ink); font-size:13.5px;">
      </div>
      <div>
        <label for="pdf-file-input" style="display:block; font-size:12px; font-weight:600; color:var(--muted); margin-bottom:6px;">PDF File</label>
        <input type="file" id="pdf-file-input" accept="application/pdf" required>
      </div>
      <button type="submit" class="ghost">Upload</button>
      <div class="letter-result" id="pdf-upload-result" style="flex-basis:100%;"></div>
    </form>
    <table>
      <thead>
        <tr><th>Company</th><th>Template</th><th>Uploaded</th><th>Preview</th><th>Name Placement</th><th></th></tr>
      </thead>
      <tbody id="pdf-templates-body"></tbody>
    </table>
    <div class="empty" id="pdf-templates-empty" style="display:none;">No PDF templates uploaded yet.</div>
  </div>

  <div class="stats">
    <div class="stat-card confirmed">
      <div class="num">__CONFIRMED_COUNT__</div>
      <div class="label">Real Leads Generated</div>
    </div>
    <div class="stat-card">
      <div class="num">__LEAD_PURSUED_RATE__</div>
      <div class="label">Lead Pursued Rate</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div>
        <h2>Confirmed Leads</h2>
        <div class="sub">Property owner name corroborated by the deceased&rsquo;s obituary</div>
      </div>
      <div class="controls">
        <div class="search-box"><input id="search-confirmed" type="text" placeholder="Search name, owner, parcel&hellip;"></div>
        <select id="county-filter" class="ghost"><option value="">All Counties</option></select>
        <button class="ghost" id="bulk-send-open">Send This Week&rsquo;s Letters</button>
        <button class="ghost" id="export-confirmed">Export CSV</button>
        <button class="ghost" onclick="window.print()">Print</button>
      </div>
    </div>
    <table id="confirmed-table">
      <thead>
        <tr>
          <th data-key="county">County</th>
          <th data-key="fullName">Deceased / Owner Record</th>
          <th data-key="parcelId">Parcel</th>
          <th data-key="address">Address</th>
          <th data-key="city">City</th>
          <th data-key="state">State</th>
          <th data-key="propertyValue">Property Value</th>
          <th data-key="dateAdded">Date Added</th>
          <th data-key="letterStatus">Letter</th>
          <th data-key="mailedIn">Mailed In</th>
          <th data-key="dateMailedIn">Date Mailed In</th>
        </tr>
      </thead>
      <tbody id="confirmed-body"></tbody>
    </table>
    <div class="empty" id="confirmed-empty" style="display:none;">No confirmed leads yet &mdash; run the pipeline to populate this list.</div>
  </div>
</main>

<div class="modal-backdrop" id="letter-modal-backdrop">
  <div class="modal-card">
    <div class="modal-head">
      <h3 id="letter-modal-title">Send Letter</h3>
      <button type="button" class="modal-close" id="letter-modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <label for="letter-template-select">Template</label>
      <select id="letter-template-select"></select>

      <label for="letter-message">Message</label>
      <textarea id="letter-message" rows="12"></textarea>

      <div class="letter-warning" id="letter-address-warning">
        This lead is missing City or Zip &mdash; the address is incomplete and can&rsquo;t be mailed until that&rsquo;s filled in.
      </div>
      <div class="letter-result" id="letter-result"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="ghost" id="letter-cancel">Cancel</button>
      <button type="button" class="primary" id="letter-send">Send Letter (Test Mode)</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="bulk-modal-backdrop">
  <div class="modal-card">
    <div class="modal-head">
      <h3>Send This Week&rsquo;s Letters</h3>
      <button type="button" class="modal-close" id="bulk-modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <label for="bulk-template-select">Template (applied to every letter below, merged per lead)</label>
      <select id="bulk-template-select"></select>

      <label>Leads added in the last 7 days without a letter on record</label>
      <div class="bulk-list" id="bulk-list"></div>

      <div class="letter-result" id="bulk-result"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="ghost" id="bulk-cancel">Cancel</button>
      <button type="button" class="primary" id="bulk-send" disabled>Send Letters (Test Mode)</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="placement-modal-backdrop">
  <div class="modal-card" style="max-width:640px;">
    <div class="modal-head">
      <h3>Place Recipient Name</h3>
      <button type="button" class="modal-close" id="placement-modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <p style="margin:0; font-size:12.5px; color:var(--muted);">Click on the letter where the recipient&rsquo;s name should be stamped when this template is sent. Skip to send this template as-is, unpersonalized, for every recipient.</p>
      <div id="placement-preview-wrap" style="position:relative; border:1px solid var(--border); border-radius:8px; overflow:hidden; cursor:crosshair; line-height:0;">
        <img id="placement-preview-img" style="display:block; width:100%; height:auto;" alt="Letter preview">
        <div id="placement-marker" style="display:none; position:absolute; width:10px; height:10px; margin:-5px 0 0 -5px; border-radius:50%; background:var(--gold); border:2px solid #fff; box-shadow:0 0 0 1px rgba(0,0,0,0.35);"></div>
      </div>
      <div class="letter-result" id="placement-result"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="ghost" id="placement-skip">Skip (send as static PDF)</button>
      <button type="button" class="primary" id="placement-save" disabled>Save Position</button>
    </div>
  </div>
</div>

<footer>
  Generated locally from pipeline_cache.db &middot; parcel and obituary links open the original source for verification before contacting anyone.
</footer>

<script id="lead-data" type="application/json">__LEAD_DATA_JSON__</script>
<script>
  const leadData = JSON.parse(document.getElementById('lead-data').textContent);
  const confirmed = leadData.confirmed;
  const templates = leadData.templates;
  const pdfTemplates = leadData.pdfTemplates || [];

  // Looks up a template by id across both the built-in merge-field
  // templates and company-uploaded PDF templates, tagging which kind it is
  // so callers (send handlers, the message box) can branch on it.
  function findTemplate(id) {
    const t = templates.find(x => x.id === id);
    if (t) return Object.assign({ isPdf: false }, t);
    const p = pdfTemplates.find(x => x.id === id);
    if (p) return Object.assign({ isPdf: true }, p);
    return null;
  }

  function populateTemplateSelect(selectEl) {
    selectEl.innerHTML = '';
    if (templates.length) {
      const grp = document.createElement('optgroup');
      grp.label = 'Message Templates';
      templates.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t.id;
        opt.textContent = t.name;
        grp.appendChild(opt);
      });
      selectEl.appendChild(grp);
    }
    if (pdfTemplates.length) {
      const grp = document.createElement('optgroup');
      grp.label = 'Company PDF Designs';
      pdfTemplates.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t.id;
        opt.textContent = t.name;
        grp.appendChild(opt);
      });
      selectEl.appendChild(grp);
    }
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function linkEl(href, label) {
    const a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'link';
    a.textContent = label;
    return a;
  }

  // "Mailed In" tracking has nowhere server-side to live (this is a static
  // file with no backend), so it's stored in this browser's localStorage,
  // keyed by parcel id (falling back to name+owner for leads with no
  // parcel). It survives reloads and future regenerations of this report,
  // but only in this browser - it won't follow the file elsewhere.
  const MAILED_KEY_PREFIX = 'probateLeadsMailed:';

  function mailedKey(lead) {
    return MAILED_KEY_PREFIX + (lead.parcelId || `${lead.fullName}|${lead.ownerName}`);
  }

  function getMailedState(lead) {
    try {
      const raw = localStorage.getItem(mailedKey(lead));
      if (!raw) return { mailed: false, date: '' };
      return JSON.parse(raw);
    } catch (e) {
      return { mailed: false, date: '' };
    }
  }

  function setMailedState(lead, mailed) {
    const now = Date.now();
    const date = mailed
      ? new Date(now).toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' })
      : '';
    localStorage.setItem(mailedKey(lead), JSON.stringify({ mailed, date, timestamp: mailed ? now : 0 }));
    return date;
  }

  const MERGE_FIELD_MAP = {
    full_name: 'fullName', owner_name: 'ownerName', address: 'address',
    city: 'city', state: 'state', zip: 'zip',
  };

  function mergeTemplate(body, lead) {
    return body.replace(/\{\{(\w+)\}\}/g, (match, field) => {
      const leadKey = MERGE_FIELD_MAP[field];
      return leadKey ? (lead[leadKey] || '') : match;
    });
  }

  function hasCompleteAddress(lead) {
    return Boolean(lead.address && lead.city && lead.state && lead.zip);
  }

  function letterStatusLabel(lead) {
    if (!lead.letter) return 'not-sent';
    return lead.letter.status === 'sent' ? 'sent' : 'failed';
  }

  function letterCell(lead) {
    const td = el('td', 'letter-cell');
    const label = letterStatusLabel(lead);
    const badgeText = label === 'sent' ? 'Sent' : label === 'failed' ? 'Failed' : 'Not Sent';
    const badge = el('span', `badge ${label}`, badgeText);
    td.appendChild(badge);
    if (lead.letter && lead.letter.sentAt) {
      td.appendChild(el('span', 'owner', lead.letter.sentAt));
    }
    const btn = el('button', 'send-letter-btn', lead.letter ? 'Resend Letter' : 'Send Letter');
    btn.type = 'button';
    btn.addEventListener('click', () => openLetterModal(lead));
    td.appendChild(btn);
    return td;
  }

  // --- Send Letter modal ---
  let activeLead = null;
  const modalBackdrop = document.getElementById('letter-modal-backdrop');
  const templateSelect = document.getElementById('letter-template-select');
  const messageBox = document.getElementById('letter-message');
  const addressWarning = document.getElementById('letter-address-warning');
  const letterResult = document.getElementById('letter-result');
  const sendBtn = document.getElementById('letter-send');

  populateTemplateSelect(templateSelect);

  function fillMessageFromTemplate() {
    const template = findTemplate(templateSelect.value);
    if (!template) return;
    if (template.isPdf) {
      messageBox.value = '';
      messageBox.disabled = true;
      messageBox.placeholder = "This template sends the company's uploaded PDF design as-is - nothing to edit here.";
    } else {
      messageBox.disabled = false;
      if (activeLead) messageBox.value = mergeTemplate(template.body, activeLead);
    }
  }

  function openLetterModal(lead) {
    activeLead = lead;
    letterResult.classList.remove('show', 'ok', 'error');
    letterResult.textContent = '';
    templateSelect.value = templates.length ? templates[0].id : pdfTemplates[0].id;
    fillMessageFromTemplate();
    const complete = hasCompleteAddress(lead);
    addressWarning.classList.toggle('show', !complete);
    sendBtn.disabled = !complete;
    modalBackdrop.classList.add('open');
  }

  function closeLetterModal() {
    modalBackdrop.classList.remove('open');
    activeLead = null;
  }

  templateSelect.addEventListener('change', fillMessageFromTemplate);
  document.getElementById('letter-modal-close').addEventListener('click', closeLetterModal);
  document.getElementById('letter-cancel').addEventListener('click', closeLetterModal);
  modalBackdrop.addEventListener('click', (e) => {
    if (e.target === modalBackdrop) closeLetterModal();
  });

  // Shared by the single-lead modal and the bulk-send modal. Posts one
  // letter and, on success, stamps lead.letter so both modals' callers can
  // just re-render from the lead objects afterward.
  async function sendLetterForLead(lead, templateId, message) {
    const response = await fetch('/api/send-letter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        fullName: lead.fullName,
        ownerName: lead.ownerName,
        parcelId: lead.parcelId,
        county: lead.countyFolder,
        address: lead.address,
        city: lead.city,
        state: lead.state,
        zip: lead.zip,
        templateId,
        message,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || 'Failed to send letter.');
    }
    lead.letter = {
      status: 'sent',
      lobLetterId: data.lobLetterId,
      expectedDeliveryDate: data.expectedDeliveryDate,
      testMode: data.testMode,
      sentAt: new Date().toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' }),
    };
    return data;
  }

  sendBtn.addEventListener('click', async () => {
    if (!activeLead || !hasCompleteAddress(activeLead)) return;
    sendBtn.disabled = true;
    sendBtn.textContent = 'Sending…';
    letterResult.classList.remove('show', 'ok', 'error');

    try {
      const data = await sendLetterForLead(activeLead, templateSelect.value, messageBox.value);
      letterResult.classList.add('show', 'ok');
      letterResult.textContent = data.testMode
        ? `Sent in Lob test mode (no real letter mailed). Expected delivery: ${data.expectedDeliveryDate || 'n/a'}.`
        : `Sent. Expected delivery: ${data.expectedDeliveryDate || 'n/a'}.`;
      repaintConfirmed();
      setTimeout(closeLetterModal, 1400);
    } catch (err) {
      letterResult.classList.add('show', 'error');
      letterResult.textContent = err.message;
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = 'Send Letter (Test Mode)';
    }
  });

  // --- Bulk "Send This Week's Letters" modal ---
  const bulkBackdrop = document.getElementById('bulk-modal-backdrop');
  const bulkTemplateSelect = document.getElementById('bulk-template-select');
  const bulkList = document.getElementById('bulk-list');
  const bulkResult = document.getElementById('bulk-result');
  const bulkSendBtn = document.getElementById('bulk-send');

  populateTemplateSelect(bulkTemplateSelect);

  function isWithinLastNDays(dateAddedRaw, days) {
    if (!dateAddedRaw) return false;
    const then = new Date(dateAddedRaw + 'T00:00:00');
    if (isNaN(then)) return false;
    const cutoff = new Date();
    cutoff.setHours(0, 0, 0, 0);
    cutoff.setDate(cutoff.getDate() - days);
    return then >= cutoff;
  }

  function bulkEligibleLeads() {
    return confirmed.filter(l =>
      isWithinLastNDays(l.dateAddedRaw, 7) &&
      hasCompleteAddress(l) &&
      letterStatusLabel(l) !== 'sent'
    );
  }

  let bulkSelection = new Map(); // lead -> checkbox element

  function renderBulkList() {
    bulkList.innerHTML = '';
    bulkSelection = new Map();
    const eligible = bulkEligibleLeads();
    if (!eligible.length) {
      bulkList.appendChild(el('div', 'bulk-empty', 'No leads from the last 7 days are waiting on a letter.'));
      bulkSendBtn.disabled = true;
      return;
    }
    eligible.forEach(lead => {
      const row = el('div', 'bulk-row');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = true;
      checkbox.addEventListener('change', updateBulkSendState);
      row.appendChild(checkbox);
      row.appendChild(el('span', 'name', `${lead.fullName} (${lead.county})`));
      row.appendChild(el('span', 'addr', `${lead.address}, ${lead.city} ${lead.zip}`));
      row.appendChild(el('span', 'status', ''));
      bulkList.appendChild(row);
      bulkSelection.set(lead, row);
    });
    updateBulkSendState();
  }

  function updateBulkSendState() {
    const anyChecked = Array.from(bulkSelection.values()).some(row => row.querySelector('input').checked);
    bulkSendBtn.disabled = !anyChecked;
  }

  function openBulkModal() {
    bulkResult.classList.remove('show', 'ok', 'error');
    bulkResult.textContent = '';
    bulkTemplateSelect.value = templates.length ? templates[0].id : pdfTemplates[0].id;
    renderBulkList();
    bulkBackdrop.classList.add('open');
  }

  function closeBulkModal() {
    bulkBackdrop.classList.remove('open');
  }

  document.getElementById('bulk-send-open').addEventListener('click', openBulkModal);
  document.getElementById('bulk-modal-close').addEventListener('click', closeBulkModal);
  document.getElementById('bulk-cancel').addEventListener('click', closeBulkModal);
  bulkBackdrop.addEventListener('click', (e) => {
    if (e.target === bulkBackdrop) closeBulkModal();
  });

  bulkSendBtn.addEventListener('click', async () => {
    const template = findTemplate(bulkTemplateSelect.value);
    if (!template) return;
    const jobs = Array.from(bulkSelection.entries()).filter(([, row]) => row.querySelector('input').checked);
    if (!jobs.length) return;

    bulkSendBtn.disabled = true;
    bulkTemplateSelect.disabled = true;
    bulkResult.classList.remove('show', 'ok', 'error');
    let sentCount = 0;
    const failures = [];

    for (const [lead, row] of jobs) {
      row.classList.add('sending');
      row.querySelector('.status').textContent = 'Sending…';
      try {
        await sendLetterForLead(lead, template.id, template.isPdf ? '' : mergeTemplate(template.body, lead));
        row.classList.remove('sending');
        row.classList.add('ok');
        row.querySelector('.status').textContent = 'Sent';
        sentCount += 1;
      } catch (err) {
        row.classList.remove('sending');
        row.classList.add('err');
        row.querySelector('.status').textContent = 'Failed';
        failures.push(`${lead.fullName}: ${err.message}`);
      }
    }

    bulkResult.classList.add('show', failures.length ? 'error' : 'ok');
    bulkResult.textContent = failures.length
      ? `Sent ${sentCount}, failed ${failures.length}. ${failures.join(' | ')}`
      : `Sent all ${sentCount} letters (Lob test mode).`;
    repaintConfirmed();
    bulkTemplateSelect.disabled = false;
    updateBulkSendState();
  });

  function renderRow(lead) {
    const tr = document.createElement('tr');

    tr.appendChild(el('td', null, lead.county || '—'));

    const nameCell = el('td', 'name-cell');
    nameCell.appendChild(el('div', 'deceased', lead.fullName));
    nameCell.appendChild(el('div', 'owner', lead.ownerName));
    tr.appendChild(nameCell);

    const parcelCell = el('td');
    if (lead.assessorUrl) {
      parcelCell.appendChild(linkEl(lead.assessorUrl, lead.parcelId || 'View parcel'));
    } else {
      parcelCell.appendChild(el('span', null, lead.parcelId || '—'));
    }
    tr.appendChild(parcelCell);

    tr.appendChild(el('td', null, lead.address || '—'));
    tr.appendChild(el('td', null, lead.city || '—'));
    tr.appendChild(el('td', null, lead.state || '—'));
    tr.appendChild(el('td', null, lead.propertyValue || '—'));
    tr.appendChild(el('td', null, lead.dateAdded || '—'));

    tr.appendChild(letterCell(lead));

    const mailedState = getMailedState(lead);
    const mailedCell = el('td', 'mailed-cell');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = mailedState.mailed;
    const mailedLabel = el('span', 'mailed-label' + (mailedState.mailed ? ' yes' : ''), mailedState.mailed ? 'Yes' : 'No');
    const dateMailedCell = el('td', null, mailedState.date || '—');

    checkbox.addEventListener('change', () => {
      const newDate = setMailedState(lead, checkbox.checked);
      mailedLabel.textContent = checkbox.checked ? 'Yes' : 'No';
      mailedLabel.className = 'mailed-label' + (checkbox.checked ? ' yes' : '');
      dateMailedCell.textContent = newDate || '—';
    });

    mailedCell.appendChild(checkbox);
    mailedCell.appendChild(mailedLabel);
    tr.appendChild(mailedCell);
    tr.appendChild(dateMailedCell);

    return tr;
  }

  function paint(list, bodyId, emptyId) {
    const body = document.getElementById(bodyId);
    const empty = document.getElementById(emptyId);
    body.innerHTML = '';
    if (!list.length) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    list.forEach(l => body.appendChild(renderRow(l)));
  }

  function matches(lead, q) {
    if (!q) return true;
    q = q.toLowerCase();
    return lead.fullName.toLowerCase().includes(q) ||
           lead.ownerName.toLowerCase().includes(q) ||
           lead.parcelId.toLowerCase().includes(q) ||
           lead.address.toLowerCase().includes(q) ||
           lead.city.toLowerCase().includes(q) ||
           lead.zip.toLowerCase().includes(q) ||
           lead.county.toLowerCase().includes(q);
  }

  // Column sorting. Most columns sort on a plain lead field, but "Mailed In"
  // and "Date Mailed In" live in localStorage rather than the lead object,
  // so they're computed fresh at sort time.
  function sortValue(lead, key) {
    switch (key) {
      case 'mailedIn':
        return getMailedState(lead).mailed ? 1 : 0;
      case 'dateMailedIn':
        return getMailedState(lead).timestamp || 0;
      case 'dateAdded':
        return lead.dateAddedRaw || '';
      case 'letterStatus':
        return letterStatusLabel(lead);
      case 'propertyValue': {
        const num = parseFloat((lead.propertyValue || '').replace(/[^0-9.]/g, ''));
        return isNaN(num) ? -1 : num;
      }
      default:
        return (lead[key] || '').toString().toLowerCase();
    }
  }

  const confirmedState = { query: '', countyFolder: '', sortKey: null, sortDir: 'asc' };

  function applyState(list, state) {
    let result = state.countyFolder ? list.filter(l => l.countyFolder === state.countyFolder) : list.slice();
    if (state.query) result = result.filter(l => matches(l, state.query));
    if (state.sortKey) {
      result.sort((a, b) => {
        const va = sortValue(a, state.sortKey);
        const vb = sortValue(b, state.sortKey);
        if (va < vb) return state.sortDir === 'asc' ? -1 : 1;
        if (va > vb) return state.sortDir === 'asc' ? 1 : -1;
        return 0;
      });
    }
    return result;
  }

  function updateSortIndicators(tableId, state) {
    document.querySelectorAll(`#${tableId} thead th[data-key]`).forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.key === state.sortKey) {
        th.classList.add(state.sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
      }
    });
  }

  function repaintConfirmed() {
    paint(applyState(confirmed, confirmedState), 'confirmed-body', 'confirmed-empty');
    updateSortIndicators('confirmed-table', confirmedState);
  }

  function setupSortableHeaders(tableId, state, repaintFn) {
    document.querySelectorAll(`#${tableId} thead th[data-key]`).forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = key;
          state.sortDir = 'asc';
        }
        repaintFn();
      });
    });
  }

  setupSortableHeaders('confirmed-table', confirmedState, repaintConfirmed);

  repaintConfirmed();

  document.getElementById('search-confirmed').addEventListener('input', e => {
    confirmedState.query = e.target.value;
    repaintConfirmed();
  });

  const countyFilterSelect = document.getElementById('county-filter');
  (leadData.counties || []).forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.folder;
    opt.textContent = c.label;
    countyFilterSelect.appendChild(opt);
  });
  countyFilterSelect.addEventListener('change', e => {
    confirmedState.countyFolder = e.target.value;
    repaintConfirmed();
  });

  document.getElementById('export-confirmed').addEventListener('click', () => {
    const rows = [['County', 'Deceased', 'Owner Record', 'Parcel ID', 'Address', 'City', 'State', 'Zip', 'Property Value', 'Matched Keywords', 'Obituary URL', 'Assessor URL', 'Date Added', 'Letter Status', 'Letter Sent At', 'Mailed In', 'Date Mailed In']];
    confirmed.forEach(l => {
      const mailedState = getMailedState(l);
      rows.push([
        l.county, l.fullName, l.ownerName, l.parcelId, l.address, l.city, l.state, l.zip, l.propertyValue, l.matchedKeywords.join('; '), l.obituaryUrl, l.assessorUrl, l.dateAdded,
        letterStatusLabel(l), (l.letter && l.letter.sentAt) || '',
        mailedState.mailed ? 'Yes' : 'No', mailedState.date
      ]);
    });
    const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\\n');
    const blob = new Blob([csv], {type: 'text/csv'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'confirmed_leads.csv';
    a.click();
  });

  // --- PDF Templates admin panel ---
  const pdfTemplatesBody = document.getElementById('pdf-templates-body');
  const pdfTemplatesEmpty = document.getElementById('pdf-templates-empty');

  function renderPdfTemplatesTable() {
    pdfTemplatesBody.innerHTML = '';
    if (!pdfTemplates.length) {
      pdfTemplatesEmpty.style.display = 'block';
      return;
    }
    pdfTemplatesEmpty.style.display = 'none';
    pdfTemplates.forEach(t => {
      const tr = document.createElement('tr');
      tr.appendChild(el('td', null, t.company));
      tr.appendChild(el('td', null, t.templateName));
      tr.appendChild(el('td', null, t.uploadedAt || '—'));
      const previewTd = el('td');
      previewTd.appendChild(linkEl(`/pdf-templates/${t.filename}`, 'Preview'));
      tr.appendChild(previewTd);

      const hasPosition = Boolean(t.fieldPositions && t.fieldPositions.owner_name);
      const placedTd = el('td');
      placedTd.appendChild(el('span', `badge ${hasPosition ? 'sent' : 'not-sent'}`, hasPosition ? 'Personalized' : 'Static'));
      tr.appendChild(placedTd);

      const actionTd = el('td');
      const placeBtn = el('button', 'ghost', hasPosition ? 'Edit Placement' : 'Add Name Placement');
      placeBtn.type = 'button';
      placeBtn.style.marginRight = '6px';
      placeBtn.addEventListener('click', () => openPlacementModal({ id: t.rawId, filename: t.filename }));
      actionTd.appendChild(placeBtn);
      const delBtn = el('button', 'ghost', 'Delete');
      delBtn.type = 'button';
      delBtn.addEventListener('click', () => deletePdfTemplate(t.rawId));
      actionTd.appendChild(delBtn);
      tr.appendChild(actionTd);
      pdfTemplatesBody.appendChild(tr);
    });
  }
  renderPdfTemplatesTable();

  document.getElementById('pdf-upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const companyName = document.getElementById('pdf-company-name').value.trim();
    const templateName = document.getElementById('pdf-template-name').value.trim();
    const file = document.getElementById('pdf-file-input').files[0];
    const resultEl = document.getElementById('pdf-upload-result');
    resultEl.classList.remove('show', 'ok', 'error');
    if (!companyName || !templateName || !file) return;

    const formData = new FormData();
    formData.append('companyName', companyName);
    formData.append('templateName', templateName);
    formData.append('file', file);

    try {
      const response = await fetch('/api/upload-pdf-template', { method: 'POST', body: formData });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Upload failed.');
      openPlacementModal({ id: data.id, filename: data.filename });
    } catch (err) {
      resultEl.classList.add('show', 'error');
      resultEl.textContent = err.message;
    }
  });

  // --- Click-to-place: where the recipient's name gets stamped on a PDF template ---
  const placementBackdrop = document.getElementById('placement-modal-backdrop');
  const placementImg = document.getElementById('placement-preview-img');
  const placementMarker = document.getElementById('placement-marker');
  const placementWrap = document.getElementById('placement-preview-wrap');
  const placementResult = document.getElementById('placement-result');
  const placementSaveBtn = document.getElementById('placement-save');
  const placementSkipBtn = document.getElementById('placement-skip');

  let placementTemplate = null;
  let placementRatio = null;

  function openPlacementModal(template) {
    placementTemplate = template;
    placementRatio = null;
    placementMarker.style.display = 'none';
    placementSaveBtn.disabled = true;
    placementSaveBtn.textContent = 'Save Position';
    placementResult.classList.remove('show', 'ok', 'error');
    placementResult.textContent = '';
    placementImg.src = `/pdf-templates/${template.filename}/preview.png?t=${Date.now()}`;
    placementBackdrop.classList.add('open');
  }

  function closePlacementModal() {
    placementBackdrop.classList.remove('open');
    placementTemplate = null;
  }

  placementWrap.addEventListener('click', (e) => {
    const rect = placementImg.getBoundingClientRect();
    const xRatio = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
    const yRatio = Math.min(Math.max((e.clientY - rect.top) / rect.height, 0), 1);
    placementRatio = { x: xRatio, y: yRatio };
    placementMarker.style.left = `${xRatio * 100}%`;
    placementMarker.style.top = `${yRatio * 100}%`;
    placementMarker.style.display = 'block';
    placementSaveBtn.disabled = false;
  });

  placementSaveBtn.addEventListener('click', async () => {
    if (!placementTemplate || !placementRatio) return;
    placementSaveBtn.disabled = true;
    placementSaveBtn.textContent = 'Saving…';
    try {
      const response = await fetch('/api/set-pdf-template-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: placementTemplate.id,
          field: 'owner_name',
          xRatio: placementRatio.x,
          yRatio: placementRatio.y,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Failed to save position.');
      window.location.reload();
    } catch (err) {
      placementResult.classList.add('show', 'error');
      placementResult.textContent = err.message;
      placementSaveBtn.disabled = false;
      placementSaveBtn.textContent = 'Save Position';
    }
  });

  placementSkipBtn.addEventListener('click', () => window.location.reload());
  document.getElementById('placement-modal-close').addEventListener('click', closePlacementModal);
  placementBackdrop.addEventListener('click', (e) => {
    if (e.target === placementBackdrop) closePlacementModal();
  });

  async function deletePdfTemplate(id) {
    if (!confirm('Delete this PDF template? This cannot be undone.')) return;
    try {
      const response = await fetch('/api/delete-pdf-template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Delete failed.');
      window.location.reload();
    } catch (err) {
      alert(err.message);
    }
  }
</script>

</div>

<script>
  // NOTE: this is a client-side gate on a static local file, not real
  // access control - the credentials below are visible to anyone who views
  // the page source. It just keeps the report from being open by default.
  const VALID_EMAIL = "thomasjr@leads.extractor";
  const VALID_PASSWORD = "239184";
  const SESSION_KEY = "probateLeadsAuthed";

  const loginScreen = document.getElementById('login-screen');
  const appContent = document.getElementById('app-content');
  const loginForm = document.getElementById('login-form');
  const loginError = document.getElementById('login-error');

  function unlock() {
    loginScreen.style.display = 'none';
    appContent.style.display = 'block';
  }

  if (sessionStorage.getItem(SESSION_KEY) === 'true') {
    unlock();
  }

  loginForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const email = document.getElementById('login-email').value.trim().toLowerCase();
    const password = document.getElementById('login-password').value;
    if (email === VALID_EMAIL && password === VALID_PASSWORD) {
      sessionStorage.setItem(SESSION_KEY, 'true');
      loginError.classList.remove('show');
      unlock();
    } else {
      loginError.classList.add('show');
    }
  });
</script>

</body>
</html>
"""


def build_dashboard_html():
    """Re-reads the dated xlsx files and rebuilds the page fresh - called on
    every request so a browser refresh always reflects the latest exports,
    with no need to re-run this script."""
    confirmed_leads = load_all_confirmed_leads()
    confirmed_count = len(confirmed_leads)

    letters_sent = pipeline_db.get_latest_letters_sent()
    for lead in confirmed_leads:
        letter = letters_sent.get((lead["countyFolder"], lead["fullName"], lead["parcelId"]))
        lead["letter"] = letter or None

    # Lead Pursued Rate = % of confirmed leads that have at least one
    # successfully sent letter on record.
    sent_count = sum(1 for lead in confirmed_leads if lead["letter"] and lead["letter"]["status"] == "sent")
    lead_pursued_rate = f"{round(100 * sent_count / confirmed_count)}%" if confirmed_count else ""

    pdf_templates = [
        {
            "id": f"pdf:{t['id']}",
            "rawId": t["id"],
            "name": f"{t['companyName']} — {t['templateName']}",
            "company": t["companyName"],
            "templateName": t["templateName"],
            "filename": t["filename"],
            "fieldPositions": t["fieldPositions"],
            "uploadedAt": t["uploadedAt"],
        }
        for t in pipeline_db.get_pdf_templates()
    ]
    counties = [{"folder": c["folder"], "label": c["label"]} for c in COUNTIES]
    lead_data = {
        "confirmed": confirmed_leads,
        "templates": LETTER_TEMPLATES,
        "pdfTemplates": pdf_templates,
        "counties": counties,
    }

    county_labels = [c["label"] for c in COUNTIES]
    if len(county_labels) <= 3:
        counties_subtitle = " & ".join(county_labels)
    else:
        counties_subtitle = f"{len(county_labels)} Utah Counties"

    return (TEMPLATE
        .replace("__GENERATED_AT__", datetime.now().strftime("%B %d, %Y at %-I:%M %p"))
        .replace("__CONFIRMED_COUNT__", str(confirmed_count))
        .replace("__LEAD_PURSUED_RATE__", lead_pursued_rate)
        .replace("__COUNTIES_SUBTITLE__", counties_subtitle)
        .replace("__LEAD_DATA_JSON__", json.dumps(lead_data))
    )


REQUIRED_SEND_FIELDS = ["fullName", "county", "address", "city", "state", "zip", "templateId"]


def handle_send_letter(payload):
    """Validates the request, sends via Lob, and records the attempt
    (success or failure) in pipeline_cache.db. Returns (http_status, body_dict)."""
    template_id = str(payload.get("templateId", "")).strip()
    is_pdf_template = template_id.startswith("pdf:")

    required = REQUIRED_SEND_FIELDS if is_pdf_template else REQUIRED_SEND_FIELDS + ["message"]
    missing = [f for f in required if not str(payload.get(f, "")).strip()]
    if missing:
        # City/Zip are the two fields known to come back blank from
        # geocoding for some Salt Lake County addresses (see dashboard.py's
        # confirmed-leads table) - this is the same gap surfacing here as a
        # hard block on sending, since Lob can't deliver an incomplete address.
        return 400, {"error": f"Missing required field(s): {', '.join(missing)}. Can't mail without a complete address."}

    full_name = payload.get("fullName", "")
    owner_name = payload.get("ownerName", "")
    parcel_id = payload.get("parcelId", "")
    county = payload.get("county", "")
    message = payload.get("message", "")
    to_name = owner_name or full_name

    if is_pdf_template:
        try:
            pdf_row_id = int(template_id.split(":", 1)[1])
        except ValueError:
            return 400, {"error": "Invalid PDF template id."}
        pdf_template = pipeline_db.get_pdf_template(pdf_row_id)
        if not pdf_template:
            return 400, {"error": "That PDF template no longer exists."}
        try:
            with open(os.path.join(PDF_TEMPLATES_DIR, pdf_template["filename"]), "rb") as f:
                pdf_bytes = f.read()
        except OSError:
            return 400, {"error": "That PDF template's file is missing on disk."}

        owner_position = pdf_template["fieldPositions"].get("owner_name")
        if owner_position:
            pdf_bytes = pdf_merge.stamp_owner_name(
                pdf_bytes, owner_position["x"], owner_position["y"], to_name,
            )

        description = f"{pdf_template['companyName']} - {pdf_template['templateName']} - {full_name}"

        def send():
            return mailer.send_letter_pdf(
                to_name=to_name,
                address_line1=payload["address"],
                city=payload["city"],
                state=payload["state"],
                zip_code=payload["zip"],
                pdf_bytes=pdf_bytes,
                description=description,
            )
    else:
        letter_html = build_letter_html(message)

        def send():
            return mailer.send_letter(
                to_name=to_name,
                address_line1=payload["address"],
                city=payload["city"],
                state=payload["state"],
                zip_code=payload["zip"],
                html_body=letter_html,
                description=f"{template_id} - {full_name}",
            )

    try:
        result = send()
    except mailer.MailerConfigError as e:
        return 400, {"error": str(e)}
    except mailer.LobAPIError as e:
        pipeline_db.record_letter_sent(
            county, full_name, owner_name, parcel_id, template_id, message,
            status="failed", test_mode=mailer.is_test_mode(), error=str(e),
        )
        return 502, {"error": str(e)}

    pipeline_db.record_letter_sent(
        county, full_name, owner_name, parcel_id, template_id, message,
        lob_letter_id=result.get("id", ""),
        status="sent",
        expected_delivery_date=result.get("expected_delivery_date", ""),
        preview_url=result.get("url", ""),
        test_mode=mailer.is_test_mode(),
    )
    return 200, {
        "lobLetterId": result.get("id", ""),
        "expectedDeliveryDate": result.get("expected_delivery_date", ""),
        "previewUrl": result.get("url", ""),
        "testMode": mailer.is_test_mode(),
    }


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/pdf-templates/"):
            rest = self.path[len("/pdf-templates/"):]
            if rest.endswith("/preview.png"):
                self._serve_pdf_template_preview(rest[: -len("/preview.png")])
            else:
                self._serve_pdf_template(rest)
            return

        body = build_dashboard_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_pdf_template(self, filename):
        # basename() strips any "../" a crafted path might carry - only
        # files actually inside PDF_TEMPLATES_DIR are servable.
        filename = os.path.basename(filename)
        path = os.path.join(PDF_TEMPLATES_DIR, filename)
        if not filename.endswith(".pdf") or not os.path.isfile(path):
            self._send_json(404, {"error": "Not found"})
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_pdf_template_preview(self, filename):
        filename = os.path.basename(filename)
        path = os.path.join(PDF_TEMPLATES_DIR, filename)
        if not filename.endswith(".pdf") or not os.path.isfile(path):
            self._send_json(404, {"error": "Not found"})
            return
        with open(path, "rb") as f:
            pdf_bytes = f.read()
        try:
            png_bytes, _, _ = pdf_merge.render_first_page_png(pdf_bytes)
        except Exception as e:
            print(f"Unexpected error rendering PDF preview: {e}")
            self._send_json(500, {"error": "Couldn't render a preview of that PDF."})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png_bytes)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(png_bytes)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if self.path == "/api/upload-pdf-template":
            fields = _parse_multipart(self.headers.get("Content-Type", ""), raw_body)
            try:
                status, response = handle_upload_pdf_template(fields)
            except Exception as e:
                print(f"Unexpected error uploading PDF template: {e}")
                status, response = 500, {"error": "Unexpected server error - check the terminal running dashboard.py."}
            self._send_json(status, response)
            return

        handlers = {
            "/api/send-letter": handle_send_letter,
            "/api/delete-pdf-template": handle_delete_pdf_template,
            "/api/set-pdf-template-field": handle_set_pdf_template_field,
        }
        handler = handlers.get(self.path)
        if handler is None:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        try:
            status, response = handler(payload)
        except Exception as e:
            print(f"Unexpected error handling {self.path}: {e}")
            status, response = 500, {"error": "Unexpected server error - check the terminal running dashboard.py."}

        self._send_json(status, response)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The default access log (one line per request, including favicon
        # requests) is noisy for a single-user local tool - just note refreshes.
        print(f"Dashboard refreshed at {datetime.now().strftime('%H:%M:%S')}")


def main():
    # Also write a static snapshot to disk, for reference / opening without
    # the server running - but the server below is the primary way to view
    # this now, since it rebuilds from the xlsx files on every request.
    with open(OUTPUT_PATH, "w") as f:
        f.write(build_dashboard_html())

    try:
        server = http.server.HTTPServer(("127.0.0.1", PORT), DashboardRequestHandler)
    except OSError as e:
        print(f"Couldn't start the dashboard server on port {PORT}: {e}")
        print("Is dashboard.py already running in another terminal? If so, just refresh that browser tab.")
        raise SystemExit(1)

    url = f"http://127.0.0.1:{PORT}/"
    print(f"Dashboard live at {url} - refresh the page any time to see the latest data.")
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
        server.shutdown()


if __name__ == "__main__":
    main()
