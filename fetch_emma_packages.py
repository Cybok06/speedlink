"""Fetch Emma Data packages and create a PDF. Run: python fetch_emma_packages.py

Set EMMA_DATA_API_KEY in the project .env or in your shell environment.
Dependencies: requests, python-dotenv, reportlab (already in requirements.txt).
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
import os
from pathlib import Path
import sys
import tempfile

import requests
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


ROOT = Path(__file__).resolve().parent
PACKAGES_URL = "https://emmadataking.com/api/v1/packages"
NAVY = colors.HexColor("#132B46")
TEAL = colors.HexColor("#087E8B")


def fetch_packages(api_key):
    """Make one request; no automatic retries against the 60/minute quota."""
    try:
        response = requests.get(
            PACKAGES_URL,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            timeout=(10, 45),
            allow_redirects=False,
        )
    except requests.Timeout:
        raise ValueError("Emma Data timed out. Please try again later.") from None
    except requests.RequestException:
        raise ValueError("Could not connect to Emma Data. Check your connection and try again.") from None

    if response.status_code in (401, 403):
        raise ValueError("Emma Data rejected access. Check EMMA_DATA_API_KEY and account permissions.")
    if response.status_code == 429:
        raise ValueError("Emma Data's request limit was reached (60/minute). Wait before retrying.")
    if response.status_code != 200:
        raise ValueError(f"Emma Data returned HTTP {response.status_code}; no report was written.")
    try:
        payload = response.json()
    except ValueError:
        raise ValueError("Emma Data returned invalid JSON; no report was written.") from None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("Emma Data did not return a successful package response.")
    packages = payload.get("data")
    if not isinstance(packages, dict):
        raise ValueError("Unexpected package data: expected packages grouped by network.")
    for network, rows in packages.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Unexpected package list for network {network}.")
    return packages


def price_text(value):
    try:
        price = Decimal(str(value))
        return f"{price:,.2f}" if price.is_finite() else "Unknown"
    except (InvalidOperation, ValueError):
        return "Unknown"


def stock_text(value):
    return "In stock" if value is True else "Out of stock" if value is False else "Unknown"


def write_pdf(packages, output_path, fetched_at):
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("ReportTitle", fontSize=26, leading=31, textColor=NAVY, spaceAfter=9))
    styles.add(ParagraphStyle("NetworkTitle", fontSize=14, leading=18, textColor=NAVY, spaceBefore=18, spaceAfter=9))
    styles.add(ParagraphStyle("CellText", fontSize=9, leading=12, textColor=NAVY))
    styles.add(ParagraphStyle("HeaderText", fontSize=8, leading=11, textColor=colors.white))
    styles.add(ParagraphStyle("PriceText", parent=styles["CellText"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle("MutedText", fontSize=9, leading=14, textColor=colors.HexColor("#536477")))

    def para(value, style="CellText"):
        return Paragraph(escape(str(value if value is not None else "Unknown")), styles[style])

    total = sum(len(rows) for rows in packages.values())
    available = sum(row.get("in_stock") is True for rows in packages.values() for row in rows)
    story = [
        para("EMMA DATA / PACKAGE CATALOGUE", "MutedText"), Spacer(1, 5 * mm),
        para("Data packages & pricing", "ReportTitle"),
        para(f"Retrieved {fetched_at.strftime('%d %B %Y, %H:%M UTC')}", "MutedText"),
        Spacer(1, 6 * mm),
    ]
    summary = Table([[f"{len(packages)} NETWORKS", f"{total} PACKAGES", f"{available} IN STOCK"]], colWidths=[58 * mm] * 3)
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF4F5")),
        ("TEXTCOLOR", (0, 0), (-1, -1), TEAL), ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 13),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
    ]))
    story.append(summary)
    widths = [23 * mm, 30 * mm, 23 * mm, 27 * mm, 35 * mm, 36 * mm]
    for network, rows in packages.items():
        heading = para(f"{network}  /  {len(rows)} packages", "NetworkTitle")
        if not rows:
            story.append(KeepTogether([heading, para("No packages listed.", "MutedText")]))
            continue
        heading.keepWithNext = True
        story.append(heading)
        table_rows = [[para(label, "HeaderText") for label in
                       ("Package ID", "Capacity", "Value", "Volume (MB)", "Price", "Availability")]]
        for row in rows:
            table_rows.append([
                para(row.get("package_id")), para(row.get("capacity")),
                para(row.get("capacity_value")), para(row.get("volume_mb")),
                para(f"{row.get('currency') or 'Unknown'} {price_text(row.get('price'))}", "PriceText"),
                para(stock_text(row.get("in_stock"))),
            ])
        table = LongTable(table_rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LINEBELOW", (0, 0), (-1, 0), 1, TEAL),
        ]))
        story.append(table)
    if not total:
        story.extend([Spacer(1, 6 * mm), para("The API returned no packages.", "MutedText")])
    story.extend([
        Spacer(1, 8 * mm), para(f"Source: {PACKAGES_URL}", "MutedText"),
        para("Prices and availability reflect the API response at retrieval time and may change. Network names are shown as supplied by Emma Data.", "MutedText"),
    ])

    def page_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#DAE3EB"))
        canvas.line(18 * mm, 17 * mm, 192 * mm, 17 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#536477"))
        canvas.drawString(18 * mm, 12 * mm, "EMMA DATA | Package catalogue")
        canvas.drawRightString(192 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Replace only after a complete PDF is built, preserving any previous report on failure.
    with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".pdf", delete=False) as handle:
        temporary_path = Path(handle.name)
    try:
        doc = SimpleDocTemplate(str(temporary_path), pagesize=A4, rightMargin=18 * mm,
                                leftMargin=18 * mm, topMargin=18 * mm, bottomMargin=24 * mm,
                                title="Emma Data - Packages and Pricing", author="Speedlink")
        doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/emma_data_packages.pdf",
                        help="PDF output path (default: output/pdf/emma_data_packages.pdf)")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("EMMA_DATA_API_KEY", "").strip()
    if not api_key:
        print("Error: Set EMMA_DATA_API_KEY in your .env file or shell environment.", file=sys.stderr)
        return 1
    try:
        packages = fetch_packages(api_key)
        write_pdf(packages, args.output, datetime.now(timezone.utc))
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Fetched {sum(map(len, packages.values()))} packages across {len(packages)} networks.")
    print(f"PDF saved to: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
