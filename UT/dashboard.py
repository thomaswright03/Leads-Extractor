import html
import http.cookies
import http.server
import json
import os
import re
import secrets
import time
import uuid
import webbrowser
from datetime import datetime

import pandas as pd

import auth
import billing
import mailer
import pdf_merge
import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "dashboard.html")
PDF_TEMPLATES_DIR = os.path.join(SCRIPT_DIR, "pdf_templates")
PORT = 8765

# Comma-separated allowlist for the admin (operator) view - sees every
# county regardless of who owns it, and can send letters on any agent's
# behalf (see get_allowed_county_folders() and handle_send_letter() below).
# Not stored in Supabase since it's operator-level, not tenant data.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin(email):
    return bool(email) and email.strip().lower() in ADMIN_EMAILS

# Comma-separated allowlist for a restricted role that can only view The Map
# and mark a "ready to sell" (yellow) county "sold" (green) - nothing else.
# Used for someone (e.g. a sales rep) who closes sales but shouldn't see
# lead data, send letters, or touch a county's building/not-started status.
# An admin email already has full map access, so it doesn't need to appear
# here too.
MAP_EDITOR_EMAILS = {e.strip().lower() for e in os.environ.get("MAP_EDITOR_EMAILS", "").split(",") if e.strip()}


def is_map_editor(email):
    return bool(email) and email.strip().lower() in MAP_EDITOR_EMAILS


def is_map_only(email):
    """True for a session that should see nothing but The Map - a map
    editor who isn't also a full admin."""
    return is_map_editor(email) and not is_admin(email)

# For The Map (see /map, /api/map/*): the only counties with a real,
# functioning pipeline folder are these three - everything else on the map
# is either "not started" or manually flagged "building" (see
# county_manual_status in pipeline_db.py). A folder key's presence here is
# what turns it into "ready to sell" (yellow) or, once it has an owner,
# "sold" (green) - add a line here the day a new county's pipeline actually
# works, not before. County FIPS codes are the standard 5-digit Census ones
# (first 2 digits = state).
COUNTY_FOLDER_TO_FIPS = {
    "slk": "49035",  # Salt Lake County, UT
    "uta": "49049",  # Utah County, UT
    "mar": "04013",  # Maricopa County, AZ
    "apa": "04001",  # Apache County, AZ
    "gre": "04011",  # Greenlee County, AZ
    "gra": "04009",  # Graham County, AZ
    "pim": "04019",  # Pima County, AZ
    "gil": "04007",  # Gila County, AZ
    "yum": "04027",  # Yuma County, AZ
    "yav": "04025",  # Yavapai County, AZ
}

# The Map only knows FIPS codes (they come from the public us-atlas
# topojson), but pricing_plans is keyed by county folder - this is how
# handle_set_county_price resolves one from the other.
COUNTY_FIPS_TO_FOLDER = {fips: folder for folder, fips in COUNTY_FOLDER_TO_FIPS.items()}

# In-memory session store (session_token -> {"email", "expires_at"}). Lives
# only as long as this process - a restart just means everyone logs in
# again, which is fine for how this tool is actually used. Real credentials
# live in Supabase Auth; this only tracks which browser has already proven
# it knows one.
SESSIONS = {}
SESSION_COOKIE_NAME = "session"
SESSION_DURATION_SECONDS = 60 * 60 * 24


def create_session(email):
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"email": email, "expires_at": time.time() + SESSION_DURATION_SECONDS}
    return token


def get_session_email(handler):
    cookie_header = handler.headers.get("Cookie", "")
    if not cookie_header:
        return None
    jar = http.cookies.SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(SESSION_COOKIE_NAME)
    if not morsel:
        return None
    session = SESSIONS.get(morsel.value)
    if not session or session["expires_at"] < time.time():
        SESSIONS.pop(morsel.value, None)
        return None
    return session["email"]


def destroy_session(handler):
    cookie_header = handler.headers.get("Cookie", "")
    if not cookie_header:
        return
    jar = http.cookies.SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(SESSION_COOKIE_NAME)
    if morsel:
        SESSIONS.pop(morsel.value, None)

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


