#!/usr/bin/env python3
"""
CodeCraft Network – Packages → PDF

Run:
  python codecraft_packages_pdf.py

Output:
  Codecraft_Packages.pdf
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors


# ==========================================================
# 🔐 API KEY (LOCAL ONLY – DO NOT COMMIT)
# ==========================================================
CODECRAFT_API_KEY = "260109122317-?cZT8C-1AE8bv-LiNnt5-6A8s6Q-4j8kO6"

BASE_URL = "https://api.codecraftnetwork.com/api/packages.php"
OUTPUT_FILE = "Codecraft_Packages.pdf"
TIMEOUT = 45
# ==========================================================


def fetch_packages():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    req = urlrequest.Request(BASE_URL, headers=headers, method="GET")

    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(e.read().decode())
    except URLError as e:
        raise RuntimeError(f"Network error: {e}")


def build_pdf(regular, bigtime):
    doc = SimpleDocTemplate(
        OUTPUT_FILE,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        alignment=1,
        fontSize=18,
        spaceAfter=12,
    )

    story.append(Paragraph("Codecraft Packages", title_style))
    story.append(
        Paragraph(
            f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            styles["Normal"],
        )
    )

    story.append(Paragraph("<br/>", styles["Normal"]))

    table_data = [["Category", "Network", "Package (GB)", "Amount (GHS)"]]

    for p in regular:
        table_data.append(
            ["REGULAR", p["network"], p["package"], p["amount"]]
        )

    for p in bigtime:
        table_data.append(
            ["BIGTIME", p["network"], p["package"], p["amount"]]
        )

    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (2, 1), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
                ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
            ]
        )
    )

    story.append(table)
    doc.build(story)


def main():
    print("📦 Fetching CodeCraft packages...")

    payload = fetch_packages()
    data = payload.get("data", {})

    regular = data.get("regular_packages", [])
    bigtime = data.get("bigtime_packages", [])

    if not regular and not bigtime:
        print("⚠️ No packages found. PDF not created.")
        return

    build_pdf(regular, bigtime)

    print("✅ PDF saved successfully:")
    print(f"   {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
