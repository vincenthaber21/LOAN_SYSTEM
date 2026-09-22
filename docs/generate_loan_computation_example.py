"""Generate a professional sample loan computation PDF (flat interest, pay until paid)."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT = Path(__file__).resolve().parent / "loan_computation_example.pdf"
LOGO = BASE_DIR / "static" / "branding" / "kap_logo.png"

# KAP brand
INK = colors.HexColor("#163a14")
INK_SOFT = colors.HexColor("#4a6a48")
TEAL = colors.HexColor("#186010")
TEAL_DEEP = colors.HexColor("#12480c")
MINT = colors.HexColor("#e0f6eb")
LINE = colors.HexColor("#d5e5d8")
WHITE = colors.white

WORKING_DAYS_PER_MONTH = 22


def adjust_payment(amount: Decimal) -> Decimal:
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        return Decimal("0.00")
    if amount % Decimal("5") == 0:
        return amount.quantize(Decimal("0.01"))
    adjusted = (amount / Decimal("5")).to_integral_value(rounding=ROUND_CEILING) * Decimal("5")
    return adjusted.quantize(Decimal("0.01"))


def peso(amount) -> str:
    return f"PHP {Decimal(str(amount)):,.2f}"


def build_schedule(principal: Decimal, rate: Decimal, term: int):
    months = Decimal(term)
    periods = term * WORKING_DAYS_PER_MONTH
    total_interest = (principal * (rate / Decimal("100")) * months).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    total_payable = (principal + total_interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_day = (total_payable / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    per_month = (total_payable / months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    principal_per = (principal / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    interest_per = (total_interest / Decimal(periods)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    principal_remaining = principal
    interest_remaining = total_interest
    rows = []
    for number in range(1, periods + 1):
        if number == periods:
            p, i = principal_remaining, interest_remaining
        else:
            p, i = principal_per, interest_per
        due = (p + i).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append((p, i, due))
        principal_remaining = max(Decimal("0.00"), principal_remaining - p)
        interest_remaining = max(Decimal("0.00"), interest_remaining - i)

    monthly = []
    outstanding = total_payable
    for m in range(term):
        chunk = rows[m * WORKING_DAYS_PER_MONTH : (m + 1) * WORKING_DAYS_PER_MONTH]
        p = sum((x[0] for x in chunk), Decimal("0.00"))
        i = sum((x[1] for x in chunk), Decimal("0.00"))
        due = sum((x[2] for x in chunk), Decimal("0.00"))
        cash = adjust_payment(due)
        outstanding = (outstanding - due).quantize(Decimal("0.01"))
        monthly.append(
            {
                "month": m + 1,
                "principal": p,
                "interest": i,
                "exact": due,
                "cash": cash,
                "balance": outstanding,
            }
        )

    return {
        "principal": principal,
        "rate": rate,
        "term": term,
        "periods": periods,
        "total_interest": total_interest,
        "total_payable": total_payable,
        "per_day": per_day,
        "per_month": per_month,
        "principal_per": principal_per,
        "interest_per": interest_per,
        "monthly": monthly,
        "cash_total": sum((row["cash"] for row in monthly), Decimal("0.00")),
    }


def _styles():
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "Title",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=18,
            textColor=TEAL_DEEP,
            spaceAfter=4,
            alignment=TA_LEFT,
            leading=22,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            textColor=INK_SOFT,
            spaceAfter=14,
            leading=14,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=TEAL,
            spaceBefore=12,
            spaceAfter=6,
            leading=14,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            textColor=INK,
            leading=13,
            spaceAfter=4,
        ),
        "formula": ParagraphStyle(
            "Formula",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            textColor=INK_SOFT,
            leading=12,
            spaceAfter=3,
            leftIndent=8,
        ),
        "note": ParagraphStyle(
            "Note",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=INK_SOFT,
            leading=11,
            spaceBefore=8,
        ),
        "footer": ParagraphStyle(
            "Footer",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            textColor=INK_SOFT,
            alignment=TA_CENTER,
        ),
        "th": ParagraphStyle(
            "TH",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            textColor=WHITE,
            alignment=TA_CENTER,
            leading=10,
        ),
        "td": ParagraphStyle(
            "TD",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            textColor=INK,
            alignment=TA_RIGHT,
            leading=11,
        ),
        "td_left": ParagraphStyle(
            "TDLeft",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            textColor=INK,
            alignment=TA_LEFT,
            leading=11,
        ),
        "td_center": ParagraphStyle(
            "TDCenter",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            textColor=INK,
            alignment=TA_CENTER,
            leading=11,
        ),
        "kpi_label": ParagraphStyle(
            "KPILabel",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            textColor=INK_SOFT,
            alignment=TA_CENTER,
            leading=9,
        ),
        "kpi_value": ParagraphStyle(
            "KPIValue",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=TEAL_DEEP,
            alignment=TA_CENTER,
            leading=14,
        ),
    }
    return styles


def _header_footer(canvas, doc):
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(TEAL)
    canvas.setLineWidth(2.5)
    canvas.line(18 * mm, height - 14 * mm, width - 18 * mm, height - 14 * mm)
    canvas.setFillColor(INK_SOFT)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(18 * mm, 12 * mm, "KAP Microfinance · Flat-interest loan computation example")
    canvas.drawRightString(width - 18 * mm, 12 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(18 * mm, 16 * mm, width - 18 * mm, 16 * mm)
    canvas.restoreState()


def build_pdf(path: Path = OUTPUT) -> Path:
    data = build_schedule(Decimal("10000.00"), Decimal("6"), 3)
    styles = _styles()
    story = []

    # Brand header
    header_bits = []
    if LOGO.exists():
        logo = Image(str(LOGO), width=38 * mm, height=14 * mm, kind="proportional")
        header_bits.append(logo)
    else:
        header_bits.append(Paragraph("<b>KAP Microfinance</b>", styles["title"]))
    header_bits.append(Spacer(1, 4 * mm))
    header_bits.append(Paragraph("Sample Loan Computation", styles["title"]))
    header_bits.append(
        Paragraph(
            "Worked example using the system flat-interest formula · "
            "working-day collection (Mon–Fri, 22 days / month) · pay until paid",
            styles["subtitle"],
        )
    )
    story.extend(header_bits)

    # Loan inputs KPI strip
    kpi = Table(
        [
            [
                Paragraph("PRINCIPAL", styles["kpi_label"]),
                Paragraph("RATE (FLAT / MO)", styles["kpi_label"]),
                Paragraph("TERM", styles["kpi_label"]),
                Paragraph("WORKING DAYS", styles["kpi_label"]),
            ],
            [
                Paragraph(peso(data["principal"]), styles["kpi_value"]),
                Paragraph(f"{data['rate']}%", styles["kpi_value"]),
                Paragraph(f"{data['term']} months", styles["kpi_value"]),
                Paragraph(str(data["periods"]), styles["kpi_value"]),
            ],
        ],
        colWidths=[42 * mm, 42 * mm, 42 * mm, 42 * mm],
    )
    kpi.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), MINT),
                ("BOX", (0, 0), (-1, -1), 0.8, TEAL),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(kpi)

    # One-time computation
    story.append(Paragraph("1. One-time loan computation", styles["h2"]))
    story.append(
        Paragraph(
            "Interest is <b>flat on the original principal</b> for every month of the term "
            "(not reducing-balance).",
            styles["body"],
        )
    )
    story.append(
        Paragraph(
            f"Total interest = principal × rate × months = "
            f"{peso(data['principal'])} × {data['rate'] / 100} × {data['term']} = "
            f"<b>{peso(data['total_interest'])}</b>",
            styles["formula"],
        )
    )
    story.append(
        Paragraph(
            f"Total payable = principal + interest = "
            f"{peso(data['principal'])} + {peso(data['total_interest'])} = "
            f"<b>{peso(data['total_payable'])}</b>",
            styles["formula"],
        )
    )
    story.append(
        Paragraph(
            f"Per month (exact) = total ÷ months = "
            f"{peso(data['total_payable'])} ÷ {data['term']} = "
            f"<b>{peso(data['per_month'])}</b>",
            styles["formula"],
        )
    )
    story.append(
        Paragraph(
            f"Per working day (exact) = total ÷ ({data['term']} × 22) = "
            f"{peso(data['total_payable'])} ÷ {data['periods']} = "
            f"<b>{peso(data['per_day'])}</b>",
            styles["formula"],
        )
    )
    story.append(
        Paragraph(
            f"Daily split ≈ principal <b>{peso(data['principal_per'])}</b> + "
            f"interest <b>{peso(data['interest_per'])}</b> "
            f"(final day absorbs rounding so the schedule sums exactly).",
            styles["formula"],
        )
    )

    summary = Table(
        [
            [
                Paragraph("<b>Item</b>", styles["th"]),
                Paragraph("<b>Amount</b>", styles["th"]),
            ],
            [Paragraph("Principal (capital)", styles["td_left"]), Paragraph(peso(data["principal"]), styles["td"])],
            [Paragraph("Total interest", styles["td_left"]), Paragraph(peso(data["total_interest"]), styles["td"])],
            [Paragraph("Total payable", styles["td_left"]), Paragraph(peso(data["total_payable"]), styles["td"])],
            [Paragraph("Exact monthly installment", styles["td_left"]), Paragraph(peso(data["per_month"]), styles["td"])],
            [Paragraph("Exact daily installment", styles["td_left"]), Paragraph(peso(data["per_day"]), styles["td"])],
        ],
        colWidths=[110 * mm, 58 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), TEAL),
                ("BACKGROUND", (0, 3), (-1, 3), MINT),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("BOX", (0, 0), (-1, -1), 0.7, TEAL),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(summary)

    # Month-by-month
    story.append(Paragraph("2. Pay until paid — computation each month", styles["h2"]))
    story.append(
        Paragraph(
            "Each month covers <b>22 working days</b>. Paying the scheduled month in full "
            "reduces the outstanding balance. After Month 3 the loan is <b>₱0.00 — paid</b>. "
            "Cash remittance is rounded <b>up to the nearest ₱5</b>; the uplift is credited to savings.",
            styles["body"],
        )
    )

    month_header = [
        Paragraph("<b>Month</b>", styles["th"]),
        Paragraph("<b>Principal</b>", styles["th"]),
        Paragraph("<b>Interest</b>", styles["th"]),
        Paragraph("<b>Exact due</b>", styles["th"]),
        Paragraph("<b>Cash (₱5)</b>", styles["th"]),
        Paragraph("<b>Balance after</b>", styles["th"]),
    ]
    month_rows = [month_header]
    for row in data["monthly"]:
        bal_label = peso(row["balance"]) if row["balance"] > 0 else "PHP 0.00 — PAID"
        month_rows.append(
            [
                Paragraph(f"Month {row['month']}", styles["td_center"]),
                Paragraph(peso(row["principal"]), styles["td"]),
                Paragraph(peso(row["interest"]), styles["td"]),
                Paragraph(peso(row["exact"]), styles["td"]),
                Paragraph(peso(row["cash"]), styles["td"]),
                Paragraph(bal_label, styles["td"]),
            ]
        )
    month_rows.append(
        [
            Paragraph("<b>Total</b>", styles["td_left"]),
            Paragraph(f"<b>{peso(data['principal'])}</b>", styles["td"]),
            Paragraph(f"<b>{peso(data['total_interest'])}</b>", styles["td"]),
            Paragraph(f"<b>{peso(data['total_payable'])}</b>", styles["td"]),
            Paragraph(f"<b>{peso(data['cash_total'])}</b>", styles["td"]),
            Paragraph("<b>—</b>", styles["td_center"]),
        ]
    )

    month_table = Table(
        month_rows,
        colWidths=[24 * mm, 30 * mm, 28 * mm, 30 * mm, 28 * mm, 28 * mm],
    )
    month_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), TEAL),
                ("BACKGROUND", (0, -1), (-1, -1), MINT),
                ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#f3fbf6")),
                ("BOX", (0, 0), (-1, -1), 0.7, TEAL),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(Spacer(1, 2 * mm))
    story.append(KeepTogether([month_table]))

    # Month detail cards
    story.append(Paragraph("3. Month-by-month detail", styles["h2"]))
    opening = data["total_payable"]
    for row in data["monthly"]:
        opening_before = opening
        opening = row["balance"]
        detail = [
            Paragraph(f"<b>Month {row['month']}</b> — 22 working days", styles["body"]),
            Paragraph(
                f"Principal recovered: {peso(row['principal'])} · "
                f"Interest portion: {peso(row['interest'])}",
                styles["formula"],
            ),
            Paragraph(
                f"Exact due: {peso(row['exact'])} · "
                f"Cash remittance: {peso(row['cash'])} "
                f"(savings uplift {peso(row['cash'] - row['exact'])})",
                styles["formula"],
            ),
            Paragraph(
                f"Balance: {peso(opening_before)} − {peso(row['exact'])} = "
                f"<b>{peso(row['balance']) if row['balance'] > 0 else 'PHP 0.00 — paid'}</b>",
                styles["formula"],
            ),
        ]
        story.extend(detail)
        story.append(Spacer(1, 2 * mm))

    surplus = data["cash_total"] - data["total_payable"]
    story.append(Paragraph("4. Settlement summary", styles["h2"]))
    settle = Table(
        [
            [
                Paragraph("<b>Description</b>", styles["th"]),
                Paragraph("<b>Amount</b>", styles["th"]),
            ],
            [
                Paragraph("Loan cleared (exact total payable)", styles["td_left"]),
                Paragraph(peso(data["total_payable"]), styles["td"]),
            ],
            [
                Paragraph("Total cash collected (3 monthly remittances)", styles["td_left"]),
                Paragraph(peso(data["cash_total"]), styles["td"]),
            ],
            [
                Paragraph("Rounding surplus credited to member savings", styles["td_left"]),
                Paragraph(peso(surplus), styles["td"]),
            ],
        ],
        colWidths=[120 * mm, 48 * mm],
    )
    settle.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), TEAL),
                ("BACKGROUND", (0, -1), (-1, -1), MINT),
                ("BOX", (0, 0), (-1, -1), 0.7, TEAL),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(settle)

    story.append(
        Paragraph(
            "Notes: Interest starts on the Monday on/after disbursement. "
            "Remaining principal of PHP 1,000.00 or less waives further interest. "
            "This document is a sample only; live schedules use the same formula in the loan system.",
            styles["note"],
        )
    )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title="KAP Sample Loan Computation",
        author="KAP Microfinance Loan System",
    )
    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return path


if __name__ == "__main__":
    out = build_pdf()
    print(f"Wrote {out}")
