"""Printable passbook: the member's repayment schedule with signature lines."""

from __future__ import annotations

import io
from decimal import Decimal
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import Features
from .services import (
    adjust_payment,
    application_schedule_view_mode,
    original_schedule_display_rows,
    schedule_display_rows,
)

INK = colors.HexColor("#163a14")
INK_SOFT = colors.HexColor("#4a6a48")
TEAL = colors.HexColor("#186010")
MINT = colors.HexColor("#e0f6eb")
LINE = colors.HexColor("#d5e5d8")
ZEBRA = colors.HexColor("#f4faf6")
WHITE = colors.white

_VIEW_LABELS = {
    "day": "Daily",
    "week": "Weekly",
    "biweek": "Biweekly",
    "month": "Monthly",
}


def _plain(value) -> str:
    """Map punctuation Helvetica cannot draw onto plain ASCII."""
    text = "" if value is None else str(value)
    for src, dest in (
        ("\u2014", "-"),
        ("\u2013", "-"),
        ("\u00b7", " / "),
        ("\u2022", "-"),
        ("\u20b1", "PHP "),
    ):
        text = text.replace(src, dest)
    return text


def _money(amount) -> str:
    value = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    return f"{value:,.2f}"


def _date(value) -> str:
    if not value:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%b %d, %Y")
    return str(value)


def _normalize_view(view_mode) -> str:
    aliases = {
        "day": "day",
        "daily": "day",
        "week": "week",
        "weekly": "week",
        "biweek": "biweek",
        "biweekly": "biweek",
        "month": "month",
        "monthly": "month",
    }
    return aliases.get(str(view_mode or "").lower(), "month")


def _due_label(row, view_mode) -> str:
    if view_mode == "day":
        return escape(_date(row.get("due_date")))
    label = escape(str(row.get("label") or ""))
    span = f"{_date(row.get('start_date'))} - {_date(row.get('end_date'))}"
    days = int(row.get("day_count") or 0)
    day_note = f"{days} working day{'' if days == 1 else 's'}" if days else ""
    bits = [f"<b>{label}</b>" if label else "", escape(span)]
    if day_note:
        bits.append(f"<font color='#4a6a48'>{escape(day_note)}</font>")
    return "<br/>".join(bit for bit in bits if bit)


def _address(application) -> str:
    parts = [
        (application.borrower_present_address or "").strip(),
        (application.borrower_municipality_city or "").strip(),
    ]
    text = ", ".join(part for part in parts if part)
    return _plain(text) or "-"


def _schedule_for(loan, view_mode, showing_original):
    if showing_original:
        display = original_schedule_display_rows(loan, view_mode=view_mode)
    else:
        display = schedule_display_rows(loan, view_mode=view_mode)
    schedule = list(display["schedule"])
    if view_mode == "day" and not showing_original:
        installments = list(loan.installments.all())
        for row, item in zip(schedule, installments):
            row["amount_paid"] = item.amount_paid
    return display, schedule


