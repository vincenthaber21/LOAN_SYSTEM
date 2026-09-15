"""Fill the KAP Microfinance loan application form (MFU Form 2015-002-B, Revised 001).

The official form (converted from the KAP-supplied .docx) is stored as a
one-page PDF template.  Application values, tick marks, ID photos and digital
signatures are drawn on a transparent overlay that is merged onto the
template, so the printed hard copy is the genuine KAP form.
"""

from __future__ import annotations

import io
from pathlib import Path

from django.conf import settings
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdf_canvas

PAGE_W, PAGE_H = 612.0, 936.0  # 8.5in x 13in (long bond), matches the template
FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
INK = colors.HexColor("#0b2a6f")  # blue "pen" ink so entries stand out from the print


def _template_path() -> Path:
    return Path(settings.BASE_DIR) / "static" / "lending" / "kap" / "form_template.pdf"


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _date(value) -> str:
    return value.strftime("%m/%d/%Y") if value else ""


def _money(amount) -> str:
    return f"PHP {amount:,.2f}" if amount is not None else ""


def _full_name(*parts) -> str:
    return " ".join(p for p in (_text(x) for x in parts) if p)


def _surname_first(surname, first, middle) -> str:
    surname, rest = _text(surname), _full_name(first, middle)
    if surname and rest:
        return f"{surname}, {rest}"
    return surname or rest


def _file_path(field):
    """Return a filesystem path for an ImageField value, or None."""
    try:
        if field and field.name and Path(field.path).exists():
            return field.path
    except Exception:
        pass
    return None


def _transparent_signature(path):
    """Return an in-memory RGBA copy of a signature: white knocked out, cropped to the ink."""
    from PIL import Image, ImageFilter

    with Image.open(path) as im:
        rgba = im.convert("RGBA")
    # Stroke mask: dark or opaque pixels become ink, everything light becomes transparent.
    grey = rgba.convert("L")
    alpha = rgba.getchannel("A")
    mask = Image.eval(grey, lambda v: 255 - v if v < 225 else 0)  # darker -> more opaque
    mask = Image.composite(mask, Image.new("L", mask.size, 0), alpha.point(lambda a: 255 if a > 0 else 0))
    mask = mask.filter(ImageFilter.MaxFilter(3))  # slight thicken so thin pad strokes still read
    r, g, b = (int(INK.red * 255), int(INK.green * 255), int(INK.blue * 255))
    out = Image.new("RGBA", rgba.size, (r, g, b, 0))
    out.putalpha(mask)
    bbox = mask.getbbox()
    if bbox:
        pad_x = max(4, int((bbox[2] - bbox[0]) * 0.06))
        pad_y = max(4, int((bbox[3] - bbox[1]) * 0.12))
        out = out.crop((
            max(0, bbox[0] - pad_x),
            max(0, bbox[1] - pad_y),
            min(out.width, bbox[2] + pad_x),
            min(out.height, bbox[3] + pad_y),
        ))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf


class _Overlay:
    """Thin helper around a reportlab canvas using top-left (x, y) coordinates in points."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.c = pdf_canvas.Canvas(self.buffer, pagesize=(PAGE_W, PAGE_H))
        self.c.setFillColor(INK)
        self.c.setStrokeColor(INK)

    @staticmethod
    def _y(y_top: float) -> float:
        return PAGE_H - y_top

    def text(self, x, y, value, width=None, size=7.5, bold=False, upper=True, align="left", min_size=4.5):
        value = _text(value)
        if not value:
            return
        if upper:
            value = value.upper()
        font = FONT_BOLD if bold else FONT
        if width:
            while size > min_size and stringWidth(value, font, size) > width:
                size -= 0.25
            while stringWidth(value, font, size) > width and len(value) > 1:
                value = value[:-1]
        self.c.setFont(font, size)
        if align == "center":
            self.c.drawCentredString(x, self._y(y), value)
        else:
            self.c.drawString(x, self._y(y), value)

    def tick(self, cx, cy, size=7.5):
        """Draw a pen-style check mark centred on a form checkbox."""
        cy = self._y(cy)
        h = size / 2
        self.c.saveState()
        self.c.setLineWidth(1.3)
        self.c.setLineCap(1)
        self.c.setLineJoin(1)
        path = self.c.beginPath()
        path.moveTo(cx - h * 0.9, cy + h * 0.05)
        path.lineTo(cx - h * 0.25, cy - h * 0.75)
        path.lineTo(cx + h * 1.05, cy + h * 0.95)
        self.c.drawPath(path, stroke=1, fill=0)
        self.c.restoreState()

    def image(self, path, x0, y0, x1, y1, pad=2, knockout_white=False):
        """Fit an image inside the top-left box (x0, y0)-(x1, y1), preserving aspect ratio.

        With ``knockout_white`` the white background of a scanned/drawn signature is
        made transparent so the pen strokes sit over the printed form.
        """
        if not path:
            return
        try:
            reader = ImageReader(_transparent_signature(path) if knockout_white else path)
            iw, ih = reader.getSize()
            bw, bh = (x1 - x0) - 2 * pad, (y1 - y0) - 2 * pad
            scale = min(bw / iw, bh / ih)
            w, h = iw * scale, ih * scale
            x = x0 + pad + (bw - w) / 2
            y = self._y(y1) + pad + (bh - h) / 2
            self.c.drawImage(reader, x, y, width=w, height=h, mask="auto")
        except Exception:
            pass

    def finish(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buffer.getvalue()


# Co-borrower column is shifted right by this many points relative to the borrower column.
_COL_DX = 292.7


def _draw_person(ov: _Overlay, app, prefix: str, left: bool):
    """Fill one BORROWER / CO-BORROWER column. `prefix` is 'borrower' or 'coborrower'."""

    def g(name):
        return getattr(app, f"{prefix}_{name}", None)

    def display(name):
        getter = getattr(app, f"get_{prefix}_{name}_display", None)
        return getter() if getter else ""

    dx = 0 if left else _COL_DX

    ov.text(18.8 + dx, 351, _surname_first(g("surname"), g("first_name"), g("middle_name")), width=283)
    ov.text(18.8 + dx, 379, g("present_address"), width=283)

    ov.text(89 + dx, 393, g("period_of_staying"), width=29, size=6.5)
    ov.text(174 + dx, 393, display("dwelling_ownership"), width=129, size=6.5)

    ov.text(18.8 + dx, 419, g("permanent_address"), width=283)

    ov.text(74 + dx, 433, _date(g("date_of_birth")), width=44, size=6.5)
    ov.text(184 + dx, 433, g("place_of_birth"), width=119, size=6.5)

    ov.text(73 + dx, 447, g("id_presented"), width=45, size=6.5)
    ov.text(145 + dx, 447, g("age"), width=50, size=6.5)
    ov.text(238 + dx, 447, display("gender"), width=65, size=6.5)

    ov.text(67 + dx, 460, g("nationality"), width=51, size=6.5)
    ov.text(174 + dx, 460, display("civil_status"), width=21, size=5, min_size=3.5)
    ov.text(252 + dx, 460, g("occupation"), width=51, size=6.5)

    ov.text(87 + dx, 474, _money(g("monthly_income")), width=31, size=5.5, min_size=4)
    ov.text(195 + dx, 474, g("tel_mobile"), width=108, size=6.5)


def _build_overlay(app) -> bytes:
    ov = _Overlay()

    # ---- Header ----
    ov.text(266, 71, app.branch_name, width=120)
    ov.text(450, 71, app.form_ref_no or app.reference, width=119)

    # ---- Left column: date, application type, proposed plan payment, amount, loan purpose ----
    ov.text(124, 139, _date(app.applied_on), width=92)
    if app.application_type == "new":
        ov.tick(51.9, 167.6)
    elif app.application_type == "renew":
        ov.tick(51.9, 181.4)

    payment = {"daily": (41.15, 216.5), "weekly": (94.2, 216.2), "biweekly": (157.8, 216.2)}
    if app.payment_frequency in payment:
        ov.tick(*payment[app.payment_frequency])
    ov.text(80, 241, _money(app.amount_requested), width=130)

    purpose = {"general": (51.9, 267.9), "business": (52.2, 283.3), "agricultural": (51.9, 298.9)}
    if app.loan_purpose in purpose:
        ov.tick(*purpose[app.loan_purpose])

    # ---- 2x2 pictures and relationship ----
    ov.image(_file_path(app.borrower_photo), 250.8, 127.8, 405.8, 282.8)
    ov.image(_file_path(app.coborrower_photo), 429.0, 127.8, 584.0, 282.8)
    ov.text(393, 301, app.get_coborrower_relationship_display(), width=163, size=6.5)

    # ---- Borrower / Co-borrower ----
    _draw_person(ov, app, "borrower", left=True)
    _draw_person(ov, app, "coborrower", left=False)

    # ---- Enterprise data ----
    ov.text(93, 509, app.primary_business, width=255)
    ov.text(423, 509, app.business_name, width=173)

    ownership = {
        "owned": (117.3, 517.6),
        "rented": (159.1, 517.1),
        "mortgaged": (203.8, 517.5),
        "used_free": (261.9, 516.9),
    }
    if app.business_ownership in ownership:
        ov.tick(*ownership[app.business_ownership])
    ov.text(430, 522, app.business_address, width=166, size=6.5)

    registrations = (
        (app.reg_dti, (104.75, 531.45)),
        (app.reg_barangay, (134.8, 531.45)),
        (app.reg_mayor, (251.15, 531.45)),
        (app.reg_bir, (324.4, 531.45)),
    )
    for flag, center in registrations:
        if flag:
            ov.tick(*center)
    ov.text(432, 535, app.years_in_operation, width=38, size=5.5, min_size=4)
    ov.text(574, 535, app.persons_employed, width=22, size=5, min_size=3.5)

    for y, n in ((549, 1), (562, 2)):
        ov.text(68, y, getattr(app, f"additional_business_{n}_type"), width=118, size=6.5)
        ov.text(223, y, getattr(app, f"additional_business_{n}_name"), width=125, size=6.5)
        ov.text(393, y, getattr(app, f"additional_business_{n}_address"), width=203, size=6.5)

    # ---- Character references ----
    refs = list(app.character_references.all().order_by("sort_order", "pk")[:2])
    for y, ref in zip((611, 627), refs):
        ov.text(47, y, ref.name, width=116, size=6.5)
        ov.text(208, y, ref.address, width=126, size=6.5)
        ov.text(396, y, ref.relationship, width=78, size=6.5)
        ov.text(524, y, ref.contact_number, width=71, size=6.5)

    # ---- Signatures over printed names ----
    for cx, prefix in ((170.65, "borrower"), (440.55, "coborrower")):
        def g(name, _prefix=prefix):
            return getattr(app, f"{_prefix}_{name}", None)

        printed = g("signed_name") or _full_name(g("first_name"), g("middle_name"), g("surname"))
        # Signature above the line; printed name on the line (drawn last so it stays clear).
        ov.image(_file_path(g("signature")), cx - 85, 748, cx + 85, 772, pad=0, knockout_white=True)
        ov.text(cx, 788, printed, width=200, bold=True, align="center")
        parts = []
        if g("signed_date"):
            parts.append(f"DATE: {_date(g('signed_date'))}")
        if g("signed_place"):
            parts.append(f"PLACE: {_text(g('signed_place'))}")
        ov.text(cx, 814, "   ".join(parts), width=220, size=6.5, align="center")

    # ---- Loan recommendation / approval (office use) ----
    ov.text(75, 847, _money(app.recommended_loan_amount), width=110, size=6.5)
    ov.text(245, 847, app.recommended_loan_period, width=43, size=6.5)
    ov.text(93, 862, app.recommended_by_name, width=92, size=6.5)
    ov.text(218, 862, _date(app.recommended_by_date), width=70, size=6.5)
    ov.text(72, 884, app.validated_by_name, width=113, size=6.5)
    ov.text(218, 884, _date(app.validated_by_date), width=70, size=6.5)

    ov.text(319, 883, app.branch_manager_name, width=83, size=6.5)
    ov.text(414, 883, _date(app.branch_manager_date), width=65, size=6.5)

    decision = {"approved": (512.8, 842.3), "disapproved": (513.8, 862.0), "hold": (513.0, 884.1)}
    if app.office_decision in decision:
        ov.tick(*decision[app.office_decision])

    return ov.finish()


def build_kap_application_pdf(application) -> bytes:
    """Return the official KAP form filled with this application's data (single page)."""
    template = PdfReader(str(_template_path()))
    page = template.pages[0]
    overlay = PdfReader(io.BytesIO(_build_overlay(application))).pages[0]
    page.merge_page(overlay)

    writer = PdfWriter()
    writer.add_page(page)
    writer.add_metadata({
        "/Title": f"KAP Loan Application {application.reference}",
        "/Author": "KAP Microfinancing Inc.",
    })
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
