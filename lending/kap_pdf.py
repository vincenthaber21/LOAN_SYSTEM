"""Fill the original KAP Microfinance loan application form (MPU Form 2015-002 B).

The original form (converted from the official .docx) is stored as a one-page
PDF template.  Application values, tick marks, ID photos and digital
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
    """Return an in-memory RGBA copy of a signature image: white knocked out, strokes in ink colour."""
    from PIL import Image, ImageFilter

    with Image.open(path) as im:
        rgba = im.convert("RGBA")
    # Stroke mask: dark or opaque pixels become ink, everything light becomes transparent.
    grey = rgba.convert("L")
    alpha = rgba.getchannel("A")
    mask = Image.eval(grey, lambda v: 255 - v if v < 225 else 0)  # darker -> more opaque
    mask = Image.composite(mask, Image.new("L", mask.size, 0), alpha.point(lambda a: 255 if a > 0 else 0))
    mask = mask.filter(ImageFilter.MaxFilter(3))  # thicken thin pen strokes slightly
    r, g, b = (int(INK.red * 255), int(INK.green * 255), int(INK.blue * 255))
    out = Image.new("RGBA", rgba.size, (r, g, b, 0))
    out.putalpha(mask)
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


def _draw_person(ov: _Overlay, app, prefix: str, left: bool):
    """Fill one BORROWER / CO-BORROWER column. `prefix` is 'borrower' or 'coborrower'."""

    def g(name):
        return getattr(app, f"{prefix}_{name}", None)

    dx = 0 if left else 288  # co-borrower column is shifted right by 288pt

    ov.text(22 + dx, 317, _surname_first(g("surname"), g("first_name"), g("middle_name")), width=276)
    ov.text(22 + dx, 338, g("present_address"), width=276)
    ov.text(22 + dx, 360, g("municipality_city"), width=206)
    ov.text(236 + dx, 360, g("period_of_staying"), width=63)

    dwelling = {"owned": 109, "rented": 152.5, "mortgaged": 196, "used_free": 252}
    if g("dwelling_ownership") in dwelling:
        ov.tick(dwelling[g("dwelling_ownership")] + dx, 374.5)

    ov.text(22 + dx, 401, g("permanent_address"), width=76)
    ov.text(104 + dx, 401, g("permanent_municipality_city"), width=112)
    ov.text(222 + dx, 401, g("tel_mobile"), width=76)

    ov.text(85 + dx, 428, _date(g("date_of_birth")), width=92)
    ov.text(184 + dx, 428, g("age"), width=32)
    citizenship = {"filipino": 228, "others": 268}
    if g("citizenship") in citizenship:
        ov.tick(citizenship[g("citizenship")] + dx, 427.5)

    ov.text(85 + dx, 452, g("place_of_birth"), width=92)
    gender = {"male": 228, "female": 265}
    if g("gender") in gender:
        ov.tick(gender[g("gender")] + dx, 449.5)

    civil = {"single": 90.5, "married": 129, "widowed": 174.5, "separated": 226}
    if g("civil_status") in civil:
        ov.tick(civil[g("civil_status")] + dx, 466.5)

    ov.text(70 + dx, 488, g("nationality"), width=90)
    ov.text(208 + dx, 488, g("occupation"), width=90)
    ov.text(84 + dx, 502, g("id_presented"), width=214)
    ov.text(90 + dx, 528, g("spouse_name"), width=208)


def _build_overlay(app) -> bytes:
    ov = _Overlay()

    # ---- Header ----
    ov.text(232, 76, app.branch_name, width=72)
    ov.text(352, 76, app.form_ref_no or app.reference, width=78)
    ov.text(515, 76, _date(app.applied_on), width=62)

    # ---- Left column: date, application type, proposed plan payment ----
    ov.text(118, 109, _date(app.applied_on), width=68)
    if app.application_type == "new":
        ov.tick(59, 142.5)
    elif app.application_type == "renew":
        ov.tick(59, 154.5)

    if app.payment_frequency == "daily":
        ov.tick(72, 195)
    elif app.payment_frequency == "weekly":
        ov.tick(127, 195)
    elif app.payment_frequency:
        # The form only prints Daily / Weekly; write other schedules beside them.
        ov.text(163, 198, app.get_payment_frequency_display(), width=26, size=6.5)
    ov.text(120, 209, _money(app.amount_requested), width=68)

    purpose = {"additional_capital": 234.5, "existing_improvement": 247, "others": 261}
    if app.loan_purpose in purpose:
        ov.tick(37, purpose[app.loan_purpose])
    if app.loan_purpose == "others" or (not app.loan_purpose and app.purpose):
        ov.text(76, 263, app.purpose, width=100)

    # ---- 2x2 pictures and relationship ----
    ov.image(_file_path(app.borrower_photo), 230.25, 113.84, 378.59, 257.79)
    ov.image(_file_path(app.coborrower_photo), 416.71, 113.84, 565.05, 257.79)
    if app.coborrower_relationship == "spouse":
        ov.tick(398, 272)
    elif app.coborrower_relationship == "others":
        ov.tick(454, 272)

    # ---- Borrower / Co-borrower ----
    _draw_person(ov, app, "borrower", left=True)
    _draw_person(ov, app, "coborrower", left=False)

    # ---- Enterprise data ----
    ov.text(104, 568, app.primary_business, width=195)
    ov.text(362, 568, app.business_name, width=228)
    ownership = {"owned": 109, "rented": 149, "mortgaged": 190, "used_free": 242}
    if app.business_ownership in ownership:
        ov.tick(ownership[app.business_ownership], 578.5)
    ov.text(338, 585, app.business_address, width=252)

    registrations = (
        (app.reg_dti, 109),
        (app.reg_barangay, 135),
        (app.reg_mayor, 229),
        (app.reg_bir, 285),
        (app.reg_others, 311),
    )
    for flag, cx in registrations:
        if flag:
            ov.tick(cx, 597)
    ov.text(344, 599, app.reg_others_text, width=40, size=6.5)
    ov.text(466, 599, app.years_in_operation, width=40)
    ov.text(514, 606.5, app.persons_employed, width=74, size=5.5)

    for y, n in ((619, 1), (636, 2)):
        ov.text(104, y, getattr(app, f"additional_business_{n}_type"), width=136)
        ov.text(272, y, getattr(app, f"additional_business_{n}_name"), width=126)
        ov.text(435, y, getattr(app, f"additional_business_{n}_address"), width=155)

    # ---- Character references ----
    refs = list(app.character_references.all().order_by("sort_order", "pk")[:2])
    for y, ref in zip((687, 704), refs):
        ov.text(49, y, ref.name, width=108)
        ov.text(200, y, ref.address, width=138)
        ov.text(394, y, ref.relationship, width=90)
        ov.text(533, y, ref.contact_number, width=57)

    # ---- Signatures over printed names ----
    for cx, x_dp, prefix in ((153.5, 72, "borrower"), (457, 370, "coborrower")):
        def g(name, _prefix=prefix):
            return getattr(app, f"{_prefix}_{name}", None)

        ov.image(_file_path(g("signature")), cx - 80, 764, cx + 80, 793, pad=0, knockout_white=True)
        printed = g("signed_name") or _full_name(g("first_name"), g("middle_name"), g("surname"))
        ov.text(cx, 794, printed, width=150, bold=True, align="center")
        parts = []
        if g("signed_date"):
            parts.append(f"DATE: {_date(g('signed_date'))}")
        if g("signed_place"):
            parts.append(f"PLACE: {_text(g('signed_place'))}")
        ov.text(x_dp, 813, "   ".join(parts), width=165, size=6.5)

    # ---- Loan recommendation / approval (office use) ----
    ov.text(78, 841, _money(app.recommended_loan_amount), width=48, size=6.5)
    ov.text(178, 841, app.recommended_loan_period, width=34, size=6.5)
    ov.text(30, 869, app.recommended_by_name, width=96, size=6.5)
    ov.text(157, 862, _date(app.recommended_by_date), width=54, size=6.5)
    ov.text(30, 892, app.validated_by_name, width=96, size=6.5)
    ov.text(157, 885, _date(app.validated_by_date), width=54, size=6.5)

    ov.text(264, 838, _money(app.recommended_loan_amount), width=54, size=6.5)
    ov.text(286, 850, _money(app.hold_out_amount), width=32, size=6.5)
    insurance = {"life_3yr": 879, "pog": 889, "none": 899}
    if app.insurance_proposed in insurance:
        ov.tick(229, insurance[app.insurance_proposed])

    ov.text(344, 875, app.branch_manager_name, width=100, size=6.5)
    ov.text(452, 875, _date(app.branch_manager_date), width=52, size=6.5)
    decision = {"approved": 856, "disapproved": 871, "hold": 885}
    if app.office_decision in decision:
        ov.tick(527, decision[app.office_decision])

    return ov.finish()


def build_kap_application_pdf(application) -> bytes:
    """Return the original KAP form filled with this application's data (single page)."""
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