def build_passbook_schedule_pdf(loan, *, view_mode=None, showing_original=False) -> bytes:
    """Return a passbook PDF for this loan's repayment schedule."""
    view_mode = _normalize_view(view_mode or application_schedule_view_mode(loan))
    showing_original = bool(showing_original and loan.is_rescheduled)
    features = Features.load()
    application = loan.application
    display, schedule = _schedule_for(loan, view_mode, showing_original)
    original_terms = (display.get("original_terms") or loan.original_schedule_terms()) if showing_original else None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        title=f"Passbook schedule {loan.reference}",
        author=features.tagline or features.store_name,
    )
    styles = _styles()
    story = []
    story.extend(_header(loan, features, styles, view_mode, showing_original))
    story.append(Spacer(1, 3 * mm))
    story.append(_summary_table(loan, application, styles, view_mode, showing_original, original_terms))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(_plan_blurb(loan, view_mode, showing_original, original_terms, len(schedule)), styles["note"]))
    story.append(Spacer(1, 3 * mm))
    story.append(_schedule_table(schedule, view_mode, styles))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "Balance is the scheduled amount still payable after each period is collected in full. "
        "Amount due is the cash collection (rounded up to the next PHP 5). "
        "The collector signs beside each period when it is paid.",
        styles["footnote"],
    ))

    def _page(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.4)
        width, height = A4
        canvas.line(12 * mm, 12 * mm, width - 12 * mm, 12 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(INK_SOFT)
        canvas.drawString(12 * mm, 7.5 * mm, f"{features.tagline or features.store_name}  |  Passbook schedule  |  {loan.reference}")
        canvas.drawRightString(width - 12 * mm, 7.5 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buffer.getvalue()


def _styles():
    return {
        "brand": ParagraphStyle(
            "pb_brand",
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=TEAL,
        ),
        "company": ParagraphStyle(
            "pb_company",
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=INK,
        ),
        "title": ParagraphStyle(
            "pb_title",
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=17,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
        "meta": ParagraphStyle(
            "pb_meta",
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=INK_SOFT,
            alignment=TA_RIGHT,
        ),
        "label": ParagraphStyle(
            "pb_label",
            fontName="Helvetica",
            fontSize=7,
            leading=9,
            textColor=INK_SOFT,
        ),
        "value": ParagraphStyle(
            "pb_value",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11,
            textColor=INK,
        ),
        "note": ParagraphStyle(
            "pb_note",
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=INK,
        ),
        "th": ParagraphStyle(
            "pb_th",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9,
            textColor=WHITE,
        ),
        "th_right": ParagraphStyle(
            "pb_th_right",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9,
            textColor=WHITE,
            alignment=TA_RIGHT,
        ),
        "th_center": ParagraphStyle(
            "pb_th_center",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9,
            textColor=WHITE,
            alignment=TA_CENTER,
        ),
        "td": ParagraphStyle(
            "pb_td",
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=INK,
        ),
        "td_right": ParagraphStyle(
            "pb_td_right",
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
        "td_center": ParagraphStyle(
            "pb_td_center",
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=INK,
            alignment=TA_CENTER,
        ),
        "td_strong": ParagraphStyle(
            "pb_td_strong",
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
        "footnote": ParagraphStyle(
            "pb_footnote",
            fontName="Helvetica-Oblique",
            fontSize=7.5,
            leading=10,
            textColor=INK_SOFT,
        ),
    }


def _header(loan, features, styles, view_mode, showing_original):
    logo_path = Features.default_logo_source_path()
    logo = ""
    if logo_path is not None:
        logo = Image(str(logo_path), width=16 * mm, height=16 * mm, kind="proportional", mask="auto")
    brand = Paragraph(escape((features.store_name or "KAP").upper()), styles["brand"])
    company = Paragraph(escape(features.tagline or "KAP Microfinancing Inc."), styles["company"])
    left = [brand, company]
    title = "Original passbook schedule" if showing_original else "Passbook schedule"
    right = [
        Paragraph(title, styles["title"]),
        Paragraph(
            f"{escape(loan.reference)}<br/>{_VIEW_LABELS[view_mode]} collections<br/>"
            f"Printed {_date(timezone.localdate())}",
            styles["meta"],
        ),
    ]
    left_flow = Table([[item] for item in left], colWidths=[120 * mm])
    left_flow.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    right_flow = Table([[item] for item in right], colWidths=[58 * mm])
    right_flow.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
    ]))
    banner = Table(
        [[logo, left_flow, right_flow]],
        colWidths=[18 * mm, 110 * mm, 58 * mm],
    )
    banner.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, TEAL),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
    ]))
    return [banner]


def _pair(label, value, styles):
    return [
        Paragraph(escape(label), styles["label"]),
        Paragraph(escape(_plain(value)), styles["value"]),
    ]


def _summary_table(loan, application, styles, view_mode, showing_original, original_terms):
    if showing_original and original_terms:
        principal = _money(original_terms["principal"])
        rate = f"{original_terms['interest_rate']}%"
        term = f"{original_terms['term_months']} months"
        payable = _money(original_terms["total_payable"])
        per_day = original_terms["per_day"]
        if view_mode == "week":
            due = adjust_payment(per_day * Decimal(5))
            due_label = "Weekly collection"
        elif view_mode == "biweek":
            due = adjust_payment(per_day * Decimal(10))
            due_label = "Biweekly collection"
        elif view_mode == "month":
            due = adjust_payment(original_terms["per_month"])
            due_label = "Monthly collection"
        else:
            due = adjust_payment(per_day)
            due_label = "Daily collection"
        collection = _money(due)
        balance = _money(original_terms["total_payable"])
        opened = _date(loan.disbursed_date)
        maturity = "Original plan"
    else:
        principal = _money(loan.principal)
        if loan.is_rescheduled:
            rate = f"{loan.reschedule_rate}% (in principal)"
        else:
            rate = f"{loan.interest_rate}% fixed"
        term = _plain(f"{loan.term_months} months / {loan.term_weeks_label}")
        payable = _money(loan.adjusted_total_payable)
        due_amounts = {
            "day": (loan.adjusted_daily_payment, "Daily collection"),
            "week": (loan.adjusted_weekly_payment, "Weekly collection"),
            "biweek": (loan.adjusted_biweekly_payment, "Biweekly collection"),
            "month": (loan.adjusted_monthly_payment, "Monthly collection"),
        }
        due, due_label = due_amounts[view_mode]
        collection = _money(due)
        balance = _money(loan.adjusted_outstanding_balance)
        opened = loan.start_date
        maturity = loan.maturity_date

    rows = [
        _pair("Member", application.borrower_name, styles) + _pair("Loan reference", loan.reference, styles),
        _pair("Product", loan.product_name, styles) + _pair("Pay", application.get_payment_frequency_display(), styles),
        _pair("Address", _address(application), styles) + _pair("Opened", opened, styles),
        _pair("Maturity", maturity, styles) + _pair("Term", term, styles),
        _pair("Principal (PHP)", principal, styles) + _pair("Interest", rate, styles),
        _pair(due_label + " (PHP)", collection, styles) + _pair("Total payable (PHP)", payable, styles),
        _pair("Remaining (PHP)", balance, styles) + _pair("Branch", (application.branch_name or "").strip() or "-", styles),
    ]
    table = Table(rows, colWidths=[32 * mm, 61 * mm, 32 * mm, 61 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), MINT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("LINEAFTER", (1, 0), (1, -1), 0.3, LINE),
    ]))
    return table