def handle_generate_pdf_template(fields):
    """Builds a designed PDF from the template-creation wizard's answers
    (see pdf_merge.generate_letter_pdf) and registers it exactly like an
    uploaded PDF template - including the name-placement position, computed
    directly from the layout instead of requiring the usual manual
    click-to-place step."""
    company_name = str(fields.get("companyName", "")).strip()
    template_name = str(fields.get("templateName", "")).strip()
    business_name = str(fields.get("businessName", "")).strip()
    if not company_name or not template_name or not business_name:
        return 400, {"error": "Business name and template name are required."}

    angles = [a for a in str(fields.get("angles", "")).split(",") if a]
    logo_field = fields.get("logo")
    logo_bytes = logo_field["content"] if isinstance(logo_field, dict) and logo_field.get("content") else None

    try:
        pdf_bytes, name_x_ratio, name_y_ratio = pdf_merge.generate_letter_pdf(
            business_name=business_name,
            phone=str(fields.get("phone", "")).strip(),
            email=str(fields.get("email", "")).strip(),
            accent_hex=str(fields.get("accentColor", "")).strip(),
            tone=str(fields.get("tone", "")).strip(),
            angles=angles,
            contact_method=str(fields.get("contactMethod", "")).strip(),
            closing_note=str(fields.get("closingNote", "")).strip(),
            logo_bytes=logo_bytes,
        )
    except Exception as e:
        print(f"Unexpected error generating PDF template: {e}")
        return 500, {"error": "Couldn't generate the PDF - check the terminal running dashboard.py."}

    os.makedirs(PDF_TEMPLATES_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.pdf"
    with open(os.path.join(PDF_TEMPLATES_DIR, filename), "wb") as f:
        f.write(pdf_bytes)

    template_id = pipeline_db.add_pdf_template(company_name, template_name, filename)
    pipeline_db.set_pdf_template_field_position(template_id, "owner_name", name_x_ratio, name_y_ratio)
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


def _compute_all_county_statuses():
    """Every US county's pipeline-progress status, keyed by 5-digit FIPS
    code. A county missing from this dict is "not started". Factored out of
    handle_map_status so handle_set_county_status can also check a single
    county's current computed status (needed to enforce a restricted map
    editor's "only ready -> sold" rule). A "ready" entry also carries the
    county's current listed price (priceCents) - The Map's price editor
    (see handle_set_county_price) only ever touches a county in this state,
    so this is the one place that needs to hand it over."""
    county_prices = {
        p["key"]: p["monthly_price_cents"]
        for p in pipeline_db.get_pricing_plans()
        if p["kind"] == "county"
    }
    statuses = dict(pipeline_db.get_manual_county_statuses())
    for folder_key, fips in COUNTY_FOLDER_TO_FIPS.items():
        owner = pipeline_db.get_county_owner(folder_key)
        if owner:
            # A real Stripe owner is always authoritative - overwrites any
            # manual override for this same county ("building", "ready", or
            # a manually-recorded sale), since that customer's own real
            # signup/subscription is the actual record once it exists. The
            # "real" flag tells the map not to offer any manual override on
            # top of it - there's nothing to override.
            statuses[fips] = {"status": "sold", "customerEmail": owner, "real": True}
        elif fips not in statuses:
            # No manual override recorded at all - the default computed
            # state for a county with a real pipeline script and no real
            # owner is "ready to sell". A manual "building" or manually-
            # recorded "sold" override (see handle_set_county_status) is
            # left exactly as stored - deleting it (status "ready") is what
            # falls back to this computed default.
            statuses[fips] = {"status": "ready"}
            if folder_key in county_prices:
                statuses[fips]["priceCents"] = county_prices[folder_key]
    return statuses


def handle_map_status():
    """For The Map: the frontend already has every county's shape/name from
    the public us-atlas topojson it fetches directly, so this only needs to
    carry the handful of counties that actually have something to report."""
    return 200, _compute_all_county_statuses()


def handle_set_county_status(payload, restrict_to_marking_sold=False):
    """restrict_to_marking_sold is set for a session that's a map editor but
    not a full admin (see is_map_only) - it can only flip a county that's
    currently "ready to sell" to "sold", nothing else, regardless of what a
    crafted request claims."""
    fips = str(payload.get("fips", "")).strip()
    status = str(payload.get("status", "")).strip()
    if not re.fullmatch(r"\d{5}", fips):
        return 400, {"error": "Invalid county FIPS code."}

    has_real_script = fips in COUNTY_FOLDER_TO_FIPS.values()

    if restrict_to_marking_sold:
        if status != "sold":
            return 403, {"error": "This account can only mark a county as sold."}
        if _compute_all_county_statuses().get(fips, {}).get("status") != "ready":
            return 400, {"error": "That county isn't ready to sell yet."}
    else:
        # A county with a real pipeline script can move freely between
        # being built, ready to sell, and (manually) sold, in either
        # direction - "not started" doesn't apply once a real script
        # exists. A county with no script yet can only be flagged as being
        # worked on or reset - there's nothing to sell.
        valid_statuses = {"building", "ready", "sold"} if has_real_script else {"not_started", "building"}
        if status not in valid_statuses:
            return 400, {"error": f"Status must be one of: {', '.join(sorted(valid_statuses))}."}

        current = _compute_all_county_statuses().get(fips, {})
        if current.get("real"):
            return 400, {"error": "This county has a real paying subscriber - status can't be changed here."}

    customer_name = str(payload.get("customerName", "")).strip()
    customer_email = str(payload.get("customerEmail", "")).strip()
    customer_phone = str(payload.get("customerPhone", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    if status == "sold":
        # This records a sale closed outside the self-serve signup/Stripe
        # flow (e.g. the sales rep closed it directly) - it doesn't create
        # a login or grant real dashboard access, it just puts the
        # customer's info on file here. A real Stripe subscription for this
        # county still takes priority if one exists (see handle_map_status).
        if not customer_email or "@" not in customer_email:
            return 400, {"error": "A valid customer email is required to mark a county sold."}

    # "ready" is never stored - it's just the absence of a manual override
    # for a county with a real script (see _compute_all_county_statuses),
    # so reverting to it is a delete, same mechanism "not_started" already
    # uses for a county with no script yet.
    pipeline_db.set_manual_county_status(
        fips, "not_started" if status == "ready" else status,
        customer_name=customer_name or None,
        customer_email=customer_email or None,
        customer_phone=customer_phone or None,
        notes=notes or None,
    )
    return 200, {"ok": True}


def handle_set_county_price(payload):
    """Changes a county's standing monthly price - available/ready-to-sell
    counties only (see below), from The Map's county panel. Adam (or any
    full admin) sometimes closes a deal above the listed price; this is how
    that becomes the new listed price everywhere else that quotes it
    (signup, "Add More Access", the next Stripe Checkout for this county)
    instead of a one-off override that only he remembers. Open to the same
    two roles as handle_set_county_status (full admin or the restricted map
    editor) - see this function's call site."""
    fips = str(payload.get("fips", "")).strip()
    if not re.fullmatch(r"\d{5}", fips):
        return 400, {"error": "Invalid county FIPS code."}

    folder = COUNTY_FIPS_TO_FOLDER.get(fips)
    if not folder:
        return 400, {"error": "This county doesn't have a real pipeline yet."}

    # Only a county that's actually available to sell right now - not being
    # built, and not already sold (real subscriber or a manually-recorded
    # sale) - changing the price after a sale wouldn't touch that
    # customer's already-created subscription anyway, so it would look
    # editable here without doing anything real.
    if _compute_all_county_statuses().get(fips, {}).get("status") != "ready":
        return 400, {"error": "Only a county that's ready to sell can have its price changed here."}

    try:
        price_dollars = float(payload.get("priceDollars"))
    except (TypeError, ValueError):
        return 400, {"error": "Enter a valid price."}
    if price_dollars <= 0 or price_dollars > 100000:
        return 400, {"error": "Enter a realistic monthly price."}
    price_cents = round(price_dollars * 100)

    try:
        billing.update_plan_price("county", folder, price_cents)
    except billing.BillingError as e:
        return 400, {"error": str(e)}
    return 200, {"ok": True, "priceCents": price_cents}


# Every county pipeline folder this dashboard aggregates, each generated by
# template_county/new_county.py (or, for slk, hand-built before that wizard
# existed). Add one entry here whenever a new county folder is created - this
# is the one place that needs a manual update; nothing else auto-discovers
# counties, since importing a county's own scripts here isn't safe (e.g.
# uta/geocode.py pulls in selenium, which this dashboard has no reason to need).
# "folder" doubles as the access-control key (see get_allowed_county_folders())
# and must stay a short unique name even for counties outside UT - "path" is
# what actually locates the folder on disk, relative to this script's own
# directory, and only needs setting when it isn't a direct subfolder here.
COUNTIES = [
    {
        "folder": "slk",
        "label": "Salt Lake County",
        "state": "UT",
        "parcel_url_base": "https://apps.saltlakecounty.gov/assessor/new/valuationInfoExpanded.cfm?parcel_id=",
    },
    {
        "folder": "uta",
        "label": "Utah County",
        "state": "UT",
        "parcel_url_base": "https://www.utahcounty.gov/LandRecords/ValNotice.asp?av_serial=",
    },
    {
        "folder": "mar",
        "path": "../AZ/mar",
        "label": "Maricopa County",
        "state": "AZ",
        "parcel_url_base": "https://mcassessor.maricopa.gov/mcs/?q=",
    },
    {
        "folder": "apa",
        "path": "../AZ/apa",
        "label": "Apache County",
        "state": "AZ",
        "parcel_url_base": "https://eagleassessor.co.apache.az.us/assessor/taxweb/account.jsp?accountNum=",
    },
    {
        "folder": "gre",
        "path": "../AZ/gre",
        "label": "Greenlee County",
        "state": "AZ",
        # Greenlee's parcel search site has no bookmarkable per-parcel URL
        # (its ParcelDetail.aspx?a=<guid> links are scoped to the search
        # session that generated them, not stable across sessions -
        # confirmed live) - this just lands an admin on the blank search
        # page (the account number gets tacked on as an ignored, harmless
        # query param) rather than a real deep link to the specific parcel.
        "parcel_url_base": "https://parcelsearch.greenlee.az.gov/Production/RealWareDataMartPropertyInquiryWebsite/Inquiry.aspx?ignored=",
    },
    {
        "folder": "gra",
        "path": "../AZ/gra",
        "label": "Graham County",
        "state": "AZ",
        # Graham runs the same RealWare DataMart platform as Greenlee, with
        # the same session-scoped ParcelDetail.aspx?a=<guid> links - no
        # bookmarkable per-parcel URL, so this lands an admin on the blank
        # search page (the account number tacked on as an ignored, harmless
        # query param) rather than a real deep link to the specific parcel.
        "parcel_url_base": "https://gcwebinfo.graham.az.gov/Production/RealWareDataMartPropertyInquiryWebsite/Inquiry.aspx?ignored=",
    },
    {
        "folder": "pim",
        "path": "../AZ/pim",
        "label": "Pima County",
        "state": "AZ",
        # Pima's Angular SPA keeps the viewed parcel in sessionStorage, not
        # the URL (confirmed live) - no bookmarkable per-parcel link, same
        # practical problem as Greenlee/Graham just implemented
        # differently, so this just lands an admin on the homepage rather
        # than a real deep link to the specific parcel.
        "parcel_url_base": "https://www.asr.pima.gov/",
    },
    {
        "folder": "gil",
        "path": "../AZ/gil",
        "label": "Gila County",
        "state": "AZ",
        # Gila runs the same Tyler Technologies EagleWeb/TaxWeb platform as
        # Apache County, and unlike every other AZ county here its
        # account.jsp?accountNum=<id> links are real, stable, bookmarkable
        # URLs (confirmed live) - not session-scoped like Greenlee/Graham's
        # RealWare GUIDs or Pima's sessionStorage-based viewer. A visitor
        # without an active guest session still gets bounced to a login
        # page first either way (same as Apache's identical setup), but
        # this is a real deep link to the specific parcel once logged in.
        "parcel_url_base": "https://gilacountyaz-assessor.tylerhost.net/assessor/taxweb/account.jsp?accountNum=",
    },
    {
        "folder": "yum",
        "path": "../AZ/yum",
        "label": "Yuma County",
        "state": "AZ",
        # Same Tyler Technologies EagleWeb/TaxWeb platform as Gila and
        # Apache Counties - a real, stable, bookmarkable deep link to the
        # specific parcel once logged in (same login-wall caveat as those
        # two: an unauthenticated visitor still gets bounced to a login
        # page first).
        "parcel_url_base": "https://yumacountyaz-assessor.tylerhost.net/assessor/taxweb/account.jsp?accountNum=",
    },
    {
        "folder": "yav",
        "path": "../AZ/yav",
        "label": "Yavapai County",
        "state": "AZ",
        # Yavapai runs a completely different platform from every other AZ
        # county here - a plain, unauthenticated ASP.NET/Kendo Treasurer
        # site ("Pinal & Yavapai IGA"), not Tyler EagleWeb. Its Tax Summary
        # page is a real, stable, bookmarkable, stateless deep link (a
        # plain ?parcelnumber= query param, confirmed live) - no login wall
        # at all, unlike Gila/Yuma/Apache's identical Tyler deployments.
        "parcel_url_base": "https://taxinquiry.yavapaiaz.gov/Parcel/TaxSummary?parcelnumber=",
    },
]

# Matches DC.py's get_output_path() naming: "2026-07-21.xlsx" or, on a second
# run the same day, "2026-07-21_14-30-05.xlsx".
DATED_XLSX_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_\d{2}-\d{2}-\d{2})?\.xlsx$")


def assessor_url(parcel_id, parcel_url_base):
    # Digits-only is correct for Salt Lake/Utah County's pure-numeric serials,
    # but Maricopa APNs can carry a trailing letter split suffix (e.g.
    # "13267062L" for a rental-registered parcel) that its own ?q= search
    # treats as significant - stripping it would silently link to a
    # different parcel, so keep alphanumerics instead.
    cleaned = "".join(ch for ch in (parcel_id or "") if ch.isalnum())
    return f"{parcel_url_base}{cleaned}" if cleaned else ""


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
    county_dir = os.path.join(SCRIPT_DIR, county.get("path", county["folder"]))
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
                # The county's own state (e.g. "AZ") - distinct from "state"
                # above, which is this specific lead's mailing address state
                # and isn't reliably the same thing (a county's exclusivity
                # and a lead's mailing address are unrelated). Lets the
                # dashboard filter by state first, since multiple states
                # each have their own same-named county (e.g. Washington
                # County exists in several states).
                "countyState": county["state"],
            }
    return list(leads_by_key.values())


def get_allowed_county_folders(email):
    """Every county folder key this email can access - explicit per-county
    grants unioned with whole-state grants expanded against today's
    COUNTIES list. Shared by build_dashboard_html() and the send-letter
    authorization check so the two can't drift apart.

    An admin email (ADMIN_EMAILS) sees every county regardless of who
    owns it - the whole point of the admin view."""
    if not email:
        return set()
    if is_admin(email):
        return {c["folder"] for c in COUNTIES}
    granted_counties = set(pipeline_db.get_counties_for_user(email))
    granted_states = set(pipeline_db.get_state_grants_for_user(email))
    return granted_counties | {c["folder"] for c in COUNTIES if c["state"] in granted_states}


def load_all_confirmed_leads(counties):
    """Every confirmed lead across the given counties (a subset of COUNTIES -
    normally just whichever ones the logged-in user has access to), in one
    flat list."""
    leads = []
    for county in counties:
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
    --navy: #101d33;
    --navy-light: #24354f;
    --navy-deep: #0a1424;
    /* Text color for headings/links/numbers - distinct from --navy because
       --navy also backs the always-dark header bar. Overriding --navy alone
       for dark mode would make the header's own white text unreadable. */
    --heading: #16233a;
    --gold: #a6822e;
    --gold-light: #ecce8a;
    --gold-deep: #7c5e1f;
    --cream: #faf7f1;
    --card: #ffffff;
    --ink: #232c3b;
    --muted: #707b8d;
    --border: #e9e2d2;
    --confirmed-bg: #eaf6ee;
    --confirmed-ink: #2a6b3d;
    --review-bg: #fbf1de;
    --review-ink: #8f631a;
    --shadow-tint: 16, 26, 46;
    --ease: cubic-bezier(0.22, 1, 0.36, 1);
  }
  * { box-sizing: border-box; }
  ::selection { background: var(--gold-light); color: #2a1f04; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background:
      radial-gradient(1100px 480px at 12% -10%, rgba(166,130,46,0.06), transparent 60%),
      var(--cream);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  ::-webkit-scrollbar { width: 12px; height: 12px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb {
    background: rgba(var(--shadow-tint), 0.22);
    border-radius: 999px;
    border: 3px solid var(--cream);
  }
  ::-webkit-scrollbar-thumb:hover { background: rgba(var(--shadow-tint), 0.35); }

  a, button, input, select, textarea { transition: background-color .18s var(--ease), border-color .18s var(--ease), color .18s var(--ease), box-shadow .18s var(--ease), transform .18s var(--ease), opacity .18s var(--ease); }
  button { font-family: inherit; }
  :focus-visible {
    outline: none;
    box-shadow: 0 0 0 3px rgba(166,130,46,0.28), 0 0 0 1px var(--gold);
    border-radius: 6px;
  }

  header.top {
    position: relative;
    background:
      radial-gradient(640px 320px at 85% -60%, rgba(236,206,138,0.14), transparent 65%),
      linear-gradient(135deg, var(--navy-deep), var(--navy) 55%, var(--navy-light));
    color: #fff;
    padding: 38px 32px 46px;
    box-shadow: 0 18px 40px -18px rgba(var(--shadow-tint), 0.55);
  }
  header.top::after {
    content: "";
    position: absolute;
    left: 0; right: 0; bottom: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(236,206,138,0.5), transparent);
  }
  .top-inner {
    max-width: 1180px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 16px;
    position: relative;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .brand .mark {
    width: 46px;
    height: 46px;
    border-radius: 50%;
    background: radial-gradient(circle at 32% 30%, var(--gold-light), var(--gold) 65%, var(--gold-deep) 100%);
    flex-shrink: 0;
    box-shadow: 0 0 0 4px rgba(255,255,255,0.10), 0 6px 18px -4px rgba(0,0,0,0.5);
  }
  .brand h1 {
    margin: 0;
    font-family: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, serif;
    font-size: 27px;
    font-weight: 600;
    letter-spacing: 0.2px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.15);
  }
  .brand p {
    margin: 3px 0 0;
    font-size: 12.5px;
    letter-spacing: 0.03em;
    color: rgba(236,206,138,0.75);
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
    position: relative;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
    margin: 0 0 28px;
  }
  .stat-card {
    position: relative;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 22px 20px 26px;
    box-shadow: 0 10px 28px -10px rgba(var(--shadow-tint), 0.18);
    overflow: hidden;
    transition: transform .28s var(--ease), box-shadow .28s var(--ease);
  }
  .stat-card::before {
    content: "";
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 4px;
    background: linear-gradient(180deg, var(--gold-light), var(--gold));
  }
  .stat-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 18px 36px -12px rgba(var(--shadow-tint), 0.28);
  }
  .stat-card .num {
    font-family: Georgia, "Iowan Old Style", Palatino, serif;
    font-size: 32px;
    font-weight: 700;
    color: var(--heading);
    line-height: 1;
    letter-spacing: 0.01em;
  }
  .stat-card .label {
    margin-top: 7px;
    font-size: 12px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.09em;
    font-weight: 600;
  }
  .stat-card.confirmed .num { color: var(--confirmed-ink); }

  .panel {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 14px 32px -16px rgba(var(--shadow-tint), 0.16);
    overflow: hidden;
    margin-bottom: 24px;
    transition: box-shadow .28s var(--ease);
  }
  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    background: linear-gradient(180deg, rgba(166,130,46,0.035), transparent);
  }
  .panel-head h2 {
    margin: 0;
    font-family: Georgia, "Iowan Old Style", Palatino, serif;
    font-size: 18.5px;
    font-weight: 600;
    color: var(--heading);
    letter-spacing: 0.01em;
  }
  .panel-head .sub {
    font-size: 12.5px;
    color: var(--muted);
    margin-top: 3px;
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
  .search-box input:focus-visible { background: var(--card); }
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
    background: var(--card);
    color: var(--heading);
    padding: 8px 16px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 1px 2px rgba(var(--shadow-tint), 0.06);
  }
  button.ghost:hover {
    background: var(--gold-light);
    border-color: var(--gold-light);
    color: #4a3a0d;
    transform: translateY(-1px);
    box-shadow: 0 6px 14px -6px rgba(166,130,46,0.45);
  }
  button.ghost:active { transform: translateY(0); }
  button.ghost:disabled { opacity: 0.5; cursor: not-allowed; transform: none; box-shadow: none; }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    font-weight: 700;
    color: var(--muted);
    padding: 12px 22px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, #fbf9f3, #f7f3ea);
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
    padding: 15px 22px;
    border-bottom: 1px solid var(--border);
    font-size: 13.5px;
    vertical-align: top;
  }
  tbody tr { position: relative; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr { transition: background-color .15s var(--ease); }
  tbody tr:hover { background: #fbf9f2; }
  tbody tr:hover td:first-child::before {
    content: "";
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
    background: linear-gradient(180deg, var(--gold-light), var(--gold));
  }
  .name-cell .deceased { font-weight: 600; color: var(--heading); }
  .name-cell .owner { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
  .chip {
    display: inline-block;
    background: var(--gold-light);
    color: #5a4409;
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 11.5px;
    margin: 1px 3px 1px 0;
    font-weight: 700;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 11px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.02em;
  }
  .badge::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    flex-shrink: 0;
  }
  .badge.confirmed { background: var(--confirmed-bg); color: var(--confirmed-ink); }
  .badge.review { background: var(--review-bg); color: var(--review-ink); }
  .badge.taken { background: rgba(112,123,141,0.14); color: var(--muted); }
  a.link {
    color: var(--heading);
    text-decoration: none;
    font-size: 13px;
    font-weight: 500;
    border-bottom: 1px solid var(--gold);
    padding-bottom: 1px;
  }
  a.link:hover { color: var(--gold-deep); border-color: var(--gold-deep); }
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
    padding: 48px 22px;
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
    padding: 5px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  button.send-letter-btn:hover {
    background: var(--gold);
    border-color: var(--gold);
    color: #fff;
    box-shadow: 0 4px 12px -4px rgba(166,130,46,0.5);
  }

  @keyframes modalIn {
    from { opacity: 0; transform: translateY(10px) scale(0.98); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }
  @keyframes backdropIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }
  .modal-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(10,15,25,0.62);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    align-items: center;
    justify-content: center;
    padding: 20px;
    z-index: 100;
  }
  .modal-backdrop.open { display: flex; animation: backdropIn .22s var(--ease); }
  .modal-card {
    background: var(--card);
    border-radius: 16px;
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 30px 70px -18px rgba(0,0,0,0.5);
    animation: modalIn .28s var(--ease);
  }
  .modal-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border);
    position: relative;
  }
  .modal-head::after {
    content: "";
    position: absolute;
    left: 24px; right: 24px; bottom: -1px;
    height: 1px;
    background: linear-gradient(90deg, var(--gold-light), transparent 60%);
  }
  .modal-head h3 { margin: 0; font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 18px; font-weight: 600; color: var(--heading); }
  .modal-close {
    border: none;
    background: none;
    font-size: 22px;
    line-height: 1;
    color: var(--muted);
    cursor: pointer;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .modal-close:hover { background: var(--cream); color: var(--heading); }
  .modal-body {
    padding: 20px 24px;
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
  .modal-body select:focus-visible, .modal-body textarea:focus-visible { background: var(--card); }

  .field-label {
    display: block;
    font-size: 11.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin-bottom: 7px;
  }
  .field-input {
    width: 100%;
    padding: 10px 13px;
    border: 1px solid var(--border);
    border-radius: 9px;
    font-size: 13.5px;
    background: var(--cream);
    color: var(--ink);
    font-family: inherit;
  }
  .field-input:focus-visible { background: var(--card); }
  input[type="file"] {
    font-size: 13px;
    color: var(--muted);
  }
  input[type="file"]::file-selector-button {
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--heading);
    padding: 7px 13px;
    border-radius: 999px;
    font-size: 12.5px;
    font-weight: 600;
    margin-right: 10px;
    cursor: pointer;
    transition: background-color .18s var(--ease), transform .18s var(--ease);
  }
  input[type="file"]::file-selector-button:hover { background: var(--gold-light); }

  .letter-warning {
    display: none;
    font-size: 12.5px;
    color: #a13a3a;
    background: #fbe4e4;
    border-radius: 8px;
    padding: 8px 12px;
  }
  .letter-warning.show { display: block; }
  .letter-price-note {
    font-size: 12.5px;
    color: var(--muted);
    background: var(--panel-2, #f4efe4);
    border-radius: 8px;
    padding: 8px 12px;
  }
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
    padding: 18px 24px;
    border-top: 1px solid var(--border);
  }
  button.primary {
    border: none;
    background: linear-gradient(135deg, var(--navy-light), var(--navy));
    color: #fff;
    padding: 10px 20px;
    border-radius: 999px;
    font-size: 13.5px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 8px 20px -8px rgba(var(--shadow-tint), 0.55);
  }
  button.primary:hover { transform: translateY(-1px); box-shadow: 0 12px 26px -8px rgba(var(--shadow-tint), 0.65); }
  button.primary:active { transform: translateY(0); }
  button.primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; box-shadow: none; }

  .bulk-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 260px;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 6px 4px;
    background: var(--cream);
  }
  .bulk-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 11px;
    border-radius: 7px;
    font-size: 13px;
    transition: background-color .15s var(--ease);
  }
  .bulk-row:hover { background: rgba(166,130,46,0.07); }
  .bulk-row .name { flex: 1; }
  .bulk-row .addr { color: var(--muted); font-size: 12px; }
  .bulk-row .status { font-size: 11.5px; font-weight: 600; color: var(--muted); min-width: 90px; text-align: right; }
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
    letter-spacing: 0.01em;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --cream: #12161f;
      --card: #1b202b;
      --ink: #e7e9ee;
      --muted: #93a0b4;
      --border: #2a3140;
      --heading: #e3c98a;
      --confirmed-bg: #17281c;
      --confirmed-ink: #7bc98a;
      --review-bg: #2b220f;
      --review-ink: #e0b563;
      --shadow-tint: 0, 0, 0;
    }
    body {
      background:
        radial-gradient(1100px 480px at 12% -10%, rgba(236,206,138,0.05), transparent 60%),
        var(--cream);
    }
    thead th { background: linear-gradient(180deg, #191e27, #161b23); }
    tbody tr:hover { background: #202634; }
    .search-box input { background: #14181f; color: var(--ink); }
    .search-box input:focus-visible { background: #191e27; }
    button.ghost { background: var(--card); }
    button.ghost:hover { background: var(--gold); color: #1a1200; border-color: var(--gold); }
    .panel-head { background: linear-gradient(180deg, rgba(236,206,138,0.05), transparent); }
    .modal-body select, .modal-body textarea { background: #14181f; }
    .bulk-list { background: #14181f; }
    ::-webkit-scrollbar-thumb { border-color: var(--cream); }
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

  @keyframes cardRise {
    from { opacity: 0; transform: translateY(14px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .login-screen {
    position: relative;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background:
      radial-gradient(900px 500px at 20% 0%, rgba(236,206,138,0.10), transparent 60%),
      radial-gradient(700px 500px at 100% 100%, rgba(236,206,138,0.06), transparent 55%),
      linear-gradient(135deg, var(--navy-deep), var(--navy) 55%, var(--navy-light));
    padding: 24px;
    overflow: hidden;
  }
  .login-card {
    position: relative;
    background: var(--card);
    border-radius: 18px;
    padding: 38px 36px;
    width: 100%;
    max-width: 380px;
    box-shadow: 0 30px 70px -16px rgba(0,0,0,0.55);
    animation: cardRise .5s var(--ease);
  }
  .login-card .mark {
    width: 46px;
    height: 46px;
    border-radius: 50%;
    background: radial-gradient(circle at 32% 30%, var(--gold-light), var(--gold) 65%, var(--gold-deep) 100%);
    margin: 0 auto 18px;
    box-shadow: 0 8px 20px -6px rgba(166,130,46,0.55);
  }
  .login-card h1 {
    margin: 0 0 5px;
    font-family: Georgia, "Iowan Old Style", Palatino, serif;
    font-size: 22px;
    font-weight: 600;
    color: var(--heading, var(--navy));
    text-align: center;
    letter-spacing: 0.01em;
  }
  .login-card p.sub {
    margin: 0 0 26px;
    font-size: 13px;
    color: var(--muted);
    text-align: center;
  }
  .login-card label {
    display: block;
    font-size: 11.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin-bottom: 6px;
    margin-top: 16px;
  }
  .login-card input {
    width: 100%;
    padding: 11px 13px;
    border: 1px solid var(--border);
    border-radius: 9px;
    font-size: 14px;
    background: var(--cream);
    color: var(--ink);
  }
  .login-card input:focus-visible { background: var(--card); }
  .login-card button[type="submit"] {
    width: 100%;
    margin-top: 24px;
    padding: 12px;
    border: none;
    border-radius: 999px;
    background: linear-gradient(135deg, var(--navy-light), var(--navy));
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.01em;
    cursor: pointer;
    box-shadow: 0 10px 24px -8px rgba(var(--shadow-tint), 0.55);
  }
  .login-card button[type="submit"]:hover { transform: translateY(-1px); box-shadow: 0 14px 28px -8px rgba(var(--shadow-tint), 0.65); }
  .login-card button[type="submit"]:active { transform: translateY(0); }
  .login-card button[type="submit"]:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
  .login-error {
    display: none;
    margin-top: 14px;
    font-size: 12.5px;
    color: #b3261e;
    text-align: center;
  }
  .login-error.show { display: block; }
  .login-card.wide { max-width: 460px; }
  .login-card p.switch-link {
    margin: 18px 0 0;
    font-size: 12.5px;
    color: var(--muted);
    text-align: center;
  }
  .login-card p.switch-link a { color: var(--gold-deep); font-weight: 700; text-decoration: none; }
  .login-card p.switch-link a:hover { color: var(--gold); }
  .login-card label.field-sub { font-weight: 500; text-transform: none; letter-spacing: normal; margin-top: 10px; font-size: 12px; }
  .login-card .field-row { display: flex; gap: 10px; }
  .login-card .field-row > div { flex: 1; }
  .plan-option {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 14px;
    border: 1.5px solid var(--border);
    border-radius: 10px;
    margin-top: 8px;
    cursor: pointer;
    background: var(--cream);
  }
  .plan-option:hover { border-color: var(--gold-light); }
  .plan-option:has(input:checked) {
    border-color: var(--gold);
    background: rgba(166,130,46,0.08);
    box-shadow: 0 0 0 1px var(--gold);
  }
  .plan-option input { width: auto; accent-color: var(--gold-deep); }
  .plan-option .plan-label { flex: 1; font-size: 13.5px; font-weight: 500; color: var(--ink); }
  .plan-option.is-taken { cursor: not-allowed; opacity: 0.6; background: var(--panel-2, #f2f2f2); }
  .plan-option.is-taken:hover { border-color: var(--border); }
  .plan-option.is-taken .plan-label { color: var(--muted); }
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
    <p class="switch-link">New here? <a href="#" id="show-signup-link">Sign up for access</a></p>
  </form>

  <form class="login-card wide" id="signup-form" style="display:none;">
    <div class="mark"></div>
    <h1>Create Your Account</h1>
    <p class="sub">Pick a plan and start receiving leads</p>

    <label for="signup-email">Email</label>
    <input type="email" id="signup-email" autocomplete="username" required>
    <label for="signup-password">Password</label>
    <input type="password" id="signup-password" autocomplete="new-password" minlength="8" required>

    <label style="margin-top:22px;">Return Address (printed on your letters)</label>
    <label for="signup-from-name" class="field-sub">Business / Sender Name</label>
    <input type="text" id="signup-from-name" required>
    <label for="signup-from-address1" class="field-sub">Street Address</label>
    <input type="text" id="signup-from-address1" required>
    <label for="signup-from-address2" class="field-sub">Suite/Unit (optional)</label>
    <input type="text" id="signup-from-address2">
    <div class="field-row">
      <div>
        <label for="signup-from-city" class="field-sub">City</label>
        <input type="text" id="signup-from-city" required>
      </div>
      <div style="max-width:70px;">
        <label for="signup-from-state" class="field-sub">State</label>
        <input type="text" id="signup-from-state" maxlength="2" required>
      </div>
      <div style="max-width:100px;">
        <label for="signup-from-zip" class="field-sub">Zip</label>
        <input type="text" id="signup-from-zip" required>
      </div>
    </div>

    <label style="margin-top:22px;">Choose a Plan</label>
    <p class="sub" id="signup-plans-state-note" style="display:none; margin:0 0 8px; text-align:left;"></p>
    <input type="text" id="signup-plan-search" placeholder="Search counties&hellip;" style="margin-bottom:10px;">
    <div id="signup-plans"></div>
    <p class="sub" id="signup-plans-empty" style="display:none; text-align:left;">No counties match &mdash; try a different search.</p>

    <label style="display:flex; align-items:flex-start; gap:8px; margin-top:18px; font-weight:normal;">
      <input type="checkbox" id="signup-agree-terms" required style="margin-top:3px;">
      <span>I agree to the <a href="/terms" target="_blank">Terms of Service</a> and
      <a href="/privacy" target="_blank">Privacy Policy</a>.</span>
    </label>

    <button type="submit">Create Account &amp; Continue to Payment</button>
    <div class="login-error" id="signup-error"></div>
    <p class="switch-link">Already have an account? <a href="#" id="show-login-link">Log in</a></p>
  </form>

  <div class="login-card" id="checkout-pending-card" style="display:none;">
    <div class="mark"></div>
    <h1>Confirming Your Payment</h1>
    <p class="sub">This only takes a few seconds - hang tight.</p>
    <div class="login-error" id="checkout-pending-error"></div>
  </div>
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
    <div class="generated">
      Generated __GENERATED_AT__
      <span style="margin-left:10px;">__USER_EMAIL__</span>
      __MAP_LINK__
      <a href="#" id="logout-link" style="color: rgba(255,255,255,0.75); margin-left: 12px; text-decoration: underline; cursor: pointer;">Log Out</a>
    </div>
  </div>
</header>

<main>
  <div class="panel">
    <div class="panel-head">
      <div>
        <h2>Add More Access</h2>
        <div class="sub">Subscribe to another county or the whole-state bundle</div>
      </div>
    </div>
    <div id="add-access-plans" style="display:flex; flex-wrap:wrap; gap:10px; padding:18px 22px;"></div>
    <div class="letter-result" id="add-access-result" style="margin:0 22px 18px;"></div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div>
        <h2>PDF Letter Templates</h2>
        <div class="sub">Upload a company&rsquo;s pre-designed letter PDF to send as-is via Lob</div>
      </div>
      <div class="controls">
        <button type="button" class="ghost" id="create-pdf-template-btn">Create PDF Template</button>
      </div>
    </div>
    <form id="pdf-upload-form" style="display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; padding:20px 24px; border-bottom:1px solid var(--border);">
      <div style="flex:1; min-width:160px;">
        <label for="pdf-company-name" class="field-label">Company Name</label>
        <input type="text" id="pdf-company-name" required class="field-input">
      </div>
      <div style="flex:1; min-width:160px;">
        <label for="pdf-template-name" class="field-label">Template Name</label>
        <input type="text" id="pdf-template-name" required class="field-input">
      </div>
      <div>
        <label for="pdf-file-input" class="field-label">PDF File</label>
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
        <select id="state-filter" class="ghost"><option value="">All States</option></select>
        <select id="county-filter" class="ghost"><option value="">All Counties</option></select>
        <button class="ghost" id="bulk-send-open">Send This Week&rsquo;s Letters</button>
        <button class="ghost" id="export-confirmed">Export CSV</button>
        <button class="ghost" onclick="window.print()">Print</button>
      </div>
    </div>
    <div style="overflow-x:auto;">
    <table id="confirmed-table" style="width:auto; min-width:100%;">
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
    </div>
    <div class="empty" id="confirmed-empty" style="display:none;">__CONFIRMED_EMPTY_MESSAGE__</div>
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
      <div class="letter-price-note" id="letter-price-note"></div>
      <div class="letter-result" id="letter-result"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="ghost" id="letter-cancel">Cancel</button>
      <button type="button" class="primary" id="letter-send">Send Letter</button>
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

      <div class="letter-price-note" id="bulk-price-note"></div>
      <div class="letter-result" id="bulk-result"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="ghost" id="bulk-cancel">Cancel</button>
      <button type="button" class="primary" id="bulk-send" disabled>Send Letters</button>
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
  const letterPriceCents = leadData.letterPriceCents || 0;
  const lobTestMode = !!leadData.lobTestMode;
  const letterPriceDollars = (letterPriceCents / 100).toFixed(2);

  // Lob test mode never actually mails anything, so nothing is charged for
  // it either (see handle_send_letter in dashboard.py) - the note and
  // button label need to say that plainly rather than quote a price that
  // won't actually be billed.
  function letterChargeNote(count) {
    if (lobTestMode) {
      return `Lob test mode: ${count > 1 ? 'these letters are' : 'this letter is'} simulated only — nothing will be mailed and your card will not be charged.`;
    }
    const total = ((letterPriceCents * count) / 100).toFixed(2);
    return count > 1
      ? `Sending will charge $${letterPriceDollars} per letter ($${total} total for ${count}) to the card on file, on top of your monthly subscription.`
      : `Sending will charge $${letterPriceDollars} to the card on file, on top of your monthly subscription.`;
  }

  function letterSendLabel(count) {
    if (lobTestMode) return count > 1 ? 'Send Letters (Test Mode)' : 'Send Letter (Test Mode)';
    const total = ((letterPriceCents * count) / 100).toFixed(2);
    return count > 1 ? `Send Letters ($${total})` : `Send Letter ($${letterPriceDollars})`;
  }

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
  const priceNote = document.getElementById('letter-price-note');
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
    priceNote.textContent = letterChargeNote(1);
    sendBtn.textContent = letterSendLabel(1);
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
      sendBtn.textContent = letterSendLabel(1);
    }
  });

  // --- Bulk "Send This Week's Letters" modal ---
  const bulkBackdrop = document.getElementById('bulk-modal-backdrop');
  const bulkTemplateSelect = document.getElementById('bulk-template-select');
  const bulkList = document.getElementById('bulk-list');
  const bulkResult = document.getElementById('bulk-result');
  const bulkPriceNote = document.getElementById('bulk-price-note');
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
      bulkPriceNote.textContent = '';
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
    const checkedCount = Array.from(bulkSelection.values()).filter(row => row.querySelector('input').checked).length;
    bulkSendBtn.disabled = checkedCount === 0;
    bulkPriceNote.textContent = checkedCount > 0 ? letterChargeNote(checkedCount) : '';
    bulkSendBtn.textContent = letterSendLabel(checkedCount || 1);
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
      : `Sent all ${sentCount} letters${lobTestMode ? ' (Lob test mode).' : '.'}`;
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

  const confirmedState = { query: '', stateFilter: '', countyFolder: '', sortKey: 'dateAdded', sortDir: 'desc' };

  function applyState(list, state) {
    let result = state.stateFilter ? list.filter(l => l.countyState === state.stateFilter) : list.slice();
    if (state.countyFolder) result = result.filter(l => l.countyFolder === state.countyFolder);
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

  const stateFilterSelect = document.getElementById('state-filter');
  const countyFilterSelect = document.getElementById('county-filter');
  const allCounties = leadData.counties || [];

  // Sorted, de-duplicated list of states that actually have a county -
  // built from the counties themselves rather than hand-maintained, so a
  // newly added county's state just shows up here automatically.
  Array.from(new Set(allCounties.map(c => c.state))).sort().forEach(state => {
    const opt = document.createElement('option');
    opt.value = state;
    opt.textContent = state;
    stateFilterSelect.appendChild(opt);
  });

  function repopulateCountyFilter(selectedState) {
    countyFilterSelect.innerHTML = '<option value="">All Counties</option>';
    allCounties
      .filter(c => !selectedState || c.state === selectedState)
      // Same-named counties exist across different states (e.g. many
      // states have their own "Washington County") - spelling out the
      // state here (not just when a state's already picked) is what
      // actually disambiguates them in this dropdown.
      .forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.folder;
        opt.textContent = `${c.label} (${c.state})`;
        countyFilterSelect.appendChild(opt);
      });
  }
  repopulateCountyFilter('');

  stateFilterSelect.addEventListener('change', e => {
    confirmedState.stateFilter = e.target.value;
    // Switching state invalidates whatever county was picked before (it
    // may not even belong to the newly selected state) - reset to "All
    // Counties" within the new state rather than leaving a stale filter.
    confirmedState.countyFolder = '';
    repopulateCountyFilter(confirmedState.stateFilter);
    repaintConfirmed();
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
  document.getElementById('create-pdf-template-btn').addEventListener('click', () => {
    window.location.href = '/create-template';
  });
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

  // --- Add More Access (subscribe to another county/bundle while already logged in) ---
  const addAccessPlansDiv = document.getElementById('add-access-plans');
  const addAccessResult = document.getElementById('add-access-result');
  const ownedCountyFolders = new Set((leadData.counties || []).map(c => c.folder));

  (leadData.pricingPlans || []).forEach(plan => {
    const alreadyOwned = plan.kind === 'county' && ownedCountyFolders.has(plan.key);
    const takenByOther = plan.taken && !alreadyOwned;
    const label = alreadyOwned ? `${plan.label} — Active`
      : takenByOther ? `${plan.label} — Taken`
      : `${plan.label} — $${(plan.priceCents / 100).toFixed(2)}/mo`;
    const btn = el('button', 'ghost', label);
    btn.type = 'button';
    btn.disabled = alreadyOwned || takenByOther;
    if (!alreadyOwned && !takenByOther) {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        addAccessResult.classList.remove('show', 'ok', 'error');
        try {
          const response = await fetch('/api/create-checkout-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: plan.kind, key: plan.key }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || 'Failed to start checkout.');
          window.location.href = data.url;
        } catch (err) {
          addAccessResult.classList.add('show', 'error');
          addAccessResult.textContent = err.message;
          btn.disabled = false;
        }
      });
    }
    addAccessPlansDiv.appendChild(btn);
  });

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
  // Real auth: the server decides __SERVER_AUTHENTICATED__ based on a
  // session cookie it issued after a successful /api/login call, which
  // itself checks the password against Supabase Auth. Unauthenticated
  // responses never embed real lead/mailing data in the page at all (see
  // build_dashboard_html's authenticated=False branch), so there's nothing
  // sensitive to find in the page source before logging in.
  const SERVER_AUTHENTICATED = __SERVER_AUTHENTICATED__;

  const loginScreen = document.getElementById('login-screen');
  const appContent = document.getElementById('app-content');
  const loginForm = document.getElementById('login-form');
  const loginError = document.getElementById('login-error');

  function unlock() {
    loginScreen.style.display = 'none';
    appContent.style.display = 'block';
  }

  if (SERVER_AUTHENTICATED) {
    unlock();
  }

  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;
    loginError.classList.remove('show');
    const submitBtn = loginForm.querySelector('button[type="submit"]');
    const originalLabel = submitBtn.textContent;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Signing in…';
    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Sign in failed.');
      // Left disabled/relabeled on success - the page reload below replaces
      // this whole screen, so there's nothing to reset it for.
      window.location.reload();
    } catch (err) {
      loginError.textContent = err.message;
      loginError.classList.add('show');
      submitBtn.disabled = false;
      submitBtn.textContent = originalLabel;
    }
  });

  const logoutLink = document.getElementById('logout-link');
  if (logoutLink) {
    logoutLink.addEventListener('click', async (e) => {
      e.preventDefault();
      await fetch('/api/logout', { method: 'POST' });
      window.location.reload();
    });
  }

  // --- Claim a signup session after returning from Stripe checkout ---
  // A brand-new signup's Supabase account isn't created until Stripe
  // confirms payment (see billing.handle_webhook_event), which can land a
  // moment after this redirect - so this polls rather than assuming the
  // account (and a session for it) is ready the instant the browser is
  // back on our own domain.
  const signupToken = !SERVER_AUTHENTICATED && new URLSearchParams(window.location.search).get('signup_token');
  if (signupToken) {
    const loginFormEl = document.getElementById('login-form');
    const signupFormEl = document.getElementById('signup-form');
    const pendingCard = document.getElementById('checkout-pending-card');
    const pendingError = document.getElementById('checkout-pending-error');
    loginFormEl.style.display = 'none';
    signupFormEl.style.display = 'none';
    pendingCard.style.display = 'block';

    let attempts = 0;
    const maxAttempts = 14; // ~20s at 1.5s apart
    const tryClaimSession = async () => {
      attempts += 1;
      try {
        const response = await fetch('/api/claim-signup-session', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: signupToken }),
        });
        if (response.status === 200) {
          window.location.href = '/';
          return;
        }
        if (response.status === 404) {
          pendingError.textContent = 'This signup link has expired or was already used.';
          pendingError.classList.add('show');
          return;
        }
      } catch (err) {
        // Network hiccup - fall through to retry below.
      }
      if (attempts < maxAttempts) {
        setTimeout(tryClaimSession, 1500);
      } else {
        pendingError.textContent = 'Payment received! Finishing setup is taking longer than expected - log in with the email and password you just created.';
        pendingError.classList.add('show');
      }
    };
    tryClaimSession();
  }

  // --- Sign up (public paid path - separate from the admin-added accounts above) ---
  const signupForm = document.getElementById('signup-form');
  const signupError = document.getElementById('signup-error');
  const signupPlansDiv = document.getElementById('signup-plans');
  const signupPlanSearch = document.getElementById('signup-plan-search');
  const signupPlansStateNote = document.getElementById('signup-plans-state-note');
  const signupPlansEmpty = document.getElementById('signup-plans-empty');
  const signupFromState = document.getElementById('signup-from-state');
  const showSignupLink = document.getElementById('show-signup-link');
  const showLoginLink = document.getElementById('show-login-link');

  showSignupLink.addEventListener('click', (e) => {
    e.preventDefault();
    loginForm.style.display = 'none';
    signupForm.style.display = 'block';
  });
  showLoginLink.addEventListener('click', (e) => {
    e.preventDefault();
    signupForm.style.display = 'none';
    loginForm.style.display = 'block';
  });

  // The return-address State field is filled in before a plan is chosen
  // (it's earlier on the same form) - once it holds a real 2-letter state
  // that a county plan actually exists in, narrow the list to that state
  // rather than making someone scroll every county in the country to find
  // theirs. The search box narrows further on top of that (or on its own,
  // with no state typed yet) - both filters just AND together.
  const PLAN_STATES = new Set((leadData.pricingPlans || []).map((p) => p.state).filter(Boolean));

  function renderSignupPlans() {
    const stateVal = signupFromState.value.trim().toUpperCase();
    const searchVal = signupPlanSearch.value.trim().toLowerCase();
    const stateFilterActive = stateVal.length === 2 && PLAN_STATES.has(stateVal);

    let plans = leadData.pricingPlans || [];
    if (stateFilterActive) plans = plans.filter((p) => p.state === stateVal);
    if (searchVal) plans = plans.filter((p) => p.label.toLowerCase().includes(searchVal));

    signupPlansStateNote.style.display = stateFilterActive ? 'block' : 'none';
    if (stateFilterActive) signupPlansStateNote.textContent = `Showing ${stateVal} counties`;

    // Keep whatever was already selected if it's still in the filtered
    // list - re-rendering on every keystroke shouldn't silently swap the
    // agent's choice out from under them.
    const previouslyChecked = signupPlansDiv.querySelector('input[name="signup-plan"]:checked');
    const previousValue = previouslyChecked ? previouslyChecked.value : null;

    signupPlansDiv.innerHTML = '';
    plans.forEach((plan) => {
      const option = document.createElement('label');
      option.className = 'plan-option' + (plan.taken ? ' is-taken' : '');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'signup-plan';
      radio.value = `${plan.kind}:${plan.key}`;
      radio.disabled = !!plan.taken;
      if (radio.value === previousValue && !plan.taken) radio.checked = true;
      option.appendChild(radio);
      option.appendChild(el('span', 'plan-label', plan.label));
      // No price shown here on purpose - Adam sometimes closes a county
      // above its listed price (see The Map's price editor), so a number
      // quoted on this public form could go stale the moment that happens.
      // Checkout still charges whatever the plan's current price actually
      // is (see billing.create_checkout_session) - this just stops the
      // signup page from advertising a number that might not be it.
      if (plan.taken) {
        option.appendChild(el('span', 'badge taken', 'Taken'));
      }
      signupPlansDiv.appendChild(option);
    });

    if (!signupPlansDiv.querySelector('input[name="signup-plan"]:checked')) {
      const firstAvailable = signupPlansDiv.querySelector('input[name="signup-plan"]:not(:disabled)');
      if (firstAvailable) firstAvailable.checked = true;
    }
    signupPlansEmpty.style.display = plans.length === 0 ? 'block' : 'none';
  }

  renderSignupPlans();
  signupFromState.addEventListener('input', renderSignupPlans);
  signupPlanSearch.addEventListener('input', renderSignupPlans);

  signupForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    signupError.classList.remove('show');

    const selectedPlan = signupForm.querySelector('input[name="signup-plan"]:checked');
    if (!selectedPlan) {
      signupError.textContent = 'Please choose a plan.';
      signupError.classList.add('show');
      return;
    }
    const [kind, key] = selectedPlan.value.split(':');

    const signupSubmitBtn = signupForm.querySelector('button[type="submit"]');
    const originalSignupLabel = signupSubmitBtn.textContent;
    signupSubmitBtn.disabled = true;
    signupSubmitBtn.textContent = 'Creating account…';

    const body = {
      email: document.getElementById('signup-email').value.trim(),
      password: document.getElementById('signup-password').value,
      fromName: document.getElementById('signup-from-name').value.trim(),
      fromAddress1: document.getElementById('signup-from-address1').value.trim(),
      fromAddress2: document.getElementById('signup-from-address2').value.trim(),
      fromCity: document.getElementById('signup-from-city').value.trim(),
      fromState: document.getElementById('signup-from-state').value.trim(),
      fromZip: document.getElementById('signup-from-zip').value.trim(),
      agreedToTerms: document.getElementById('signup-agree-terms').checked,
      kind, key,
    };

    try {
      const response = await fetch('/api/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Sign up failed.');
      if (data.checkoutUrl) {
        // Left disabled/relabeled - about to navigate away to Stripe.
        window.location.href = data.checkoutUrl;
      } else {
        window.location.reload();
      }
    } catch (err) {
      signupError.textContent = err.message;
      signupError.classList.add('show');
      signupSubmitBtn.disabled = false;
      signupSubmitBtn.textContent = originalSignupLabel;
    }
  });
</script>

<footer style="text-align:center; padding:24px 20px; font-size:11.5px; color:#707b8d;">&copy; 2026 Wright AI Solutions, LLC. All rights reserved.</footer>

</body>
</html>
"""


def build_dashboard_html(authenticated=False, email=None):
    """Re-reads the dated xlsx files and rebuilds the page fresh - called on
    every request so a browser refresh always reflects the latest exports,
    with no need to re-run this script.

    When not authenticated, none of the real lead/mailing data is loaded or
    embedded in the response at all - only the login screen is meaningful,
    so there's nothing for an unauthenticated request to read out of the
    page source either. When authenticated, only the counties this email
    has access to are loaded - either an explicit per-county grant
    (user_county_access) or a whole-state grant (user_state_access, from
    buying the state bundle) that covers every county in that state,
    including ones added after the purchase. Access is opt-in only, a user
    with no grants at all sees nothing."""
    allowed_folders = get_allowed_county_folders(email) if authenticated else set()
    visible_counties = [c for c in COUNTIES if c["folder"] in allowed_folders]

    if authenticated:
        confirmed_leads = load_all_confirmed_leads(visible_counties)
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
    else:
        confirmed_leads, confirmed_count, lead_pursued_rate, pdf_templates = [], 0, "", []

    # Pricing is public/marketing info (not sensitive lead data), so unlike
    # everything else above this is loaded regardless of auth state - the
    # sign-up form on the logged-out login screen needs it to show plan
    # choices before anyone has an account yet.
    # One agent per county: state-bundle plans are no longer offered. A
    # taken county stays in the list (marked taken) rather than disappearing
    # - showing the full footprint of counties is a deliberate marketing
    # choice, not just what's currently purchasable.
    taken_counties = pipeline_db.get_taken_counties()
    # Keyed off the full COUNTIES list (not visible_counties, which is
    # auth-filtered) since signup itself is shown to logged-out visitors -
    # this is just how the signup form knows which state each plan
    # belongs to, for its state/search filtering.
    county_state_by_folder = {c["folder"]: c["state"] for c in COUNTIES}
    pricing_plans = [
        {
            "kind": p["kind"], "key": p["key"], "label": p["label"], "priceCents": p["monthly_price_cents"],
            "state": county_state_by_folder.get(p["key"], ""),
            "taken": p["key"] in taken_counties,
        }
        for p in pipeline_db.get_pricing_plans()
        if p["kind"] == "county"
    ]

    counties = [{"folder": c["folder"], "label": c["label"], "state": c["state"]} for c in visible_counties]
    lead_data = {
        "confirmed": confirmed_leads,
        "templates": LETTER_TEMPLATES,
        "pdfTemplates": pdf_templates,
        "counties": counties,
        "pricingPlans": pricing_plans,
        "letterPriceCents": billing.LETTER_PRICE_CENTS,
        "lobTestMode": mailer.is_test_mode(),
    }

    if not authenticated:
        counties_subtitle = "Sign in to view"
    elif not visible_counties:
        counties_subtitle = "No county access assigned"
    else:
        county_labels = [c["label"] for c in visible_counties]
        counties_subtitle = " & ".join(county_labels) if len(county_labels) <= 3 else f"{len(county_labels)} Utah Counties"

    if authenticated and not visible_counties:
        confirmed_empty_message = (
            "Your account doesn&rsquo;t have access to any counties yet &mdash; "
            "contact your administrator to get access."
        )
    else:
        confirmed_empty_message = "No confirmed leads yet &mdash; run the pipeline to populate this list."

    return (TEMPLATE
        .replace("__GENERATED_AT__", datetime.now().strftime("%B %d, %Y at %-I:%M %p"))
        .replace("__CONFIRMED_COUNT__", str(confirmed_count))
        .replace("__LEAD_PURSUED_RATE__", lead_pursued_rate)
        .replace("__COUNTIES_SUBTITLE__", counties_subtitle)
        .replace("__CONFIRMED_EMPTY_MESSAGE__", confirmed_empty_message)
        .replace("__SERVER_AUTHENTICATED__", "true" if authenticated else "false")
        .replace("__USER_EMAIL__", html.escape(email or ""))
        .replace(
            "__MAP_LINK__",
            '<a href="/map" style="color: rgba(255,255,255,0.75); margin-left: 12px; text-decoration: underline;">The Map</a>'
            if is_admin(email) else "",
        )
        .replace("__LEAD_DATA_JSON__", json.dumps(lead_data))
    )


REQUIRED_SEND_FIELDS = ["fullName", "county", "address", "city", "state", "zip", "templateId"]


def handle_send_letter(payload, sender_email=None):
    """Validates the request, sends via Lob, and records the attempt
    (success or failure) in pipeline_cache.db. Returns (http_status, body_dict).

    The Lob "from" address is the mailing profile of the county's owner -
    not necessarily sender_email - so an admin sending on an agent's behalf
    (see is_admin() / get_allowed_county_folders()) still puts that agent's
    own return address on the letter, not the admin's. For a normal agent
    sending their own county's letters the two are the same email, so this
    is unchanged from before. Falls back to mailer.py's LOB_FROM_* env vars
    (Summit Sotheby's, the original single-operator identity) when the
    county has no owner with a profile."""
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

    owner_email = pipeline_db.get_county_owner(county) if county else None
    if sender_email and owner_email and sender_email.strip().lower() != owner_email.strip().lower():
        print(f"{sender_email} sent a letter for county '{county}' on behalf of {owner_email}.")

    from_address = None
    mailing_profile = pipeline_db.get_mailing_profile(owner_email) if owner_email else None
    if mailing_profile:
        from_address = {
            "name": mailing_profile["from_name"],
            "address_line1": mailing_profile["from_address1"],
            "address_city": mailing_profile["from_city"],
            "address_state": mailing_profile["from_state"],
            "address_zip": mailing_profile["from_zip"],
        }
        if mailing_profile.get("from_address2"):
            from_address["address_line2"] = mailing_profile["from_address2"]

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
                from_address=from_address,
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
                from_address=from_address,
            )

    # The monthly county fee is for data access - sending a letter is a
    # separate, optional, per-use charge on top, billed to the same card
    # the county owner's subscription already uses. An admin testing an
    # unclaimed county (owner_email is None) has no subscription to bill,
    # so that case is left free rather than blocked.
    letter_charge_id = None
    # Lob test mode never actually mails anything - it's purely a
    # send()-succeeds-but-nothing-goes-out sandbox - so there's nothing to
    # charge for regardless of whose county this is.
    if owner_email and not mailer.is_test_mode():
        customer_id = pipeline_db.get_active_customer_id("county", county)
        if not customer_id:
            return 402, {"error": "No active subscription/payment method found for this county - can't send a letter."}
        try:
            charge = billing.charge_for_letter(customer_id, f"Letter fee - {county} - {full_name}")
            letter_charge_id = charge.get("id")
        except billing.BillingError as e:
            return 402, {"error": f"Payment failed, letter not sent: {e}"}

    try:
        result = send()
    except mailer.MailerConfigError as e:
        # Nothing was charged for a config error (it's raised before ever
        # calling Lob), so no refund needed here.
        return 400, {"error": str(e)}
    except mailer.LobAPIError as e:
        # The letter fee was already charged above (has to be, so a failed
        # charge blocks the send) - since Lob then failed to actually mail
        # anything, refund it rather than leave the agent paying for a
        # letter that never went out.
        if letter_charge_id:
            billing.refund_letter_charge(letter_charge_id)
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


LEGAL_PAGE_STYLE = """
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #faf7f1;
    color: #232c3b;
    line-height: 1.6;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 48px 24px 80px; }
  a { color: #7c5e1f; }
  h1 {
    font-family: Georgia, "Iowan Old Style", Palatino, serif;
    font-size: 28px;
    margin-bottom: 4px;
  }
  .updated { color: #707b8d; font-size: 13px; margin-bottom: 36px; }
  h2 {
    font-family: Georgia, "Iowan Old Style", Palatino, serif;
    font-size: 19px;
    margin-top: 34px;
    border-bottom: 1px solid #e9e2d2;
    padding-bottom: 6px;
  }
  p, li { font-size: 14.5px; }
  .back { display: inline-block; margin-bottom: 24px; font-size: 13.5px; }
"""

LEGAL_EFFECTIVE_DATE = "August 4, 2026"
LEGAL_ENTITY_NAME = "Wright AI Solutions, LLC"
LEGAL_CONTACT_EMAIL = "t@thomasewright.com"
LEGAL_GOVERNING_STATE = "Utah"

TERMS_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terms of Service - Probate Property Leads</title>
<style>{LEGAL_PAGE_STYLE}</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; Back</a>
  <h1>Terms of Service</h1>
  <div class="updated">Effective {LEGAL_EFFECTIVE_DATE}</div>

  <p>These Terms of Service ("Terms") govern access to and use of the Probate
  Property Leads platform (the "Service"), operated by {LEGAL_ENTITY_NAME}
  ("we," "us," or "our"). By creating an account or subscribing to the
  Service, you agree to these Terms.</p>

  <h2>1. The Service</h2>
  <p>The Service provides subscribers ("Agents") with access to probate
  and pre-foreclosure property leads for specific counties, tools to send
  physical mail to leads via a third-party mail provider, and related
  account management features.</p>

  <h2>2. Eligibility &amp; Accounts</h2>
  <p>Anyone may create an account, but a county subscription is only
  available while that county is unclaimed - once an agent subscribes to a
  county, it's held exclusively by that agent and isn't available to any
  other subscriber. You're responsible for maintaining the confidentiality
  of your login credentials and for all activity under your account. You
  must provide accurate business/return-address information at signup, since
  it's printed on mail sent through the Service.</p>

  <h2>3. Subscriptions, Billing &amp; Cancellation</h2>
  <p>Subscriptions are billed monthly in advance via our payment
  processor, Stripe. Each county is sold to at most one subscriber at a
  time - once you subscribe to a county, it is not available to any other
  subscriber until your access ends. We do not guarantee that any
  particular county will be available at the time you attempt to
  subscribe.</p>
  <p><strong>Refunds:</strong> subscription fees are non-refundable,
  including for partial billing periods. You may cancel at any time;
  cancellation stops future billing but does not refund the current
  period, and county access continues until the end of the period already
  paid for.</p>
  <p><strong>Per-letter charges:</strong> sending physical mail through the
  Service is optional and billed separately from your subscription, at a
  fixed per-letter rate, charged to the payment method on file at the time
  each letter is sent. If that charge fails, the letter is not sent.</p>

  <h2>4. Acceptable Use</h2>
  <p>You agree not to: share your account or county access with anyone
  outside your own business; resell, sublicense, or redistribute leads or
  Service access to a third party; scrape, export, or reproduce the
  Service's data for use outside the Service; or use the Service for any
  unlawful purpose.</p>

  <h2>5. Lead Data &amp; Your Compliance Obligations</h2>
  <p>Leads are compiled from public records (including county assessor
  and probate records, and published obituaries) and are provided "as
  is," without any guarantee of accuracy, completeness, or continued
  availability. You are solely responsible for ensuring that your own use
  of lead data and any communications you send - including mail sent
  through the Service - complies with all applicable federal, state, and
  local laws, including any laws specific to contacting individuals in
  connection with a probate or estate matter.</p>
  <p>Leads are matched and confirmed using automated processes, including
  automated comparison of names and records. These processes are not
  perfect, do not achieve a 100% success or accuracy rate, and can
  produce incorrect, incomplete, or mismatched results. You should
  independently verify a lead's underlying information before relying on
  it or contacting anyone.</p>

  <h2>6. Intellectual Property</h2>
  <p>The Service, including its software, design, and compiled lead
  data, is owned by {LEGAL_ENTITY_NAME} or its licensors. These Terms
  don't grant you any rights to our intellectual property beyond what's
  needed to use the Service as intended during an active subscription.</p>

  <h2>7. Termination</h2>
  <p>We may suspend or terminate your account for violation of these
  Terms, non-payment, or misuse of leads or the Service. You may stop
  using the Service at any time by canceling your subscription.</p>

  <h2>8. Disclaimer of Warranties</h2>
  <p>The Service is provided "as is" and "as available," without
  warranties of any kind, express or implied, including fitness for a
  particular purpose. We don't guarantee any particular volume, quality,
  or outcome from leads provided through the Service.</p>

  <h2>9. Limitation of Liability</h2>
  <p>To the fullest extent permitted by law, {LEGAL_ENTITY_NAME} will not
  be liable for any indirect, incidental, or consequential damages
  arising from your use of the Service, and our total liability for any
  claim will not exceed the amount you paid us in the three months
  preceding the claim.</p>

  <h2>10. Governing Law</h2>
  <p>These Terms are governed by the laws of the State of
  {LEGAL_GOVERNING_STATE}, without regard to conflict-of-law principles.</p>

  <h2>11. Changes to These Terms</h2>
  <p>We may update these Terms from time to time. Continued use of the
  Service after a change takes effect constitutes acceptance of the
  updated Terms.</p>

  <h2>12. Contact</h2>
  <p>Questions about these Terms can be sent to
  <a href="mailto:{LEGAL_CONTACT_EMAIL}">{LEGAL_CONTACT_EMAIL}</a>.</p>
</div>
<footer style="text-align:center; padding:24px 20px; font-size:11.5px; color:#707b8d;">&copy; 2026 Wright AI Solutions, LLC. All rights reserved.</footer>
</body>
</html>
"""

PRIVACY_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - Probate Property Leads</title>
<style>{LEGAL_PAGE_STYLE}</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; Back</a>
  <h1>Privacy Policy</h1>
  <div class="updated">Effective {LEGAL_EFFECTIVE_DATE}</div>

  <p>This Privacy Policy explains how {LEGAL_ENTITY_NAME} ("we," "us," or
  "our") collects, uses, and shares information in connection with the
  Probate Property Leads platform (the "Service").</p>

  <h2>1. Information We Collect</h2>
  <p>When you create an account, we collect your email address and the
  business name and return-mailing address you provide (used as the
  return address on mail you send through the Service). We do not collect
  or store your payment card details directly - subscription payments are
  handled by our payment processor, Stripe, subject to Stripe's own
  privacy policy.</p>

  <h2>2. Lead Data</h2>
  <p>The Service also provides access to information compiled from public
  records - county assessor and probate records, and published obituaries
  - about property owners and estates. This data is not information
  submitted by you, and this section describes it for transparency rather
  than as data collected "from" you.</p>

  <h2>3. How We Use Information</h2>
  <p>We use account information to operate the Service: authenticating
  logins, billing subscriptions, granting county access, and printing the
  correct return address on mail you send. We don't sell your account
  information to third parties.</p>

  <h2>4. Third-Party Service Providers</h2>
  <p>We rely on the following third parties to operate the Service, each
  of which processes limited data on our behalf under their own privacy
  terms:</p>
  <ul>
    <li><strong>Supabase</strong> - authentication and database hosting</li>
    <li><strong>Stripe</strong> - subscription billing and payment processing</li>
    <li><strong>Lob</strong> - physical mail printing and delivery</li>
  </ul>

  <h2>5. Data Retention</h2>
  <p>We retain account information for as long as your account is active,
  and letter-sending records indefinitely as a business record of mail
  sent through the Service. You can request deletion of your account
  information by contacting us, subject to records we're required to
  keep for billing or legal purposes.</p>

  <h2>6. Data Security</h2>
  <p>We use industry-standard measures - including encrypted connections
  and access-controlled hosting - to protect account information, but no
  method of transmission or storage is completely secure.</p>

  <h2>7. Cookies</h2>
  <p>The Service uses a single session cookie to keep you logged in. We
  don't use advertising or cross-site tracking cookies.</p>

  <h2>8. Children's Privacy</h2>
  <p>The Service is intended for business use by real estate professionals
  and is not directed at children.</p>

  <h2>9. Changes to This Policy</h2>
  <p>We may update this Privacy Policy from time to time. Continued use
  of the Service after a change takes effect constitutes acceptance of
  the updated policy.</p>

  <h2>10. Contact</h2>
  <p>Questions about this policy can be sent to
  <a href="mailto:{LEGAL_CONTACT_EMAIL}">{LEGAL_CONTACT_EMAIL}</a>.</p>
</div>
<footer style="text-align:center; padding:24px 20px; font-size:11.5px; color:#707b8d;">&copy; 2026 Wright AI Solutions, LLC. All rights reserved.</footer>
</body>
</html>
"""

CREATE_TEMPLATE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Create PDF Template - Probate Property Leads</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #faf7f1;
    color: #232c3b;
  }
  header.top {
    background: linear-gradient(135deg, #0a1424, #101d33 55%, #24354f);
    color: #fff;
    padding: 22px 32px;
  }
  header.top a { color: #ecce8a; font-size: 13.5px; text-decoration: none; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 40px 24px 80px; }
  h1 { font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 27px; margin: 0 0 6px; }
  .sub { color: #707b8d; font-size: 14px; margin-bottom: 30px; }
  .progress { display: flex; gap: 6px; margin-bottom: 34px; }
  .progress i { flex: 1; height: 4px; border-radius: 2px; background: #e9e2d2; }
  .progress i.done { background: #a6822e; }
  .card { background: #fff; border: 1px solid #e9e2d2; border-radius: 14px; padding: 30px 32px; }
  .step { display: none; }
  .step.active { display: block; }
  .step h2 { font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 20px; margin: 0 0 4px; }
  .step .step-hint { color: #707b8d; font-size: 13.5px; margin-bottom: 22px; }
  label.field-label { display: block; font-size: 12px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; color: #707b8d; margin-bottom: 6px; }
  .field-input, textarea.field-input {
    width: 100%; padding: 10px 12px; border: 1.5px solid #e9e2d2; border-radius: 8px;
    font-size: 14px; font-family: inherit; background: #fdfcfa; color: #232c3b;
  }
  .field-input:focus, textarea.field-input:focus { outline: none; border-color: #a6822e; }
  .field-row { display: flex; gap: 14px; margin-bottom: 18px; }
  .field-row > div { flex: 1; }
  .choice-option {
    display: flex; align-items: flex-start; gap: 12px; padding: 14px 16px;
    border: 1.5px solid #e9e2d2; border-radius: 10px; margin-bottom: 10px; cursor: pointer; background: #fdfcfa;
  }
  .choice-option:hover { border-color: #ecce8a; }
  .choice-option:has(input:checked) { border-color: #a6822e; background: rgba(166,130,46,0.07); box-shadow: 0 0 0 1px #a6822e; }
  .choice-option input { margin-top: 3px; accent-color: #7c5e1f; }
  .choice-option .choice-title { font-weight: 600; font-size: 14px; }
  .choice-option .choice-desc { font-size: 12.5px; color: #707b8d; margin-top: 2px; }
  .nav-row { display: flex; justify-content: space-between; margin-top: 28px; }
  button { font-family: inherit; cursor: pointer; border-radius: 999px; padding: 11px 22px; font-size: 13.5px; font-weight: 700; border: 1.5px solid transparent; }
  button.ghost { background: #fff; border-color: #e9e2d2; color: #232c3b; }
  button.ghost:hover { border-color: #a6822e; }
  button.primary { background: #101d33; color: #fff; }
  button.primary:hover { background: #24354f; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .logo-preview { width: 64px; height: 64px; border-radius: 8px; border: 1.5px solid #e9e2d2; object-fit: contain; display: none; margin-top: 10px; background: #fff; }
  .result-banner { display: none; margin-top: 18px; padding: 12px 14px; border-radius: 8px; font-size: 13.5px; }
  .result-banner.show.ok { display: block; background: #eaf6ee; color: #2a6b3d; }
  .result-banner.show.error { display: block; background: #fbe4e4; color: #a13a3a; }
  input[type="color"] { width: 52px; height: 40px; padding: 2px; border: 1.5px solid #e9e2d2; border-radius: 8px; background: #fdfcfa; cursor: pointer; }
</style>
</head>
<body>
<header class="top"><a href="/">&larr; Back to dashboard</a></header>
<div class="wrap">
  <h1>Create a PDF Letter Template</h1>
  <div class="sub">Answer a few questions and we'll design a ready-to-send letter template for you - no design tools needed.</div>

  <div class="progress">
    <i data-step="0" class="done"></i><i data-step="1"></i><i data-step="2"></i><i data-step="3"></i><i data-step="4"></i>
  </div>

  <div class="card">
    <form id="wizard-form">
      <div class="step active" data-step="0">
        <h2>Your Branding</h2>
        <div class="step-hint">This appears in the letterhead at the top of every letter.</div>
        <div style="margin-bottom:18px;">
          <label class="field-label" for="w-business-name">Business / Sender Name</label>
          <input type="text" id="w-business-name" class="field-input" required>
        </div>
        <div class="field-row">
          <div>
            <label class="field-label" for="w-phone">Phone</label>
            <input type="text" id="w-phone" class="field-input" placeholder="(555) 123-4567">
          </div>
          <div>
            <label class="field-label" for="w-email">Email</label>
            <input type="email" id="w-email" class="field-input" placeholder="you@example.com">
          </div>
        </div>
        <div class="field-row">
          <div>
            <label class="field-label" for="w-accent-color">Accent Color</label>
            <input type="color" id="w-accent-color" value="#a6822e">
          </div>
          <div>
            <label class="field-label" for="w-logo">Logo (optional)</label>
            <input type="file" id="w-logo" accept="image/png,image/jpeg">
            <img id="w-logo-preview" class="logo-preview" alt="Logo preview">
          </div>
        </div>
      </div>

      <div class="step" data-step="1">
        <h2>Tone</h2>
        <div class="step-hint">How should the letter sound?</div>
        <label class="choice-option">
          <input type="radio" name="w-tone" value="warm" checked>
          <div><div class="choice-title">Warm &amp; Compassionate</div><div class="choice-desc">Leads with empathy for the family's loss before getting to business.</div></div>
        </label>
        <label class="choice-option">
          <input type="radio" name="w-tone" value="direct">
          <div><div class="choice-title">Direct &amp; Professional</div><div class="choice-desc">Gets straight to how you can help, in a businesslike tone.</div></div>
        </label>
        <label class="choice-option">
          <input type="radio" name="w-tone" value="casual">
          <div><div class="choice-title">Casual &amp; Friendly</div><div class="choice-desc">Reads more like a personal note than a form letter.</div></div>
        </label>
      </div>

      <div class="step" data-step="2">
        <h2>Your Offer</h2>
        <div class="step-hint">Select whichever apply - these become bullet points in the letter.</div>
        <label class="choice-option">
          <input type="checkbox" name="w-angle" value="cash" checked>
          <div><div class="choice-title">Cash offer</div><div class="choice-desc">No financing delays or contingencies.</div></div>
        </label>
        <label class="choice-option">
          <input type="checkbox" name="w-angle" value="as_is" checked>
          <div><div class="choice-title">As-is, no repairs needed</div><div class="choice-desc">No cleanout, staging, or repairs required.</div></div>
        </label>
        <label class="choice-option">
          <input type="checkbox" name="w-angle" value="no_fees">
          <div><div class="choice-title">No agent commissions or fees</div></div>
        </label>
        <label class="choice-option">
          <input type="checkbox" name="w-angle" value="fast_close">
          <div><div class="choice-title">Fast closing timeline</div></div>
        </label>
      </div>

      <div class="step" data-step="3">
        <h2>Call to Action</h2>
        <div class="step-hint">How should they get in touch?</div>
        <label class="choice-option">
          <input type="radio" name="w-contact" value="phone" checked>
          <div><div class="choice-title">Phone call</div></div>
        </label>
        <label class="choice-option">
          <input type="radio" name="w-contact" value="text">
          <div><div class="choice-title">Text message</div></div>
        </label>
        <label class="choice-option">
          <input type="radio" name="w-contact" value="email">
          <div><div class="choice-title">Email</div></div>
        </label>
        <div style="margin-top:18px;">
          <label class="field-label" for="w-closing-note">Anything else to add? (optional)</label>
          <textarea id="w-closing-note" class="field-input" rows="3" placeholder="e.g. a personal line about your experience with probate sales"></textarea>
        </div>
      </div>

      <div class="step" data-step="4">
        <h2>Name This Template</h2>
        <div class="step-hint">So you can tell it apart from other templates later.</div>
        <div style="margin-bottom:18px;">
          <label class="field-label" for="w-template-name">Template Name</label>
          <input type="text" id="w-template-name" class="field-input" placeholder="e.g. Warm Cash Offer" required>
        </div>
        <div id="wizard-result" class="result-banner"></div>
      </div>

      <div class="nav-row">
        <button type="button" class="ghost" id="w-back" disabled>Back</button>
        <button type="button" class="primary" id="w-next">Next</button>
        <button type="button" class="primary" id="w-generate" style="display:none;">Generate Template</button>
      </div>
    </form>
  </div>
</div>

<script>
  const steps = Array.from(document.querySelectorAll('.step'));
  const dots = Array.from(document.querySelectorAll('.progress i'));
  let current = 0;

  function showStep(i) {
    steps.forEach(s => s.classList.toggle('active', Number(s.dataset.step) === i));
    dots.forEach(d => d.classList.toggle('done', Number(d.dataset.step) <= i));
    document.getElementById('w-back').disabled = i === 0;
    document.getElementById('w-next').style.display = i === steps.length - 1 ? 'none' : 'inline-block';
    document.getElementById('w-generate').style.display = i === steps.length - 1 ? 'inline-block' : 'none';
  }

  document.getElementById('w-next').addEventListener('click', () => {
    if (current === 0 && !document.getElementById('w-business-name').value.trim()) {
      document.getElementById('w-business-name').focus();
      return;
    }
    current = Math.min(current + 1, steps.length - 1);
    showStep(current);
  });
  document.getElementById('w-back').addEventListener('click', () => {
    current = Math.max(current - 1, 0);
    showStep(current);
  });

  document.getElementById('w-logo').addEventListener('change', (e) => {
    const file = e.target.files[0];
    const preview = document.getElementById('w-logo-preview');
    if (!file) { preview.style.display = 'none'; return; }
    preview.src = URL.createObjectURL(file);
    preview.style.display = 'block';
  });

  document.getElementById('w-generate').addEventListener('click', async () => {
    const templateName = document.getElementById('w-template-name').value.trim();
    const businessName = document.getElementById('w-business-name').value.trim();
    const resultEl = document.getElementById('wizard-result');
    resultEl.classList.remove('show', 'ok', 'error');
    if (!templateName || !businessName) {
      current = 0;
      showStep(current);
      return;
    }

    const generateBtn = document.getElementById('w-generate');
    generateBtn.disabled = true;
    generateBtn.textContent = 'Generating…';

    const formData = new FormData();
    formData.append('companyName', businessName);
    formData.append('templateName', templateName);
    formData.append('businessName', businessName);
    formData.append('phone', document.getElementById('w-phone').value.trim());
    formData.append('email', document.getElementById('w-email').value.trim());
    formData.append('accentColor', document.getElementById('w-accent-color').value);
    formData.append('tone', document.querySelector('input[name="w-tone"]:checked').value);
    formData.append('contactMethod', document.querySelector('input[name="w-contact"]:checked').value);
    formData.append('closingNote', document.getElementById('w-closing-note').value.trim());
    const selectedAngles = Array.from(document.querySelectorAll('input[name="w-angle"]:checked')).map(cb => cb.value);
    formData.append('angles', selectedAngles.join(','));
    const logoFile = document.getElementById('w-logo').files[0];
    if (logoFile) formData.append('logo', logoFile);

    try {
      const response = await fetch('/api/generate-pdf-template', { method: 'POST', body: formData });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Failed to generate template.');
      resultEl.classList.add('show', 'ok');
      resultEl.textContent = 'Template created! Redirecting you back to the dashboard…';
      setTimeout(() => { window.location.href = '/'; }, 1200);
    } catch (err) {
      resultEl.classList.add('show', 'error');
      resultEl.textContent = err.message;
      generateBtn.disabled = false;
      generateBtn.textContent = 'Generate Template';
    }
  });

  showStep(0);
</script>
<footer style="text-align:center; padding:24px 20px; font-size:11.5px; color:#707b8d;">&copy; 2026 Wright AI Solutions, LLC. All rights reserved.</footer>
</body>
</html>
"""


# Admin-only pipeline-progress tracker (not linked anywhere agents can see -
# gated by is_admin() in do_GET). County shapes and names come straight from
# the public "us-atlas" topojson (fetched client-side from jsdelivr, same
# file every d3 US-county-map example uses) - nothing geographic is stored
# or served by this app itself. Only /api/map/status (which county) counts
# as "ready"/"sold"/"building", plus the sold ones' customer email) comes
# from our own backend.
MAP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Map - Probate Property Leads</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #faf7f1;
    color: #232c3b;
  }
  header.top {
    background: linear-gradient(135deg, #0a1424, #101d33 55%, #24354f);
    color: #fff;
    padding: 22px 32px;
  }
  header.top a { color: #ecce8a; font-size: 13.5px; text-decoration: none; }
  header.top h1 { font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 24px; margin: 8px 0 0; }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 30px 24px 80px; }
  .sub { color: #707b8d; font-size: 13.5px; margin: 0 0 16px; }
  .map-card { background: #0a0e17; border: 1px solid #232c3b; border-radius: 14px; padding: 20px; }
  button { font-family: inherit; cursor: pointer; border-radius: 999px; padding: 9px 18px; font-size: 13px; font-weight: 700; border: 1.5px solid #e9e2d2; background: #fff; color: #232c3b; }
  button:hover { border-color: #a6822e; }
  h2#state-heading { font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 21px; margin: 16px 0 4px; }
  .state-shape { fill: #1c2333; stroke: #0a0e17; stroke-width: 0.6; cursor: pointer; transition: fill 0.1s; }
  .state-shape:hover { fill: #323d54; }
  .state-label { font-size: 9.5px; font-weight: 700; fill: #f4f1ea; text-anchor: middle; pointer-events: none; }
  .county-shape { stroke: #0a0e17; stroke-width: 0.6; cursor: pointer; }
  .county-shape:hover { opacity: 0.78; }
  .county-shape.status-not_started { fill: #1c2333; }
  .county-shape.status-building { fill: #d9534f; }
  .county-shape.status-ready { fill: #e8c04a; }
  .county-shape.status-sold { fill: #3f9860; }
  .county-shape.high-pop { stroke: #ecce8a; stroke-width: 2.2; }
  .state-view-body { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
  .state-view-body .map-card { flex: 1 1 480px; min-width: 0; }
  .legend { flex: 0 0 180px; display: flex; flex-direction: column; gap: 12px; font-size: 12.5px; padding-top: 4px; }
  .legend-title { font-size: 11px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; color: #707b8d; margin-bottom: 2px; }
  .legend-item { display: flex; align-items: center; gap: 8px; }
  .legend-swatch { width: 14px; height: 14px; border-radius: 3px; border: 1px solid rgba(0,0,0,0.12); flex-shrink: 0; }
  .panel-overlay {
    position: fixed; inset: 0; background: rgba(10,20,36,0.45);
    display: none; align-items: center; justify-content: center; z-index: 50;
  }
  .panel-overlay.show { display: flex; }
  .county-panel {
    background: #fff; border-radius: 14px; padding: 26px 28px; max-width: 340px;
    width: 90%; position: relative; box-shadow: 0 20px 50px -12px rgba(0,0,0,0.35);
  }
  .county-panel button#county-panel-close {
    position: absolute; top: 8px; right: 12px; border: none; background: none;
    font-size: 20px; color: #707b8d; padding: 4px 8px; border-radius: 999px;
  }
  .county-panel h3 { margin: 0 0 10px; font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 19px; }
  .county-panel p { margin: 4px 0; font-size: 13.5px; }
  .county-panel select, .county-panel input[type="text"], .county-panel input[type="email"],
  .county-panel input[type="tel"], .county-panel input[type="number"], .county-panel textarea {
    margin-top: 10px; width: 100%; padding: 9px 10px; border: 1.5px solid #e9e2d2;
    border-radius: 8px; font-size: 13.5px; font-family: inherit; background: #fdfcfa;
  }
  .county-panel textarea { resize: vertical; }
  .county-panel select:focus, .county-panel input:focus, .county-panel textarea:focus { outline: none; border-color: #a6822e; }
  .county-panel label.field-label { display: block; font-size: 11px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; color: #707b8d; margin-top: 14px; }
  #county-panel-mark-sold, #county-panel-revert-ready, #county-panel-price { margin-top: 14px; }
  #confirm-sold-btn, #save-price-btn { background: #101d33; color: #fff; border-color: #101d33; }
  #confirm-sold-btn:hover, #save-price-btn:hover { background: #24354f; }
  #price-input-row { display: flex; gap: 8px; align-items: center; }
  #price-input-row input { margin-top: 0; }
  .price-error { color: #b3261e; font-size: 12.5px; margin-top: 6px; display: none; }
  .price-error.show { display: block; }
</style>
</head>
<body>

<header class="top">
  <a href="/">&larr; Back to Dashboard</a>
  <h1>The Map</h1>
</header>

<div class="wrap">
  <div id="view-national">
    <p class="sub">Click a state to see its counties. Each state shows (counties with a working script) / (total counties).</p>
    <div class="map-card" id="national-svg-wrap">Loading map&hellip;</div>
  </div>

  <div id="view-state" style="display:none;">
    <button id="back-to-national">&larr; All States</button>
    <h2 id="state-heading"></h2>
    <div class="state-view-body">
      <div class="map-card" id="state-svg-wrap"></div>
      <div class="legend">
        <div class="legend-title">Legend</div>
        <div class="legend-item"><span class="legend-swatch" style="background:#1c2333;"></span>No work done</div>
        <div class="legend-item"><span class="legend-swatch" style="background:#d9534f;"></span>Being built</div>
        <div class="legend-item"><span class="legend-swatch" style="background:#e8c04a;"></span>Ready to sell</div>
        <div class="legend-item"><span class="legend-swatch" style="background:#3f9860;"></span>Sold</div>
        <div class="legend-item"><span class="legend-swatch" style="background:#1c2333; border: 2.5px solid #ecce8a;"></span>75,000+ population</div>
      </div>
    </div>
  </div>
</div>

<div class="panel-overlay" id="county-panel-overlay">
  <div class="county-panel">
    <button id="county-panel-close">&times;</button>
    <h3 id="county-panel-name"></h3>
    <p id="county-panel-status"></p>
    <p id="county-panel-customer" style="display:none;"></p>
    <div id="county-panel-price" style="display:none;">
      <label class="field-label" for="price-input">Monthly Price</label>
      <div id="price-input-row">
        <input type="number" id="price-input" min="0.01" max="100000" step="0.01" placeholder="500.00">
        <button type="button" id="save-price-btn">Save</button>
      </div>
      <p class="price-error" id="price-error"></p>
    </div>
    <div id="county-panel-status-control">
      <label class="field-label" for="county-status-select">Status</label>
      <select id="county-status-select"></select>
    </div>
    <div id="county-panel-mark-sold" style="display:none;">
      <button type="button" id="mark-sold-btn">Mark as Sold</button>
      <div id="mark-sold-form" style="display:none;">
        <label class="field-label" for="sold-customer-name">Customer Name</label>
        <input type="text" id="sold-customer-name" placeholder="Jane Agent">
        <label class="field-label" for="sold-customer-email">Customer Email</label>
        <input type="email" id="sold-customer-email" placeholder="jane@example.com">
        <label class="field-label" for="sold-customer-phone">Agent Phone Number</label>
        <input type="tel" id="sold-customer-phone" placeholder="(555) 555-5555">
        <label class="field-label" for="sold-notes">Notes</label>
        <textarea id="sold-notes" rows="3" placeholder="Anything worth remembering about this sale"></textarea>
        <button type="button" id="confirm-sold-btn" style="margin-top:12px;">Confirm Sale</button>
      </div>
    </div>
    <div id="county-panel-revert-ready" style="display:none;">
      <button type="button" id="revert-ready-btn">Revert to Ready to Sell</button>
    </div>
  </div>
</div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script src="https://unpkg.com/topojson-client@3"></script>
<script>
  const IS_MAP_EDITOR_ONLY = __IS_MAP_EDITOR_ONLY__;
  const WIDTH = 975, HEIGHT = 610;
  const STATUS_LABELS = { not_started: 'Not started', building: 'Being built', ready: 'Ready to sell', sold: 'Sold' };

  // AZ and UT counties with 75,000+ population (2025 Census estimates,
  // en.wikipedia.org/wiki/List_of_counties_in_{Arizona,Utah}) - outlined
  // bold on the map regardless of pipeline status. Static reference data,
  // not worth a backend round-trip for - update by hand if it's ever worth
  // extending past these two states or refreshing the figures.
  const HIGH_POP_COUNTY_FIPS = new Set([
    '04003', '04005', '04013', '04015', '04017', '04019', '04021', '04025', '04027', // AZ: Cochise, Coconino, Maricopa, Mohave, Navajo, Pima, Pinal, Yavapai, Yuma
    '49005', '49011', '49035', '49045', '49049', '49053', '49057', // UT: Cache, Davis, Salt Lake, Tooele, Utah, Washington, Weber
  ]);

  // FIPS codes with a real, working pipeline script (dashboard.py's
  // COUNTY_FOLDER_TO_FIPS) - these can be toggled between "building",
  // "ready", and "sold" (a real script exists, so "not started" doesn't
  // apply). Every other county on the map can only be "not started" or
  // "building" (no script exists yet to sell).
  const REAL_SCRIPT_FIPS = new Set(__REAL_SCRIPT_FIPS_JSON__);

  function escapeXml(str) {
    return String(str || '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;',
    }[c]));
  }

  let statesFeatures = [];
  let countiesFeatures = [];
  let statusMap = {};
  let path;

  const nationalSvgWrap = document.getElementById('national-svg-wrap');
  const stateSvgWrap = document.getElementById('state-svg-wrap');
  const viewNational = document.getElementById('view-national');
  const viewState = document.getElementById('view-state');
  const stateHeading = document.getElementById('state-heading');
  const panelOverlay = document.getElementById('county-panel-overlay');
  const panelName = document.getElementById('county-panel-name');
  const panelStatusText = document.getElementById('county-panel-status');
  const panelCustomer = document.getElementById('county-panel-customer');
  const statusControlWrap = document.getElementById('county-panel-status-control');
  const statusSelect = document.getElementById('county-status-select');
  const markSoldWrap = document.getElementById('county-panel-mark-sold');
  const markSoldBtn = document.getElementById('mark-sold-btn');
  const markSoldForm = document.getElementById('mark-sold-form');
  const soldNameInput = document.getElementById('sold-customer-name');
  const soldEmailInput = document.getElementById('sold-customer-email');
  const soldPhoneInput = document.getElementById('sold-customer-phone');
  const soldNotesInput = document.getElementById('sold-notes');
  const confirmSoldBtn = document.getElementById('confirm-sold-btn');
  const revertReadyWrap = document.getElementById('county-panel-revert-ready');
  const revertReadyBtn = document.getElementById('revert-ready-btn');
  const priceWrap = document.getElementById('county-panel-price');
  const priceInput = document.getElementById('price-input');
  const savePriceBtn = document.getElementById('save-price-btn');
  const priceError = document.getElementById('price-error');
  let currentPanelFips = null;
  let currentPanelName = null;

  function countyStatus(fips) {
    return (statusMap[fips] && statusMap[fips].status) || 'not_started';
  }

  function renderNational() {
    const totalByState = {};
    const readyOrSoldByState = {};
    countiesFeatures.forEach((f) => {
      const stateFips = f.id.slice(0, 2);
      totalByState[stateFips] = (totalByState[stateFips] || 0) + 1;
      const st = countyStatus(f.id);
      if (st === 'ready' || st === 'sold') {
        readyOrSoldByState[stateFips] = (readyOrSoldByState[stateFips] || 0) + 1;
      }
    });

    let svg = `<svg viewBox="0 0 ${WIDTH} ${HEIGHT}" style="width:100%; height:auto; display:block;">`;
    statesFeatures.forEach((f) => {
      const d = path(f);
      if (d) svg += `<path class="state-shape" data-fips="${f.id}" d="${d}"><title>${escapeXml(f.properties.name)}</title></path>`;
    });
    statesFeatures.forEach((f) => {
      const total = totalByState[f.id] || 0;
      if (!total) return;
      const ready = readyOrSoldByState[f.id] || 0;
      const c = path.centroid(f);
      if (!isFinite(c[0]) || !isFinite(c[1])) return;
      svg += `<text class="state-label" x="${c[0]}" y="${c[1]}">${ready}/${total}</text>`;
    });
    svg += `</svg>`;
    nationalSvgWrap.innerHTML = svg;

    nationalSvgWrap.querySelectorAll('.state-shape').forEach((el) => {
      el.addEventListener('click', () => showState(el.getAttribute('data-fips')));
    });
  }

  function showState(stateFips) {
    const feature = statesFeatures.find((f) => f.id === stateFips);
    if (!feature) return;
    const stateCounties = countiesFeatures.filter((f) => f.id.slice(0, 2) === stateFips);

    const bounds = path.bounds(feature);
    const [[x0, y0], [x1, y1]] = bounds;
    const pad = Math.max(x1 - x0, y1 - y0, 1) * 0.08;
    const vb = `${x0 - pad} ${y0 - pad} ${(x1 - x0) + pad * 2} ${(y1 - y0) + pad * 2}`;

    let svg = `<svg viewBox="${vb}" style="width:100%; height:auto; display:block;">`;
    stateCounties.forEach((f) => {
      const d = path(f);
      if (!d) return;
      const st = countyStatus(f.id);
      const rawName = f.properties.name || '';
      const highPopClass = HIGH_POP_COUNTY_FIPS.has(f.id) ? ' high-pop' : '';
      svg += `<path class="county-shape status-${st}${highPopClass}" data-fips="${f.id}" data-name="${escapeXml(rawName)}" d="${d}"><title>${escapeXml(rawName)}</title></path>`;
    });
    svg += `</svg>`;
    stateSvgWrap.innerHTML = stateCounties.length
      ? svg
      : '<p class="sub" style="margin:0;">No county-level data available for this state.</p>';

    stateSvgWrap.querySelectorAll('.county-shape').forEach((el) => {
      el.addEventListener('click', () => openCountyPanel(el.getAttribute('data-fips'), el.getAttribute('data-name')));
    });

    stateHeading.textContent = feature.properties.name;
    viewNational.style.display = 'none';
    viewState.style.display = 'block';
  }

  document.getElementById('back-to-national').addEventListener('click', () => {
    viewState.style.display = 'none';
    viewNational.style.display = 'block';
  });

  function populateStatusSelect(values, current) {
    statusSelect.innerHTML = '';
    values.forEach((value) => {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = STATUS_LABELS[value];
      statusSelect.appendChild(opt);
    });
    statusSelect.value = current;
  }

  function renderPanelStatus(fips, name) {
    const entry = statusMap[fips];
    const status = (entry && entry.status) || 'not_started';
    const hasRealScript = REAL_SCRIPT_FIPS.has(fips);
    panelName.textContent = name;
    panelStatusText.textContent = 'Status: ' + STATUS_LABELS[status];

    if (status === 'sold' && entry && entry.customerEmail) {
      let customerHtml = entry.customerName
        ? `Customer: ${escapeXml(entry.customerName)} (${escapeXml(entry.customerEmail)})`
        : 'Customer: ' + escapeXml(entry.customerEmail);
      if (entry.customerPhone) customerHtml += '<br>Phone: ' + escapeXml(entry.customerPhone);
      if (entry.notes) customerHtml += '<br>Notes: ' + escapeXml(entry.notes);
      panelCustomer.innerHTML = customerHtml;
      panelCustomer.style.display = 'block';
    } else {
      panelCustomer.style.display = 'none';
    }

    markSoldForm.style.display = 'none';
    soldNameInput.value = '';
    soldEmailInput.value = '';
    soldPhoneInput.value = '';
    soldNotesInput.value = '';
    statusControlWrap.style.display = 'none';
    markSoldWrap.style.display = 'none';
    revertReadyWrap.style.display = 'none';
    priceWrap.style.display = 'none';
    priceError.classList.remove('show');
    priceInput.value = '';

    // A real Stripe subscriber (see is_map_only/"real" in dashboard.py) is
    // never editable here regardless of account type - there's nothing to
    // override, that customer's own subscription is the real record.
    if (status === 'sold' && entry && entry.real) return;

    if (status === 'sold') {
      // A sale recorded manually (not a real subscriber) can only be
      // reverted back to "ready to sell" from here - a map-only account
      // never gets this control, only a full admin can undo a sale.
      if (!IS_MAP_EDITOR_ONLY) revertReadyWrap.style.display = 'block';
      return;
    }

    if (!hasRealScript) {
      // No real script yet - "ready"/"sold" don't apply, and a map-only
      // account has no business here at all (nothing for it to mark sold).
      if (!IS_MAP_EDITOR_ONLY) {
        statusControlWrap.style.display = 'block';
        populateStatusSelect(['not_started', 'building'], status);
      }
      return;
    }

    // The price is only ever editable on a county that's actually
    // available to sell right now - once it's sold, changing this
    // wouldn't touch that customer's already-created subscription anyway
    // (see handle_set_county_price). Both roles get this, same as both
    // get to mark a ready county sold.
    if (status === 'ready') {
      priceWrap.style.display = 'block';
      priceInput.value = entry && entry.priceCents != null ? (entry.priceCents / 100).toFixed(2) : '';
    }

    // A real script exists (status is "building" or "ready"): a map-only
    // account can only mark a *ready* county sold - never toggle
    // building/ready - everyone else can freely move between the two, in
    // either direction, plus mark it sold from either state.
    if (IS_MAP_EDITOR_ONLY) {
      markSoldWrap.style.display = status === 'ready' ? 'block' : 'none';
      return;
    }
    statusControlWrap.style.display = 'block';
    populateStatusSelect(['building', 'ready'], status);
    markSoldWrap.style.display = 'block';
  }

  function openCountyPanel(fips, name) {
    currentPanelFips = fips;
    currentPanelName = name;
    renderPanelStatus(fips, name);
    panelOverlay.classList.add('show');
  }

  document.getElementById('county-panel-close').addEventListener('click', () => {
    panelOverlay.classList.remove('show');
  });
  panelOverlay.addEventListener('click', (e) => {
    if (e.target === panelOverlay) panelOverlay.classList.remove('show');
  });

  async function saveCountyStatus(fips, status, customerName, customerEmail, customerPhone, notes) {
    const resp = await fetch('/api/map/set-status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fips, status, customerName, customerEmail, customerPhone, notes }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || 'Failed to save status');
    }
    if (status === 'not_started') {
      delete statusMap[fips];
    } else {
      const entry = { status };
      if (customerName) entry.customerName = customerName;
      if (customerEmail) entry.customerEmail = customerEmail;
      if (customerPhone) entry.customerPhone = customerPhone;
      if (notes) entry.notes = notes;
      // Only "ready" ever shows a price, but carry it forward regardless -
      // a building<->ready toggle shouldn't make the price editor forget
      // it mid-session (a full page reload would recompute it fine either
      // way, this just keeps this session's view in sync without one).
      const previous = statusMap[fips];
      if (previous && previous.priceCents != null) entry.priceCents = previous.priceCents;
      statusMap[fips] = entry;
    }
    const shape = stateSvgWrap.querySelector(`.county-shape[data-fips="${fips}"]`);
    if (shape) {
      shape.classList.remove('status-not_started', 'status-building', 'status-ready', 'status-sold');
      shape.classList.add('status-' + status);
    }
  }

  statusSelect.addEventListener('change', async () => {
    const fips = currentPanelFips;
    const newStatus = statusSelect.value;
    try {
      await saveCountyStatus(fips, newStatus);
      renderPanelStatus(fips, currentPanelName);
    } catch (err) {
      alert(err.message);
    }
  });

  markSoldBtn.addEventListener('click', () => {
    markSoldForm.style.display = 'block';
    soldNameInput.focus();
  });

  confirmSoldBtn.addEventListener('click', async () => {
    const fips = currentPanelFips;
    const customerName = soldNameInput.value.trim();
    const customerEmail = soldEmailInput.value.trim();
    const customerPhone = soldPhoneInput.value.trim();
    const notes = soldNotesInput.value.trim();
    if (!customerEmail || !customerEmail.includes('@')) {
      alert('Enter a valid customer email.');
      return;
    }
    confirmSoldBtn.disabled = true;
    try {
      await saveCountyStatus(fips, 'sold', customerName, customerEmail, customerPhone, notes);
      renderPanelStatus(fips, currentPanelName);
    } catch (err) {
      alert(err.message);
    } finally {
      confirmSoldBtn.disabled = false;
    }
  });

  revertReadyBtn.addEventListener('click', async () => {
    const fips = currentPanelFips;
    revertReadyBtn.disabled = true;
    try {
      await saveCountyStatus(fips, 'ready');
      renderPanelStatus(fips, currentPanelName);
    } catch (err) {
      alert(err.message);
    } finally {
      revertReadyBtn.disabled = false;
    }
  });

  savePriceBtn.addEventListener('click', async () => {
    const fips = currentPanelFips;
    const priceDollars = parseFloat(priceInput.value);
    priceError.classList.remove('show');
    if (!priceDollars || priceDollars <= 0) {
      priceError.textContent = 'Enter a valid price.';
      priceError.classList.add('show');
      return;
    }
    savePriceBtn.disabled = true;
    try {
      const resp = await fetch('/api/map/set-price', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fips, priceDollars }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || 'Failed to save price.');
      const entry = statusMap[fips] || { status: 'ready' };
      entry.priceCents = data.priceCents;
      statusMap[fips] = entry;
      priceInput.value = (data.priceCents / 100).toFixed(2);
    } catch (err) {
      priceError.textContent = err.message;
      priceError.classList.add('show');
    } finally {
      savePriceBtn.disabled = false;
    }
  });

  async function init() {
    const [topoResp, statusResp] = await Promise.all([
      fetch('https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json'),
      fetch('/api/map/status'),
    ]);
    const us = await topoResp.json();
    statusMap = statusResp.ok ? await statusResp.json() : {};

    statesFeatures = topojson.feature(us, us.objects.states).features;
    countiesFeatures = topojson.feature(us, us.objects.counties).features;

    const projection = d3.geoAlbersUsa().fitSize([WIDTH, HEIGHT], { type: 'FeatureCollection', features: statesFeatures });
    path = d3.geoPath(projection);

    renderNational();
  }

  init().catch((err) => {
    nationalSvgWrap.textContent = 'Failed to load the map: ' + err.message;
  });
</script>

<footer style="text-align:center; padding:24px 20px; font-size:11.5px; color:#707b8d;">&copy; 2026 Wright AI Solutions, LLC. All rights reserved.</footer>

</body>
</html>
"""


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/terms", "/privacy"):
            body = (TERMS_HTML if self.path == "/terms" else PRIVACY_HTML).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # A map-only account (see is_map_only) never sees the real dashboard,
        # the PDF wizard, or anything else - it's bounced straight to the one
        # page it's allowed to use.
        if is_map_only(get_session_email(self)) and self.path not in ("/map", "/api/map/status"):
            self.send_response(302)
            self.send_header("Location", "/map")
            self.end_headers()
            return

        if self.path == "/create-template":
            if not get_session_email(self):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            body = CREATE_TEMPLATE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/map":
            email = get_session_email(self)
            if not (is_admin(email) or is_map_editor(email)):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            page = (
                MAP_HTML
                .replace("__IS_MAP_EDITOR_ONLY__", "true" if is_map_only(email) else "false")
                .replace("__REAL_SCRIPT_FIPS_JSON__", json.dumps(list(COUNTY_FOLDER_TO_FIPS.values())))
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/map/status":
            email = get_session_email(self)
            if not (is_admin(email) or is_map_editor(email)):
                self._send_json(403, {"error": "Admin access required."})
                return
            status, response = handle_map_status()
            self._send_json(status, response)
            return

        if self.path.startswith("/pdf-templates/"):
            if not get_session_email(self):
                self._send_json(401, {"error": "Not signed in."})
                return
            rest = self.path[len("/pdf-templates/"):]
            if rest.endswith("/preview.png"):
                self._serve_pdf_template_preview(rest[: -len("/preview.png")])
            else:
                self._serve_pdf_template(rest)
            return

        email = get_session_email(self)
        body = build_dashboard_html(authenticated=bool(email), email=email).encode("utf-8")
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

        if self.path == "/api/login":
            self._handle_login(raw_body)
            return

        if self.path == "/api/signup":
            self._handle_signup(raw_body)
            return

        if self.path == "/api/claim-signup-session":
            self._handle_claim_signup_session(raw_body)
            return

        if self.path == "/api/logout":
            destroy_session(self)
            self._send_json(200, {"ok": True}, clear_cookie=True)
            return

        if self.path == "/api/stripe-webhook":
            self._handle_stripe_webhook(raw_body)
            return

        if not get_session_email(self):
            self._send_json(401, {"error": "Not signed in."})
            return

        # A map-only account (see is_map_only) can't reach any other
        # authenticated endpoint - not through the UI (it's redirected away,
        # see do_GET), and not by hitting an API directly either.
        if is_map_only(get_session_email(self)) and self.path not in ("/api/map/set-status", "/api/map/set-price"):
            self._send_json(403, {"error": "This account only has access to The Map."})
            return

        if self.path == "/api/upload-pdf-template":
            fields = _parse_multipart(self.headers.get("Content-Type", ""), raw_body)
            try:
                status, response = handle_upload_pdf_template(fields)
            except Exception as e:
                print(f"Unexpected error uploading PDF template: {e}")
                status, response = 500, {"error": "Unexpected server error - check the terminal running dashboard.py."}
            self._send_json(status, response)
            return

        if self.path == "/api/generate-pdf-template":
            fields = _parse_multipart(self.headers.get("Content-Type", ""), raw_body)
            try:
                status, response = handle_generate_pdf_template(fields)
            except Exception as e:
                print(f"Unexpected error generating PDF template: {e}")
                status, response = 500, {"error": "Unexpected server error - check the terminal running dashboard.py."}
            self._send_json(status, response)
            return

        if self.path == "/api/create-checkout-session":
            self._handle_create_checkout_session(raw_body)
            return

        if self.path == "/api/map/set-status":
            email = get_session_email(self)
            if not (is_admin(email) or is_map_editor(email)):
                self._send_json(403, {"error": "Admin access required."})
                return
            try:
                payload = json.loads(raw_body or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON body"})
                return
            status, response = handle_set_county_status(payload, restrict_to_marking_sold=is_map_only(email))
            self._send_json(status, response)
            return

        if self.path == "/api/map/set-price":
            email = get_session_email(self)
            if not (is_admin(email) or is_map_editor(email)):
                self._send_json(403, {"error": "Admin access required."})
                return
            try:
                payload = json.loads(raw_body or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON body"})
                return
            try:
                status, response = handle_set_county_price(payload)
            except Exception as e:
                print(f"Unexpected error setting county price: {e}")
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

        if self.path == "/api/send-letter":
            email = get_session_email(self)
            county = str(payload.get("county", "")).strip()
            if county not in get_allowed_county_folders(email):
                self._send_json(403, {"error": "You don't have access to that county."})
                return

        try:
            if self.path == "/api/send-letter":
                status, response = handler(payload, get_session_email(self))
            else:
                status, response = handler(payload)
        except Exception as e:
            print(f"Unexpected error handling {self.path}: {e}")
            status, response = 500, {"error": "Unexpected server error - check the terminal running dashboard.py."}

        self._send_json(status, response)

    def _handle_login(self, raw_body):
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        email = str(payload.get("email", "")).strip()
        password = str(payload.get("password", ""))
        if not email or not password:
            self._send_json(400, {"error": "Email and password are required."})
            return

        try:
            signed_in_email, _ = auth.sign_in(email, password)
        except auth.LoginError:
            self._send_json(401, {"error": "Incorrect email or password."})
            return
        except RuntimeError as e:
            self._send_json(500, {"error": str(e)})
            return

        token = create_session(signed_in_email)
        self._send_json(200, {"ok": True, "email": signed_in_email}, set_cookie=token)

    def _handle_signup(self, raw_body):
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        email = str(payload.get("email", "")).strip().lower()
        password = str(payload.get("password", ""))
        from_name = str(payload.get("fromName", "")).strip()
        from_address1 = str(payload.get("fromAddress1", "")).strip()
        from_address2 = str(payload.get("fromAddress2", "")).strip()
        from_city = str(payload.get("fromCity", "")).strip()
        from_state = str(payload.get("fromState", "")).strip()
        from_zip = str(payload.get("fromZip", "")).strip()
        kind = str(payload.get("kind", "")).strip()
        key = str(payload.get("key", "")).strip()
        agreed_to_terms = bool(payload.get("agreedToTerms"))

        required = [email, password, from_name, from_address1, from_city, from_state, from_zip, kind, key]
        if not all(required):
            self._send_json(400, {"error": "All fields are required (Suite/Unit is optional)."})
            return
        if not agreed_to_terms:
            self._send_json(400, {"error": "You must agree to the Terms of Service and Privacy Policy."})
            return
        if kind != "county":
            self._send_json(400, {"error": "Invalid plan selected."})
            return

        if pipeline_db.get_county_owner(key):
            self._send_json(400, {"error": "That county is no longer available - it's already assigned to another agent."})
            return

        # A mailing profile only ever exists alongside a real account under
        # this flow (both are created together once payment clears, below),
        # so this doubles as the "email already registered" check without
        # needing a Supabase admin lookup.
        if pipeline_db.get_mailing_profile(email):
            self._send_json(400, {"error": "An account with that email already exists - please log in instead."})
            return

        # No Supabase account is created here. It's only created once Stripe
        # confirms payment (see billing.handle_webhook_event) - that way an
        # abandoned or declined checkout never leaves a real, logged-in
        # account behind with nothing paid for. The signup form's data is
        # held in memory under this token until the webhook resolves it.
        pending_token = billing.stash_pending_signup({
            "email": email, "password": password, "from_name": from_name,
            "from_address1": from_address1, "from_address2": from_address2,
            "from_city": from_city, "from_state": from_state, "from_zip": from_zip,
        })

        base_url = self._base_url()
        try:
            checkout_url = billing.create_checkout_session(
                email, kind, key,
                success_url=f"{base_url}/?checkout=success&signup_token={pending_token}",
                cancel_url=f"{base_url}/?checkout=cancel",
                extra_metadata={"pending_signup_token": pending_token},
            )
        except billing.BillingError as e:
            billing.pop_pending_signup(pending_token)
            self._send_json(400, {"error": f"Couldn't start checkout: {e}"})
            return

        self._send_json(200, {"ok": True, "checkoutUrl": checkout_url})

    def _handle_claim_signup_session(self, raw_body):
        """Polled by the frontend after Stripe redirects back from checkout
        for a brand-new signup. The account doesn't exist until the webhook
        (a separate, async server-to-server call from Stripe) has landed and
        resolved the pending signup below - which can be a moment after this
        redirect - so this can legitimately return "still pending" a few
        times before it succeeds."""
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        token = str(payload.get("token", "")).strip()
        pending = billing.get_pending_signup(token) if token else None
        if not pending:
            self._send_json(404, {"error": "Signup session not found or already used."})
            return
        if not pending.get("resolved_email"):
            self._send_json(202, {"pending": True})
            return

        email = pending["resolved_email"]
        billing.pop_pending_signup(token)
        session_token = create_session(email)
        self._send_json(200, {"ok": True, "email": email}, set_cookie=session_token)

    def _base_url(self):
        host = self.headers.get("Host", f"127.0.0.1:{PORT}")
        scheme = "http" if host.startswith("127.0.0.1") or host.startswith("localhost") else "https"
        return f"{scheme}://{host}"

    def _handle_create_checkout_session(self, raw_body):
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        email = get_session_email(self)
        kind = str(payload.get("kind", "")).strip()
        key = str(payload.get("key", "")).strip()
        if kind != "county" or not key:
            self._send_json(400, {"error": "Invalid plan."})
            return

        if key in pipeline_db.get_counties_for_user(email):
            self._send_json(400, {"error": "You already have access to that plan."})
            return

        if pipeline_db.get_county_owner(key):
            self._send_json(400, {"error": "That county is no longer available - it's already assigned to another agent."})
            return

        base_url = self._base_url()
        try:
            checkout_url = billing.create_checkout_session(
                email, kind, key,
                success_url=f"{base_url}/?checkout=success",
                cancel_url=f"{base_url}/?checkout=cancel",
            )
        except billing.BillingError as e:
            self._send_json(400, {"error": str(e)})
            return

        self._send_json(200, {"url": checkout_url})

    def _handle_stripe_webhook(self, raw_body):
        sig_header = self.headers.get("Stripe-Signature", "")
        try:
            event_type = billing.handle_webhook_event(raw_body, sig_header)
        except billing.BillingError as e:
            print(f"Stripe webhook rejected: {e}")
            self._send_json(400, {"error": str(e)})
            return
        except Exception as e:
            print(f"Unexpected error handling Stripe webhook: {e}")
            self._send_json(500, {"error": "Unexpected server error."})
            return

        print(f"Stripe webhook handled: {event_type}")
        self._send_json(200, {"received": True})

    def _send_json(self, status, payload, set_cookie=None, clear_cookie=False):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}={set_cookie}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_DURATION_SECONDS}",
            )
        if clear_cookie:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The default access log (one line per request, including favicon
        # requests) is noisy for a single-user local tool - just note refreshes.
        print(f"Dashboard refreshed at {datetime.now().strftime('%H:%M:%S')}")


def main():
    # Also write a static snapshot to disk - login-screen only, no real
    # data embedded, since a plain file on disk can't enforce the server's
    # session check. Open http://127.0.0.1:8765/ (with the server running)
    # to actually sign in and see real data.
    with open(OUTPUT_PATH, "w") as f:
        f.write(build_dashboard_html(authenticated=False))

    # Render (and most PaaS hosts) set PORT and expect the app to bind to
    # 0.0.0.0 so it's reachable from outside the container. Locally, no PORT
    # is set, so this keeps behaving exactly as before: 127.0.0.1:8765 with
    # the browser auto-opened.
    is_hosted = "PORT" in os.environ
    host = "0.0.0.0" if is_hosted else "127.0.0.1"
    port = int(os.environ.get("PORT", PORT))

    try:
        # ThreadingHTTPServer instead of plain HTTPServer so one slow
        # request (e.g. a PDF render) doesn't block every other user -
        # matters once this has real concurrent traffic, not just one operator.
        server = http.server.ThreadingHTTPServer((host, port), DashboardRequestHandler)
    except OSError as e:
        print(f"Couldn't start the dashboard server on port {port}: {e}")
        print("Is dashboard.py already running in another terminal? If so, just refresh that browser tab.")
        raise SystemExit(1)

    url = f"http://{'127.0.0.1' if not is_hosted else host}:{port}/"
    print(f"Dashboard live at {url} - refresh the page any time to see the latest data.")
    print("Press Ctrl+C to stop.")
    if not is_hosted:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
        server.shutdown()


if __name__ == "__main__":
    main()
