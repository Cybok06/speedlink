from datetime import datetime
from datetime import timedelta
from io import BytesIO
import importlib.util
import re
import time

import pandas as pd
from flask import Blueprint, redirect, render_template, request, send_file, session, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from db import db
from phone_number_guard import (
    eligibility_json_response,
    is_block_new_numbers_enabled,
    set_block_new_numbers_enabled,
)

admin_phone_numbers_bp = Blueprint("admin_phone_numbers", __name__)

orders_col = db["orders"]
blocked_phone_numbers_col = db["blocked_phone_numbers"]
CACHE_TTL_SECONDS = 300
_catalog_cache = {
    "services": {"value": [], "expires_at": 0.0},
    "networks": {"value": [], "expires_at": 0.0},
    "new_numbers_count": {"value": 0, "expires_at": 0.0},
}


def _require_admin():
    return session.get("role") == "admin"


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def _pagination_params():
    page_raw = request.args.get("page", 1)
    try:
        page = max(int(page_raw), 1)
    except Exception:
        page = 1
    per_page = 20
    return page, per_page


def _preferred_excel_engine():
    if importlib.util.find_spec("xlsxwriter"):
        return "xlsxwriter"
    if importlib.util.find_spec("openpyxl"):
        return "openpyxl"
    raise RuntimeError("Excel export requires either xlsxwriter or openpyxl to be installed.")


def _active_tab():
    tab = (request.args.get("tab") or "unique").strip().lower()
    return tab if tab in {"unique", "new"} else "unique"


def _normalize_network_name(value):
    text = str(value or "").strip().upper()
    if not text:
        return ""
    compact = re.sub(r"[^A-Z0-9]+", "", text)
    aliases = {
        "AIRTELTIGO": "AirtelTigo",
        "AT": "AirtelTigo",
        "MTN": "MTN",
        "TELECEL": "Telecel",
        "VODAFONE": "Telecel",
    }
    if compact in aliases:
        return aliases[compact]
    return str(value or "").strip()


def _guess_network_from_service_name(service_name):
    normalized = _normalize_network_name(service_name)
    return normalized if normalized in {"MTN", "Telecel", "AirtelTigo"} else ""


def _normalize_source_label(value):
    src = (value or "").strip().lower()
    if src == "customer_dashboard":
        return "Customer Dashboard"
    if src == "campus":
        return "Campus"
    return "Store Page"


def _parse_date(value: str):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _date_range_from_request():
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    start_dt = _parse_date(start_date)
    end_dt = _parse_date(end_date)
    if end_dt:
        end_dt = end_dt + timedelta(days=1)
    return start_date, end_date, start_dt, end_dt


