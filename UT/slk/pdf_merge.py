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
from reportlab.pdfgen import canvas


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