def _plan_blurb(loan, view_mode, showing_original, original_terms, count):
    frequency = _VIEW_LABELS[view_mode].lower()
    if showing_original and original_terms:
        return (
            f"Original plan for {escape(loan.application.borrower_name)} from disbursement on {escape(loan.start_date)}. "
            f"{original_terms['term_months']}-month term at {original_terms['interest_rate']}%. "
            f"{count} {frequency} payment{'' if count == 1 else 's'}."
        )
    extra = ""
    if loan.is_rescheduled:
        extra = (
            f" Rescheduled on {escape(loan.reschedule_date_display or '')}; "
            f"collections start {escape(loan.reschedule_interest_start_display or '')}."
        )
    return (
        f"{frequency.title()} passbook for {escape(loan.application.borrower_name)}. "
        f"Pay {escape(loan.application.get_payment_frequency_display())}."
        f"{extra} {count} payment line{'' if count == 1 else 's'}."
    )


def _schedule_table(schedule, view_mode, styles):
    header = [
        Paragraph("#", styles["th_center"]),
        Paragraph("Due", styles["th"]),
        Paragraph("Amount due", styles["th_right"]),
        Paragraph("Collected", styles["th_right"]),
        Paragraph("Balance", styles["th_right"]),
        Paragraph("Status", styles["th_center"]),
        Paragraph("Collector signature", styles["th"]),
    ]
    data = [header]
    balance = sum((Decimal(str(row.get("amount") or 0)) for row in schedule), Decimal("0.00"))
    balance = balance.quantize(Decimal("0.01"))
    status_colors = {
        "paid": "186010",
        "overdue": "8f3a3c",
        "pending": "6a5a12",
    }
    for index, row in enumerate(schedule):
        exact = Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01"))
        balance = (balance - exact).quantize(Decimal("0.01"))
        if index == len(schedule) - 1 and abs(balance) <= Decimal("0.05"):
            balance = Decimal("0.00")
        paid = row.get("amount_paid")
        collected = "-" if paid in (None, "") else _money(paid)
        status_label = str(row.get("status_label") or row.get("status") or "")
        color = status_colors.get(str(row.get("status") or "").lower(), "4a6a48")
        data.append([
            Paragraph(str(row.get("installment_number") or index + 1), styles["td_center"]),
            Paragraph(_due_label(row, view_mode), styles["td"]),
            Paragraph(_money(row.get("adjusted_amount")), styles["td_strong"]),
            Paragraph(collected, styles["td_right"]),
            Paragraph(_money(balance), styles["td_right"]),
            Paragraph(f"<font color='#{color}'><b>{escape(status_label or '-')}</b></font>", styles["td_center"]),
            Paragraph("", styles["td"]),
        ])

    if len(data) == 1:
        data.append([
            Paragraph("-", styles["td_center"]),
            Paragraph("The repayment schedule is not available yet.", styles["td"]),
            Paragraph("", styles["td"]),
            Paragraph("", styles["td"]),
            Paragraph("", styles["td"]),
            Paragraph("", styles["td"]),
            Paragraph("", styles["td"]),
        ])

    widths = [
        10 * mm,
        48 * mm,
        24 * mm,
        22 * mm,
        24 * mm,
        18 * mm,
        40 * mm,
    ]
    table = Table(data, colWidths=widths, repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LINEBELOW", (6, 1), (6, -1), 0.4, colors.HexColor("#9bb59a")),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, TEAL),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
    ]
    table.setStyle(TableStyle(commands))
    return table
