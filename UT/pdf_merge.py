"""Stamps a recipient's name onto a company-uploaded PDF letter template at
send time, and renders a PDF's first page as a PNG so the dashboard can show
a click-to-place preview when the operator sets where that name should go.

Needs reportlab (draws the overlay text), pypdf (merges the overlay onto the
uploaded PDF), and pypdfium2 (renders a PDF page to an image with no system
dependencies like poppler). Install with:
    pip3 install reportlab pypdf pypdfium2
"""

import io

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

TONE_OPENERS = {
    "warm": (
        "I hope this letter finds you as well as can be during this time. "
        "I'm reaching out about the property that may be part of a loved one's "
        "estate, and I wanted to share a few options that might make this "
        "process a little easier for you."
    ),
    "direct": (
        "I'm writing regarding the property that may be part of an estate "
        "you're currently settling. I work with families in exactly this "
        "situation and wanted to outline how I might be able to help."
    ),
    "casual": (
        "I know settling an estate can bring up a lot of decisions, so I "
        "wanted to reach out directly about the property and share a "
        "straightforward option if it's helpful."
    ),
}

ANGLE_BULLETS = {
    "cash": "A straightforward cash offer, with no financing delays or contingencies to worry about.",
    "as_is": "The property can be sold exactly as it is today - no repairs, cleanout, or staging needed.",
    "no_fees": "No agent commissions or listing fees come out of what you receive.",
    "fast_close": "A closing timeline that can move as quickly as you need it to.",
}

CONTACT_CTA = {
    "phone": "feel free to call me anytime",
    "text": "feel free to send me a text anytime",
    "email": "feel free to reach out by email anytime",
}


def _wrap_text(text, font, size, max_width, c):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if c.stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_paragraph(c, text, x, y, max_width, font="Helvetica", size=11, leading=15):
    for line in _wrap_text(text, font, size, max_width, c):
        c.drawString(x, y, line)
        y -= leading
    return y