def _service_catalog():
    cache_entry = _catalog_cache["services"]
    now = time.time()
    if cache_entry["expires_at"] > now:
        return cache_entry["value"]

    pipeline = [
        {"$unwind": "$items"},
        {"$match": {"items.serviceName": {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": "$items.serviceName"}},
        {"$sort": {"_id": 1}},
    ]
    rows = list(orders_col.aggregate(pipeline))
    value = [str(row.get("_id") or "").strip() for row in rows if str(row.get("_id") or "").strip()]
    cache_entry["value"] = value
    cache_entry["expires_at"] = now + CACHE_TTL_SECONDS
    return value


def _network_catalog():
    cache_entry = _catalog_cache["networks"]
    now = time.time()
    if cache_entry["expires_at"] > now:
        return cache_entry["value"]

    pipeline = [
        {"$unwind": "$items"},
        {
            "$project": {
                "_id": 0,
                "provider_network": "$items.provider_network",
                "network": "$items.network",
                "network_name": "$items.network_name",
                "service_name": "$items.serviceName",
            }
        },
    ]
    rows = list(orders_col.aggregate(pipeline))
    seen = set()
    networks = []
    for row in rows:
        network = (
            _normalize_network_name(row.get("provider_network"))
            or _normalize_network_name(row.get("network"))
            or _normalize_network_name(row.get("network_name"))
            or _guess_network_from_service_name(row.get("service_name"))
        )
        if network and network not in seen:
            seen.add(network)
            networks.append(network)
    value = sorted(networks)
    cache_entry["value"] = value
    cache_entry["expires_at"] = now + CACHE_TTL_SECONDS
    return value


def _new_numbers_count():
    cache_entry = _catalog_cache["new_numbers_count"]
    now = time.time()
    if cache_entry["expires_at"] > now:
        return int(cache_entry["value"] or 0)

    pipeline = [
        {"$unwind": "$items"},
        {"$match": {"items.phone": {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": "$items.phone"}},
        {"$count": "total"},
    ]
    rows = list(orders_col.aggregate(pipeline))
    value = int(rows[0]["total"]) if rows else 0
    cache_entry["value"] = value
    cache_entry["expires_at"] = now + CACHE_TTL_SECONDS
    return value


def _export_filters_from_request():
    export_scope = (request.args.get("export_scope") or "").strip().lower()
    selected_service = (request.args.get("export_service_name") or request.args.get("service_name") or "").strip()
    selected_network = _normalize_network_name(request.args.get("export_network_name") or request.args.get("network_name"))

    if export_scope == "service":
        return "service", selected_service, ""
    if export_scope == "network":
        return "network", "", selected_network
    return "all", "", ""


def _fetch_phone_rows(
    service_name: str,
    q: str,
    network_name: str = "",
    skip: int | None = None,
    limit: int | None = None,
):
    count_only = limit is not None and int(limit) <= 0

    if q:
        phone_match = {"$regex": re.escape(q), "$options": "i"}
    else:
        phone_match = {"$exists": True, "$nin": [None, ""]}

    base_match = {"items.phone": phone_match}
    if service_name:
        base_match["items.serviceName"] = service_name

    pipeline = [
        {"$unwind": "$items"},
        {"$match": base_match},
        {
            "$group": {
                "_id": "$items.phone",
                "order_ids": {"$addToSet": "$order_id"},
                "last_order_at": {"$max": "$created_at"},
                "services": {"$addToSet": "$items.serviceName"},
                "provider_networks": {"$addToSet": "$items.provider_network"},
                "networks": {"$addToSet": "$items.network"},
                "network_names": {"$addToSet": "$items.network_name"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "phone": "$_id",
                "orders_count": {"$size": "$order_ids"},
                "last_order_at": 1,
                "services": {
                    "$filter": {
                        "input": "$services",
                        "as": "service",
                        "cond": {"$and": [{"$ne": ["$$service", None]}, {"$ne": ["$$service", ""]}]},
                    }
                },
                "provider_networks": 1,
                "networks": 1,
                "network_names": 1,
            }
        },
    ]

    if network_name:
        pipeline.append(
            {
                "$match": {
                    "$expr": {
                        "$or": [
                            {
                                "$in": [
                                    network_name,
                                    {
                                        "$map": {
                                            "input": {"$ifNull": ["$provider_networks", []]},
                                            "as": "network",
                                            "in": {
                                                "$switch": {
                                                    "branches": [
                                                        {
                                                            "case": {
                                                                "$eq": [
                                                                    {
                                                                        "$toUpper": {
                                                                            "$replaceAll": {
                                                                                "input": {
                                                                                    "$replaceAll": {
                                                                                        "input": {"$ifNull": ["$$network", ""]},
                                                                                        "find": " ",
                                                                                        "replacement": "",
                                                                                    }
                                                                                },
                                                                                "find": "-",
                                                                                "replacement": "",
                                                                            }
                                                                        }
                                                                    },
                                                                    "VODAFONE",
                                                                ]
                                                            },
                                                            "then": "Telecel",
                                                        },
                                                        {
                                                            "case": {
                                                                "$eq": [
                                                                    {
                                                                        "$toUpper": {
                                                                            "$replaceAll": {
                                                                                "input": {
                                                                                    "$replaceAll": {
                                                                                        "input": {"$ifNull": ["$$network", ""]},
                                                                                        "find": " ",
                                                                                        "replacement": "",
                                                                                    }
                                                                                },
                                                                                "find": "-",
                                                                                "replacement": "",
                                                                            }
                                                                        }
                                                                    },
                                                                    "TELECEL",
                                                                ]
                                                            },
                                                            "then": "Telecel",
                                                        },
                                                        {
                                                            "case": {
                                                                "$eq": [
                                                                    {
                                                                        "$toUpper": {
                                                                            "$replaceAll": {
                                                                                "input": {
                                                                                    "$replaceAll": {
                                                                                        "input": {"$ifNull": ["$$network", ""]},
                                                                                        "find": " ",
                                                                                        "replacement": "",
                                                                                    }
                                                                                },
                                                                                "find": "-",
                                                                                "replacement": "",
                                                                            }
                                                                        }
                                                                    },
                                                                    "AIRTELTIGO",
                                                                ]
                                                            },
                                                            "then": "AirtelTigo",
                                                        },
                                                        {
                                                            "case": {
                                                                "$eq": [
                                                                    {
                                                                        "$toUpper": {
                                                                            "$replaceAll": {
                                                                                "input": {
                                                                                    "$replaceAll": {
                                                                                        "input": {"$ifNull": ["$$network", ""]},
                                                                                        "find": " ",
                                                                                        "replacement": "",
                                                                                    }
                                                                                },
                                                                                "find": "-",
                                                                                "replacement": "",
                                                                            }
                                                                        }
                                                                    },
                                                                    "MTN",
                                                                ]
                                                            },
                                                            "then": "MTN",
                                                        },
                                                    ],
                                                    "default": "",
                                                }
                                            },
                                        }
                                    },
                                ]
                            },
                            {"$in": [network_name, "$networks"]},
                            {"$in": [network_name, "$network_names"]},
                        ]
                    }
                }
            }
        )

    rows_pipeline = [{"$sort": {"orders_count": -1, "last_order_at": -1, "phone": 1}}]
    if skip is not None and not count_only:
        rows_pipeline.append({"$skip": skip})
    if limit is not None and int(limit) > 0:
        rows_pipeline.append({"$limit": limit})

    pipeline.append(
        {
            "$facet": {
                "metadata": [{"$count": "total"}],
                "rows": rows_pipeline,
            }
        }
    )

    agg_result = list(orders_col.aggregate(pipeline))
    payload = agg_result[0] if agg_result else {"metadata": [], "rows": []}
    total = int(payload["metadata"][0]["total"]) if payload.get("metadata") else 0
    rows = [] if count_only else (payload.get("rows") or [])

    row_keys = [_normalize_phone(r.get("phone")) for r in rows if r.get("phone")]
    active_blocks = list(
        blocked_phone_numbers_col.find(
            {"is_active": True, "normalized_phone": {"$in": row_keys}},
            {"normalized_phone": 1, "reason": 1, "_id": 0},
        )
    )
    blocked_map = {d.get("normalized_phone"): d for d in active_blocks if d.get("normalized_phone")}

    for row in rows:
        key = _normalize_phone(row.get("phone"))
        row["normalized_phone"] = key
        row["is_blocked"] = key in blocked_map
        row["block_reason"] = (blocked_map.get(key) or {}).get("reason", "")
        row["services"] = sorted({str(service).strip() for service in (row.get("services") or []) if str(service).strip()})
        row["services_label"] = ", ".join(row["services"])
        networks = []
        for value in (row.get("provider_networks") or []) + (row.get("networks") or []) + (row.get("network_names") or []):
            network = _normalize_network_name(value)
            if network and network not in networks:
                networks.append(network)
        if not networks:
            for service in row["services"]:
                guessed = _guess_network_from_service_name(service)
                if guessed and guessed not in networks:
                    networks.append(guessed)
        row["networks"] = networks
        row["networks_label"] = ", ".join(networks)

    return total, rows


def _fetch_new_phone_rows(
    q: str,
    start_dt=None,
    end_dt=None,
    skip: int | None = None,
    limit: int | None = None,
):
    count_only = limit is not None and int(limit) <= 0

    if q:
        phone_match = {"$regex": re.escape(q), "$options": "i"}
    else:
        phone_match = {"$exists": True, "$nin": [None, ""]}

    pipeline = [
        {"$unwind": "$items"},
        {"$match": {"items.phone": phone_match}},
        {"$sort": {"created_at": 1, "_id": 1}},
        {
            "$group": {
                "_id": "$items.phone",
                "first_order_at": {"$first": "$created_at"},
                "first_order_id": {"$first": "$order_id"},
                "first_service": {"$first": "$items.serviceName"},
                "first_provider_network": {"$first": "$items.provider_network"},
                "first_network": {"$first": "$items.network"},
                "first_network_name": {"$first": "$items.network_name"},
                "first_source": {"$first": "$source"},
            }
        },
    ]

    date_match = {}
    if start_dt:
        date_match["$gte"] = start_dt
    if end_dt:
        date_match["$lt"] = end_dt
    if date_match:
        pipeline.append({"$match": {"first_order_at": date_match}})

    pipeline.append(
        {
            "$project": {
                "_id": 0,
                "phone": "$_id",
                "first_order_at": 1,
                "first_order_id": 1,
                "first_service": {"$ifNull": ["$first_service", ""]},
                "first_provider_network": {"$ifNull": ["$first_provider_network", ""]},
                "first_network": {"$ifNull": ["$first_network", ""]},
                "first_network_name": {"$ifNull": ["$first_network_name", ""]},
                "first_source": {"$ifNull": ["$first_source", ""]},
            }
        }
    )

    rows_pipeline = [{"$sort": {"first_order_at": -1, "phone": 1}}]
    if skip is not None and not count_only:
        rows_pipeline.append({"$skip": skip})
    if limit is not None and int(limit) > 0:
        rows_pipeline.append({"$limit": limit})

    pipeline.append(
        {
            "$facet": {
                "metadata": [{"$count": "total"}],
                "rows": rows_pipeline,
            }
        }
    )

    agg_result = list(orders_col.aggregate(pipeline))
    payload = agg_result[0] if agg_result else {"metadata": [], "rows": []}
    total = int(payload["metadata"][0]["total"]) if payload.get("metadata") else 0
    rows = [] if count_only else (payload.get("rows") or [])

    row_keys = [_normalize_phone(r.get("phone")) for r in rows if r.get("phone")]
    active_blocks = list(
        blocked_phone_numbers_col.find(
            {"is_active": True, "normalized_phone": {"$in": row_keys}},
            {"normalized_phone": 1, "reason": 1, "_id": 0},
        )
    )
    blocked_map = {d.get("normalized_phone"): d for d in active_blocks if d.get("normalized_phone")}

    for row in rows:
        key = _normalize_phone(row.get("phone"))
        row["normalized_phone"] = key
        row["is_blocked"] = key in blocked_map
        row["block_reason"] = (blocked_map.get(key) or {}).get("reason", "")
        network = (
            _normalize_network_name(row.get("first_provider_network"))
            or _normalize_network_name(row.get("first_network"))
            or _normalize_network_name(row.get("first_network_name"))
            or _guess_network_from_service_name(row.get("first_service"))
        )
        row["first_network_label"] = network or "-"
        row["first_service_label"] = str(row.get("first_service") or "").strip() or "-"
        row["first_source_label"] = _normalize_source_label(row.get("first_source"))

    return total, rows


@admin_phone_numbers_bp.route("/admin/phone-numbers")
def phone_numbers_page():
    if not _require_admin():
        return redirect(url_for("login.login"))

    active_tab = _active_tab()
    q = (request.args.get("q") or "").strip()
    service_name = (request.args.get("service_name") or "").strip()
    network_name = _normalize_network_name(request.args.get("network_name"))
    start_date, end_date, start_dt, end_dt = _date_range_from_request()
    page, per_page = _pagination_params()
    service_options = _service_catalog()
    network_options = _network_catalog()
    new_numbers_count = _new_numbers_count()
    block_new_numbers_enabled = is_block_new_numbers_enabled()

    if active_tab == "new":
        total, _ = _fetch_new_phone_rows(q=q, start_dt=start_dt, end_dt=end_dt, skip=0, limit=0)
    else:
        total, _ = _fetch_phone_rows(service_name=service_name, q=q, network_name=network_name, skip=0, limit=0)
    total_pages = max((total + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page

    if active_tab == "new":
        _, rows = _fetch_new_phone_rows(q=q, start_dt=start_dt, end_dt=end_dt, skip=skip, limit=per_page)
    else:
        _, rows = _fetch_phone_rows(service_name=service_name, q=q, network_name=network_name, skip=skip, limit=per_page)

    total_blocked = blocked_phone_numbers_col.count_documents({"is_active": True})

    return render_template(
        "admin_phone_numbers.html",
        active_tab=active_tab,
        rows=rows,
        q=q,
        service_name=service_name,
        network_name=network_name,
        start_date=start_date,
        end_date=end_date,
        service_options=service_options,
        network_options=network_options,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        total_blocked=total_blocked,
        new_numbers_count=new_numbers_count,
        block_new_numbers_enabled=block_new_numbers_enabled,
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/export/excel")
def export_phone_numbers_excel():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    export_scope, service_name, network_name = _export_filters_from_request()
    _, rows = _fetch_phone_rows(service_name=service_name, q=q, network_name=network_name)
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    data = []
    for row in rows:
        data.append(
            {
                "Phone Number": row.get("phone", ""),
                "Networks": row.get("networks_label", ""),
                "Services": row.get("services_label", ""),
                "Orders Placed": int(row.get("orders_count") or 0),
                "Status": "Blocked" if row.get("is_blocked") else "Active",
                "Block Reason": row.get("block_reason") or "",
                "Last Order At": (
                    row.get("last_order_at").strftime("%Y-%m-%d %H:%M")
                    if row.get("last_order_at")
                    else ""
                ),
                "Export Scope": export_scope.title(),
                "Selected Network": network_name or "All Networks",
                "Selected Service": service_name or "All Services",
                "Generated At": generated_at,
            }
        )

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine=_preferred_excel_engine()) as writer:
        df.to_excel(writer, index=False, sheet_name="Phone Numbers")
    output.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"phone_numbers_{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/export/pdf")
def export_phone_numbers_pdf():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    export_scope, service_name, network_name = _export_filters_from_request()
    total, rows = _fetch_phone_rows(service_name=service_name, q=q, network_name=network_name)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )

    styles = getSampleStyleSheet()
    title = Paragraph("Phone Numbers Report", styles["Title"])
    subtitle = Paragraph(
        (
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} | "
            f"Total: {total} | Scope: {export_scope.title()} | "
            f"Network: {network_name or 'All Networks'} | Service: {service_name or 'All Services'}"
        ),
        styles["Normal"],
    )

    table_data = [["#", "Phone Number", "Networks", "Services", "Orders", "Status", "Reason", "Last Order"]]
    for idx, row in enumerate(rows, start=1):
        table_data.append(
            [
                str(idx),
                str(row.get("phone") or ""),
                str(row.get("networks_label") or "-"),
                str(row.get("services_label") or "-"),
                str(int(row.get("orders_count") or 0)),
                "Blocked" if row.get("is_blocked") else "Active",
                str(row.get("block_reason") or ""),
                row.get("last_order_at").strftime("%Y-%m-%d %H:%M") if row.get("last_order_at") else "-",
            ]
        )

    tbl = Table(table_data, repeatRows=1, colWidths=[28, 95, 90, 145, 45, 60, 165, 80])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (4, 0), (5, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    doc.build([title, Spacer(1, 8), subtitle, Spacer(1, 12), tbl])
    buffer.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"phone_numbers_{stamp}.pdf",
        mimetype="application/pdf",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/new/export/excel")
def export_new_phone_numbers_excel():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    start_date, end_date, start_dt, end_dt = _date_range_from_request()
    _, rows = _fetch_new_phone_rows(q=q, start_dt=start_dt, end_dt=end_dt)
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    data = []
    for row in rows:
        data.append(
            {
                "Phone Number": row.get("phone", ""),
                "First Order Date": (
                    row.get("first_order_at").strftime("%Y-%m-%d %H:%M")
                    if row.get("first_order_at")
                    else ""
                ),
                "First Order ID": row.get("first_order_id", ""),
                "First Network": row.get("first_network_label", ""),
                "First Service": row.get("first_service_label", ""),
                "Source": row.get("first_source_label", ""),
                "Status": "Blocked" if row.get("is_blocked") else "Active",
                "Block Reason": row.get("block_reason") or "",
                "Date Filter": (
                    f"{start_date or 'Beginning'} to {end_date or 'Today'}"
                    if start_date or end_date
                    else "All Dates"
                ),
                "Generated At": generated_at,
            }
        )

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine=_preferred_excel_engine()) as writer:
        df.to_excel(writer, index=False, sheet_name="New Phone Numbers")
    output.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"new_phone_numbers_{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/new/export/pdf")
def export_new_phone_numbers_pdf():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    start_date, end_date, start_dt, end_dt = _date_range_from_request()
    total, rows = _fetch_new_phone_rows(q=q, start_dt=start_dt, end_dt=end_dt)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )

    styles = getSampleStyleSheet()
    title = Paragraph("New Phone Numbers Report", styles["Title"])
    subtitle = Paragraph(
        (
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} | "
            f"Total: {total} | Date Filter: "
            f"{(start_date or 'Beginning') + ' to ' + (end_date or 'Today') if start_date or end_date else 'All Dates'}"
        ),
        styles["Normal"],
    )

    table_data = [["#", "Phone Number", "First Order", "Network", "Service", "Source", "Status"]]
    for idx, row in enumerate(rows, start=1):
        table_data.append(
            [
                str(idx),
                str(row.get("phone") or ""),
                row.get("first_order_at").strftime("%Y-%m-%d %H:%M") if row.get("first_order_at") else "-",
                str(row.get("first_network_label") or "-"),
                str(row.get("first_service_label") or "-"),
                str(row.get("first_source_label") or "-"),
                "Blocked" if row.get("is_blocked") else "Active",
            ]
        )

    tbl = Table(table_data, repeatRows=1, colWidths=[28, 110, 110, 80, 170, 95, 60])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (6, 0), (6, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    doc.build([title, Spacer(1, 8), subtitle, Spacer(1, 12), tbl])
    buffer.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"new_phone_numbers_{stamp}.pdf",
        mimetype="application/pdf",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/block-new-numbers", methods=["POST"])
def toggle_block_new_numbers():
    if not _require_admin():
        return redirect(url_for("login.login"))

    enabled = (request.form.get("enabled") or "").strip() == "1"
    set_block_new_numbers_enabled(enabled)

    redirect_args = {
        "tab": (request.form.get("tab") or "unique").strip().lower(),
        "q": (request.form.get("q") or "").strip(),
        "service_name": (request.form.get("service_name") or "").strip(),
        "network_name": _normalize_network_name(request.form.get("network_name")),
        "start_date": (request.form.get("start_date") or "").strip(),
        "end_date": (request.form.get("end_date") or "").strip(),
        "page": (request.form.get("page") or "1").strip(),
    }
    return redirect(url_for("admin_phone_numbers.phone_numbers_page", **redirect_args))


@admin_phone_numbers_bp.route("/api/phone-numbers/eligibility", methods=["GET"])
def phone_number_eligibility():
    phone = (request.args.get("phone") or "").strip()
    return eligibility_json_response(phone)


@admin_phone_numbers_bp.route("/admin/phone-numbers/block", methods=["POST"])
def block_phone_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    tab = (request.form.get("tab") or "unique").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    service_name = (request.form.get("service_name") or "").strip()
    network_name = _normalize_network_name(request.form.get("network_name"))
    start_date = (request.form.get("start_date") or "").strip()
    end_date = (request.form.get("end_date") or "").strip()
    page = (request.form.get("page") or "1").strip()
    reason = (request.form.get("reason") or "").strip()

    key = _normalize_phone(phone)
    if key:
        now = datetime.utcnow()
        blocked_phone_numbers_col.update_one(
            {"normalized_phone": key},
            {
                "$set": {
                    "phone": phone,
                    "normalized_phone": key,
                    "reason": reason,
                    "is_active": True,
                    "updated_at": now,
                    "blocked_by": session.get("admin_id") or session.get("user_id"),
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    return redirect(
        url_for(
            "admin_phone_numbers.phone_numbers_page",
            tab=tab,
            q=q,
            service_name=service_name,
            network_name=network_name,
            start_date=start_date,
            end_date=end_date,
            page=page,
        )
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/unblock", methods=["POST"])
def unblock_phone_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    tab = (request.form.get("tab") or "unique").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    service_name = (request.form.get("service_name") or "").strip()
    network_name = _normalize_network_name(request.form.get("network_name"))
    start_date = (request.form.get("start_date") or "").strip()
    end_date = (request.form.get("end_date") or "").strip()
    page = (request.form.get("page") or "1").strip()

    key = _normalize_phone(phone)
    if key:
        blocked_phone_numbers_col.update_one(
            {"normalized_phone": key, "is_active": True},
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow(),
                    "unblocked_by": session.get("admin_id") or session.get("user_id"),
                }
            },
        )

    return redirect(
        url_for(
            "admin_phone_numbers.phone_numbers_page",
            tab=tab,
            q=q,
            service_name=service_name,
            network_name=network_name,
            start_date=start_date,
            end_date=end_date,
            page=page,
        )
    )