def generate_letter_pdf(business_name, phone, email, accent_hex, tone, angles, contact_method,
                         closing_note="", logo_bytes=None):
    """Builds a one-page probate-inquiry letter from a company's own
    branding and messaging choices (collected by the template-creation
    wizard), the same way a company would otherwise design one by hand in
    Canva/Word and upload it. Returns (pdf_bytes, name_x_ratio, name_y_ratio)
    - the ratio is where "Dear " ends on the page, in the same top-left-
    origin ratio system stamp_owner_name()/set_pdf_template_field_position()
    already use, so the recipient's name can be stamped in immediately with
    no separate manual click-to-place step."""
    accent_hex = (accent_hex or "").strip().lstrip("#")
    if len(accent_hex) != 6:
        accent_hex = "a6822e"  # the platform's own default gold
    accent = HexColor(f"#{accent_hex}")
    ink = HexColor("#232c3b")
    muted = HexColor("#707b8d")

    page_width, page_height = letter
    margin = 72
    max_width = page_width - 2 * margin

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    logo_reserved_width = 0
    if logo_bytes:
        try:
            reader = ImageReader(io.BytesIO(logo_bytes))
            logo_w, logo_h = reader.getSize()
            box = 64
            scale = min(box / logo_w, box / logo_h)
            draw_w, draw_h = logo_w * scale, logo_h * scale
            c.drawImage(
                reader, page_width - margin - draw_w, page_height - margin - draw_h,
                width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto",
            )
            logo_reserved_width = box + 16
        except Exception:
            logo_reserved_width = 0

    y = page_height - margin
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, y, business_name)

    y -= 20
    c.setFillColor(muted)
    c.setFont("Helvetica", 10)
    contact_line = " • ".join(p for p in [phone, email] if p)
    if contact_line:
        c.drawString(margin, y, contact_line)

    y -= 14
    c.setStrokeColor(accent)
    c.setLineWidth(1)
    c.line(margin, y, page_width - margin, y)

    y -= 40
    c.setFillColor(ink)
    c.setFont("Helvetica", 11)
    salutation_prefix = "Dear "
    c.drawString(margin, y, salutation_prefix)
    # No trailing comma drawn here: at a fixed x-position it would overlap
    # and visually corrupt itself for any recipient name long enough to
    # reach it (confirmed live - happens well within normal full-name
    # lengths). The name is stamped in at send time with nothing after it,
    # same as "Dear John Smith" on its own line.
    prefix_width = c.stringWidth(salutation_prefix, "Helvetica", 11)

    name_x_ratio = (margin + prefix_width) / page_width
    name_y_ratio = (page_height - y) / page_height

    y -= 28
    opener = TONE_OPENERS.get(tone, TONE_OPENERS["direct"])
    y = _draw_paragraph(c, opener, margin, y, max_width)

    selected_bullets = [ANGLE_BULLETS[a] for a in angles if a in ANGLE_BULLETS]
    if selected_bullets:
        y -= 12
        y = _draw_paragraph(
            c, "If you're the person handling this property, here's what I can offer:",
            margin, y, max_width,
        )
        y -= 6
        for bullet in selected_bullets:
            y = _draw_paragraph(c, f"•  {bullet}", margin + 14, y, max_width - 14)

    y -= 12
    cta = CONTACT_CTA.get(contact_method, CONTACT_CTA["phone"])
    closing_parts = [f"Please {cta} - there's no obligation, and I'm happy to answer any questions."]
    if closing_note.strip():
        closing_parts.append(closing_note.strip())
    y = _draw_paragraph(c, " ".join(closing_parts), margin, y, max_width)

    y -= 30
    c.drawString(margin, y, "Sincerely,")
    y -= 34
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, business_name)
    c.setFont("Helvetica", 11)
    y -= 14
    if phone:
        c.drawString(margin, y, phone)
        y -= 14
    if email:
        c.drawString(margin, y, email)

    c.save()
    buf.seek(0)
    return buf.getvalue(), name_x_ratio, name_y_ratio


def render_first_page_png(pdf_bytes, target_width=900):
    """Renders page 1 as a PNG for the click-to-place UI. Returns
    (png_bytes, page_width_pt, page_height_pt) - the point dimensions aren't
    needed by the caller (positions are stored as ratios, not points) but are
    handy for debugging."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    page = pdf[0]
    page_width_pt, page_height_pt = page.get_size()
    scale = target_width / page_width_pt
    bitmap = page.render(scale=scale)
    image = bitmap.to_pil()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), page_width_pt, page_height_pt


def stamp_owner_name(pdf_bytes, x_ratio, y_ratio, owner_name, font="Helvetica", font_size=11):
    """Overlays owner_name onto page 1 at the given position and returns the
    merged PDF's bytes. Position is stored/passed as a ratio (0-1) of the
    page's width/height rather than absolute points, so it doesn't depend on
    whatever DPI the click-to-place preview image happened to be rendered
    at - only on the underlying PDF page's own size, read fresh here.

    x_ratio is left-to-right same as on screen. y_ratio is measured from the
    TOP of the page (matching image/click coordinates), and gets flipped
    here since PDF coordinates put (0, 0) at the bottom-left."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    base_page = reader.pages[0]
    page_width = float(base_page.mediabox.width)
    page_height = float(base_page.mediabox.height)

    x = x_ratio * page_width
    y = page_height - (y_ratio * page_height)

    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=(page_width, page_height))
    c.setFont(font, font_size)
    c.drawString(x, y, owner_name)
    c.save()
    overlay_buf.seek(0)

    overlay_page = PdfReader(overlay_buf).pages[0]
    base_page.merge_page(overlay_page)

    writer = PdfWriter()
    writer.add_page(base_page)
    for page in reader.pages[1:]:
        writer.add_page(page)

    out_buf = io.BytesIO()
    writer.write(out_buf)
    return out_buf.getvalue()
