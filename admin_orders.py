# admin_orders.py  — Admin Orders + DB-Backed Scheduler (Render-safe) + Bulk Deliver (Selected)
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify, send_file
from bson import ObjectId, Regex
from db import db
from datetime import datetime, timedelta
from io import BytesIO
import importlib.util
import re
from urllib.parse import urlencode
import uuid
from typing import List, Tuple
import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

admin_orders_bp = Blueprint("admin_orders", __name__)

orders_col        = db["orders"]
users_col         = db["users"]
balances_col      = db["balances"]         # for refunds
transactions_col  = db["transactions"]     # for refund ledger
schedules_col     = db["order_schedules"]  # NEW: persistent job queue
services_col      = db["services"]
export_batches_col = db["order_export_batches"]

# Keep legacy; primary set includes refunded
ALLOWED_STATUSES   = {"pending", "processing", "delivered", "failed", "completed", "refunded"}
ALLOWED_SORTS      = {"newest", "oldest", "amount_desc", "amount_asc"}
DEFAULT_PER_PAGE   = 10
EXPORT_SOURCE_OPTIONS = ("all", "main", "campus")
FINAL_LINE_STATUSES = {
    "delivered",
    "completed",
    "failed",
    "refunded",
    "cancelled",
    "canceled",
    "skipped_duplicate_in_cart",
    "skipped_duplicate_processing",
}
GB_VALUE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*GB\s*$", re.I)

try:
    export_batches_col.create_index([("created_at", -1)])
    export_batches_col.create_index([("badge_no", 1)], unique=True)
    export_batches_col.create_index([("lines.line_id", 1)])
except Exception:
    pass

# --------- HELPERS ----------
def _parse_date(dstr):
    if not dstr:
        return None
    try:
        s = dstr.strip()
        if len(s) <= 10:
            return datetime.strptime(s, "%Y-%m-%d")
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return None

def _build_preserved_query(args, exclude=("page",)):
    kept = {k: v for k, v in args.items() if k not in exclude and v not in (None, "", "None")}
    return urlencode(kept)

def _build_query_from_params(args):
    """Central builder so list + bulk share identical filters."""
    status_filter = (args.get("status") or "").strip().lower()
    order_id_q    = (args.get("order_id") or "").strip()
    customer_q    = (args.get("customer") or "").strip()
    paid_from     = (args.get("paid_from") or "").strip().lower()
    min_total     = (args.get("min_total") or "").strip()
    max_total     = (args.get("max_total") or "").strip()
    date_from     = _parse_date((args.get("date_from") or "").strip())
    date_to_raw   = _parse_date((args.get("date_to") or "").strip())
    date_to       = datetime(date_to_raw.year, date_to_raw.month, date_to_raw.day) + timedelta(days=1) if date_to_raw else None

    item_service  = (args.get("item_service") or "").strip()
    item_offer    = (args.get("item_offer") or "").strip()
    item_phone    = (args.get("item_phone") or "").strip()

    query = {}

    if status_filter and status_filter in ALLOWED_STATUSES:
        query["status"] = status_filter
    if paid_from:
        query["paid_from"] = paid_from
    if order_id_q:
        query["order_id"] = Regex(order_id_q, "i")

    if date_from or date_to:
        dt = {}
        if date_from: dt["$gte"] = date_from
        if date_to:   dt["$lt"]  = date_to
        query["created_at"] = dt

    amt = {}
    try:
        if min_total != "": amt["$gte"] = float(min_total)
    except Exception:
        pass
    try:
        if max_total != "": amt["$lte"] = float(max_total)
    except Exception:
        pass
    if amt:
        query["total_amount"] = amt

    if customer_q:
        rx = Regex(customer_q, "i")
        user_ids = [u["_id"] for u in users_col.find(
            {"$or": [
                {"first_name": rx}, {"last_name": rx}, {"email": rx},
                {"phone": rx}, {"username": rx},
            ]},
            {"_id": 1},
        )]
        query["user_id"] = {"$in": user_ids or []}

    item_and = []
    if item_service: item_and.append({"items.serviceName": Regex(item_service, "i")})
    if item_offer:   item_and.append({"items.value": Regex(item_offer, "i")})
    if item_phone:   item_and.append({"items.phone": Regex(item_phone, "i")})
    if item_and:
        query["$and"] = (query.get("$and") or []) + item_and

    return query

def _require_admin():
    return session.get("role") == "admin"

def _money(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def _normalize_text(value):
    return " ".join(str(value or "").strip().split())

def _normalize_status_text(value):
    text = (value or "").strip().lower()
    if text == "completed":
        return "delivered"
    return text

def _normalize_source(value):
    src = (value or "").strip().lower()
    if src in {"campus"}:
        return "campus"
    return "main"

def _normalize_network_name(value):
    text = _normalize_text(value).lower()
    if not text:
        return ""
    if "mtn" in text:
        return "MTN"
    if "telecel" in text or "vodafone" in text:
        return "Telecel"
    if (
        "airteltigo" in text
        or "airtel tigo" in text
        or "airtel-tigo" in text
        or "i share" in text
        or "ishare" in text
        or text == "at"
    ):
        return "AirtelTigo"
    return text.upper()

def _normalize_provider(value):
    raw = _normalize_text(value).lower()
    if not raw:
        return ""
    key = re.sub(r"[^a-z0-9]+", "", raw)
    if "justice" in key:
        return "justice"
    return raw

def _guess_network_from_service_name(service_name):
    return _normalize_network_name(service_name)

def _build_service_network_map():
    mapping = {}
    try:
        docs = services_col.find({}, {"name": 1, "network": 1, "service_network": 1})
        for doc in docs:
            name = _normalize_text(doc.get("name"))
            if not name:
                continue
            network = (
                _normalize_network_name(doc.get("service_network"))
                or _normalize_network_name(doc.get("network"))
                or _guess_network_from_service_name(name)
            )
            mapping[name.casefold()] = network
    except Exception:
        pass
    return mapping

def _build_export_catalog():
    service_network_map = _build_service_network_map()
    pretty_name_map = {}
    try:
        docs = services_col.find({}, {"name": 1})
        for doc in docs:
            name = _normalize_text(doc.get("name"))
            if name:
                pretty_name_map[name.casefold()] = name
    except Exception:
        pass
    service_names = []
    seen = set()
    for key in sorted(service_network_map.keys()):
        if key in seen:
            continue
        seen.add(key)
        pretty_name = pretty_name_map.get(key, key)
        service_names.append({
            "name": pretty_name,
            "network": service_network_map.get(key) or "",
        })

    networks = []
    network_seen = set()
    for item in service_names:
        network = item.get("network") or ""
        if network and network not in network_seen:
            network_seen.add(network)
            networks.append(network)

    for fallback in ("MTN", "Telecel", "AirtelTigo"):
        if fallback not in network_seen:
            networks.append(fallback)

    return service_names, networks, service_network_map

def _extract_export_service_names(args):
    values = []
    getter = getattr(args, "getlist", None)
    if callable(getter):
        values.extend(getter("service_name"))
        values.extend(getter("service_names"))
    else:
        one = args.get("service_name")
        many = args.get("service_names")
        if one is not None:
            values.append(one)
        if many is not None:
            values.append(many)

    cleaned = []
    seen = set()
    for raw in values:
        if raw is None:
            continue
        if isinstance(raw, (list, tuple)):
            parts = raw
        else:
            parts = str(raw).split(",")
        for part in parts:
            text = _normalize_text(part)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
    return cleaned

def _compact_export_offer(value):
    text = _normalize_text(value)
    if not text:
        return ""
    match = GB_VALUE_RE.match(text)
    if not match:
        return text
    number = float(match.group(1))
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"

def _display_offer_from_item(item):
    value = item.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()

    value_obj = item.get("value_obj") or {}
    if isinstance(value_obj, dict):
        vol = value_obj.get("volume")
        try:
            if vol is not None:
                vol_float = float(vol)
                if vol_float >= 1000:
                    gb = vol_float / 1000.0
                    return f"{gb:g}GB"
                return f"{vol_float:g}MB"
        except Exception:
            pass
    return ""

def _extract_timeframe_bounds(args):
    mode = (args.get("timeframe") or "today").strip().lower()
    now = datetime.utcnow()

    if mode == "custom":
        start_raw = (args.get("date_from") or "").strip()
        end_raw = (args.get("date_to") or "").strip()
        date_from = _parse_date(start_raw)
        date_to = _parse_date(end_raw)
        if date_to:
            date_to = date_to.replace(second=59, microsecond=999999)
        if not date_from or not date_to:
            raise ValueError("Custom date range requires both start and end datetimes.")
        if date_from > date_to:
            raise ValueError("Start datetime must be before end datetime.")
        label = f"{date_from.strftime('%Y-%m-%d %H:%M')} to {date_to.strftime('%Y-%m-%d %H:%M')}"
        return {
            "mode": "custom",
            "label": label,
            "date_from": date_from,
            "date_to": date_to,
        }

    start_time = (args.get("today_start_time") or "00:00").strip() or "00:00"
    end_time = (args.get("today_end_time") or "23:59").strip() or "23:59"
    try:
        start_hour, start_minute = [int(part) for part in start_time.split(":", 1)]
        end_hour, end_minute = [int(part) for part in end_time.split(":", 1)]
    except Exception as exc:
        raise ValueError("Today timeframe requires valid start and end times.") from exc

    day_start = datetime(now.year, now.month, now.day, start_hour, start_minute)
    day_end = datetime(now.year, now.month, now.day, end_hour, end_minute, 59, 999999)
    if day_start > day_end:
        raise ValueError("Today's start time must be before the end time.")
    label = f"Today {day_start.strftime('%H:%M')} to {day_end.strftime('%H:%M')}"
    return {
        "mode": "today",
        "label": label,
        "date_from": day_start,
        "date_to": day_end,
    }

def _build_export_batch_badge():
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"BATCH-{stamp}-{uuid.uuid4().hex[:4].upper()}"

def _line_identifier(order_doc, item, item_index):
    existing = (
        item.get("line_id")
        or item.get("provider_request_order_id")
        or item.get("provider_order_id")
        or item.get("provider_reference")
    )
    if existing:
        return str(existing)
    return f"{order_doc.get('order_id') or str(order_doc.get('_id'))}:{item_index}"

def _resolve_item_network(item, service_network_map):
    return (
        _normalize_network_name(item.get("provider_network"))
        or _normalize_network_name(item.get("network"))
        or _normalize_network_name(item.get("network_name"))
        or service_network_map.get(_normalize_text(item.get("serviceName")).casefold(), "")
        or _guess_network_from_service_name(item.get("serviceName"))
    )

def _is_undelivered_line(item):
    status = _normalize_status_text(item.get("line_status") or item.get("status"))
    if not status:
        return True
    return status not in FINAL_LINE_STATUSES

def _compute_order_status_from_items(items, current_status=None):
    if _normalize_status_text(current_status) == "delivered":
        return "delivered"

    statuses = [_normalize_status_text(it.get("line_status")) for it in (items or [])]
    if not statuses:
        return "processing"
    if all(s == "delivered" for s in statuses):
        return "delivered"
    if all(s == "pending" for s in statuses):
        return "pending"
    if any(s in {"processing", "queued"} for s in statuses):
        return "processing"
    if all(s == "failed" for s in statuses):
        return "failed"
    return "processing"

def _collect_undelivered_rows(args):
    service_names = _extract_export_service_names(args)
    service_keys = {name.casefold() for name in service_names}
    selected_network = _normalize_network_name(args.get("network"))
    source_filter = (args.get("source") or "all").strip().lower()
    timeframe = _extract_timeframe_bounds(args)
    _, _, service_network_map = _build_export_catalog()

    query = {
        "created_at": {
            "$gte": timeframe["date_from"],
            "$lte": timeframe["date_to"],
        }
    }
    if source_filter in {"main", "campus"}:
        query["source"] = source_filter

    rows = []
    for order in orders_col.find(query).sort([("created_at", 1)]):
        order_source = _normalize_source(order.get("source"))
        if source_filter in {"main", "campus"} and order_source != source_filter:
            continue

        items = order.get("items") or []
        for idx, item in enumerate(items):
            if not _is_undelivered_line(item):
                continue

            service_name = _normalize_text(item.get("serviceName"))
            if service_keys and service_name.casefold() not in service_keys:
                continue

            network_name = _resolve_item_network(item, service_network_map)
            if selected_network and selected_network != "ANY" and network_name != selected_network:
                continue

            offer_text = _display_offer_from_item(item)
            row = {
                "source": order_source,
                "order_id": order.get("order_id") or str(order.get("_id")),
                "line_id": _line_identifier(order, item, idx),
                "line_index": idx,
                "service": service_name,
                "network": network_name,
                "phone": _normalize_text(item.get("phone")),
                "offer": offer_text,
                "compact_offer": _compact_export_offer(offer_text),
                "created_at": order.get("created_at"),
                "status": _normalize_status_text(item.get("line_status") or item.get("status") or order.get("status")),
                "line_status": _normalize_status_text(item.get("line_status")),
                "order_db_id": str(order.get("_id")),
                "provider_request_order_id": item.get("provider_request_order_id"),
            }
            rows.append(row)

    return rows, timeframe, service_names

def _export_header_text(selected_network):
    network = _normalize_network_name(selected_network)
    return network if network and network != "ANY" else "NETWORK"

def _build_export_text_lines(rows, selected_network):
    lines = [_export_header_text(selected_network), ""]
    for row in rows:
        phone = row.get("phone") or ""
        compact_offer = row.get("compact_offer") or ""
        line = f"{phone} {compact_offer}".strip()
        if line:
            lines.append(line)
    return lines

def _render_txt_export(rows, selected_network):
    payload = "\n".join(_build_export_text_lines(rows, selected_network))
    output = BytesIO(payload.encode("utf-8"))
    output.seek(0)
    return output

def _preferred_excel_engine():
    if importlib.util.find_spec("xlsxwriter"):
        return "xlsxwriter"
    if importlib.util.find_spec("openpyxl"):
        return "openpyxl"
    raise RuntimeError("Excel export requires either xlsxwriter or openpyxl to be installed.")

def _render_excel_export(rows, selected_network):
    lines = _build_export_text_lines(rows, selected_network)
    df = pd.DataFrame({"Export": lines})
    output = BytesIO()
    engine = _preferred_excel_engine()
    with pd.ExcelWriter(output, engine=engine) as writer:
        df.to_excel(writer, index=False, header=False, sheet_name="Undelivered Orders")
    output.seek(0)
    return output

def _render_pdf_export(rows, selected_network):
    lines = _build_export_text_lines(rows, selected_network)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(lines[0], styles["Title"]), Spacer(1, 18)]
    for text in lines[2:]:
        story.append(Paragraph(text, styles["Normal"]))
        story.append(Spacer(1, 6))
    doc.build(story)
    buffer.seek(0)
    return buffer

def _save_export_batch(rows, fmt, selected_network, source_filter, timeframe, service_names):
    now = datetime.utcnow()
    badge_no = _build_export_batch_badge()
    label = f"Undelivered Export {now.strftime('%Y-%m-%d %H:%M')}"
    batch_doc = {
        "badge_no": badge_no,
        "label": label,
        "service_name": service_names[0] if len(service_names) == 1 else "",
        "service_names": service_names,
        "network": _export_header_text(selected_network),
        "source": source_filter,
        "timeframe": timeframe["mode"],
        "timeframe_label": timeframe["label"],
        "date_from": timeframe["date_from"],
        "date_to": timeframe["date_to"],
        "count": len(rows),
        "format": fmt,
        "created_at": now,
        "created_by": session.get("user_id"),
        "lines": [
            {
                "line_id": row["line_id"],
                "line_index": row.get("line_index"),
                "phone": row.get("phone"),
                "offer": row.get("offer"),
                "network": row.get("network"),
                "source": row.get("source"),
                "order_id": row.get("order_id"),
                "order_db_id": row.get("order_db_id"),
                "service_name": row.get("service"),
                "line_status": row.get("line_status"),
                "status": row.get("status"),
                "provider_request_order_id": row.get("provider_request_order_id"),
            }
            for row in rows
        ],
    }
    export_batches_col.insert_one(batch_doc)
    return batch_doc

def _serialize_batch(batch):
    if not batch:
        return None
    return {
        "id": str(batch.get("_id")),
        "badge_no": batch.get("badge_no"),
        "label": batch.get("label"),
        "service_name": batch.get("service_name"),
        "service_names": batch.get("service_names") or [],
        "network": batch.get("network"),
        "source": batch.get("source"),
        "timeframe": batch.get("timeframe"),
        "timeframe_label": batch.get("timeframe_label"),
        "date_from": batch.get("date_from").strftime("%Y-%m-%d %H:%M") if batch.get("date_from") else "",
        "date_to": batch.get("date_to").strftime("%Y-%m-%d %H:%M") if batch.get("date_to") else "",
        "count": int(batch.get("count") or 0),
        "format": batch.get("format"),
        "created_at": batch.get("created_at").strftime("%Y-%m-%d %H:%M:%S UTC") if batch.get("created_at") else "",
        "lines": batch.get("lines") or [],
    }

def _serialize_batch_summary(batch):
    data = _serialize_batch(batch)
    data["lines"] = []
    return data

def _load_export_batch(batch_id):
    try:
        return export_batches_col.find_one({"_id": ObjectId(batch_id)})
    except Exception:
        return None

def _mark_batch_lines_delivered(batch, selected_line_ids=None):
    selected_keys = {str(x).strip() for x in (selected_line_ids or []) if str(x).strip()}
    mark_all = not selected_keys
    touched = 0
    changed_orders = 0

    lines_by_order = {}
    for line in (batch.get("lines") or []):
        line_id = str(line.get("line_id") or "").strip()
        if not line_id:
            continue
        if not mark_all and line_id not in selected_keys:
            continue
        lines_by_order.setdefault(str(line.get("order_db_id")), []).append(line)

    for order_db_id, target_lines in lines_by_order.items():
        try:
            order = orders_col.find_one({"_id": ObjectId(order_db_id)})
        except Exception:
            order = None
        if not order:
            continue

        changed = False
        items = list(order.get("items") or [])
        target_map = {}
        for line in target_lines:
            target_map[str(line.get("line_id"))] = line

        for idx, item in enumerate(items):
            current_line_id = _line_identifier(order, item, idx)
            spec = target_map.get(current_line_id)
            if not spec:
                continue
            if not _is_undelivered_line(item):
                continue
            items[idx]["line_status"] = "delivered"
            changed = True
            touched += 1

        if not changed:
            continue

        new_status = _compute_order_status_from_items(items, order.get("status"))
        update_doc = {
            "items": items,
            "status": new_status,
            "updated_at": datetime.utcnow(),
        }
        if new_status == "delivered":
            update_doc["delivered_at"] = order.get("delivered_at") or datetime.utcnow()

        result = orders_col.update_one({"_id": order["_id"]}, {"$set": update_doc})
        if result.modified_count:
            changed_orders += 1

    return touched, changed_orders

# ---------- CORE: apply status change (used by manual, bulk, scheduled) ----------
def _apply_status_change(order_ids: List[ObjectId], new_status: str, reason: str = "manual", actor_admin_id=None) -> Tuple[int, List[str]]:
    """
    Idempotent per-order updates, including wallet credit for refunds.
    Returns (updated_count, errors)
    """
    updated = 0
    errors  = []

    now = datetime.utcnow()
    for oid in order_ids:
        try:
            order = orders_col.find_one({"_id": oid})
            if not order:
                errors.append(f"{oid}: not found")
                continue

            old_status = (order.get("status") or "").lower()
            update_doc = {"status": new_status, "updated_at": now}
            # Delivered → set delivered_at if missing
            if new_status == "delivered" and not order.get("delivered_at"):
                update_doc["delivered_at"] = now

            # Refunded → single wallet credit based on charged_amount
            if new_status == "refunded":
                charged_amount = _money(order.get("charged_amount"), 0.0)
                user_id = order.get("user_id")
                already_refunded = bool(order.get("refunded_at")) or (old_status == "refunded")

                if charged_amount > 0 and user_id and not already_refunded:
                    try:
                        balances_col.update_one(
                            {"user_id": user_id},
                            {"$inc": {"amount": charged_amount}, "$set": {"updated_at": now}},
                            upsert=True
                        )
                        transactions_col.insert_one({
                            "user_id": user_id,
                            "amount": charged_amount,
                            "reference": order.get("order_id"),
                            "status": "success",
                            "type": "refund",
                            "gateway": "Wallet",
                            "currency": "GHS",
                            "created_at": now,
                            "verified_at": now,
                            "meta": {
                                "note": f"{reason.capitalize()} refund",
                                "order_db_id": oid,
                                "actor_admin_id": actor_admin_id,
                            }
                        })
                    except Exception as e:
                        errors.append(f"{oid}: refund ledger err: {e}")
                update_doc["refunded_at"] = now

            res = orders_col.update_one({"_id": oid}, {"$set": update_doc})
            if res.modified_count:
                # Flip line_status in items from processing -> delivered when marking delivered
                if new_status == "delivered":
                    try:
                        orders_col.update_one(
                            {"_id": oid},
                            {"$set": {"items.$[it].line_status": "delivered"}},
                            array_filters=[{"it.line_status": "processing"}]
                        )
                    except Exception:
                        pass
                updated += 1

        except Exception as e:
            errors.append(f"{oid}: {e}")

    return updated, errors

# ---------- DB-backed scheduler utilities ----------
def _enqueue_status_job(order_id_strs: List[str], new_status: str, run_time: datetime, admin_id: str | None, note: str | None):
    """
    Persist a job document that can be executed later (Render-safe).
    """
    now = datetime.utcnow()
    doc = {
        "job_key": str(uuid.uuid4()),
        "order_ids": order_id_strs,     # strings
        "status": new_status,
        "note": note or "",
        "admin_id": admin_id,
        "state": "scheduled",           # scheduled | running | done | error | cancelled
        "attempts": 0,
        "max_attempts": 3,
        "created_at": now,
        "run_at": run_time,             # UTC datetime
        "started_at": None,
        "finished_at": None,
        "result": None,                 # {updated, errors:[], ...}
        "lock_token": None,             # for cooperative locking
        "locked_at": None
    }
    schedules_col.insert_one(doc)
    return doc

def _process_due_jobs(max_batch: int = 25):
    """
    Cooperatively process due jobs. Safe to call at the top of admin routes
    and/or from a Render Cron ping.
    """
    now = datetime.utcnow()
    # pick up to max_batch jobs that are due and not locked/running/cancelled
    cursor = schedules_col.find({
        "state": {"$in": ["scheduled", "error"]},
        "run_at": {"$lte": now},
        "$or": [{"lock_token": None}, {"locked_at": {"$lt": now - timedelta(minutes=5)}}]
    }).sort([("run_at", 1)]).limit(max_batch)

    for job in cursor:
        lock_token = str(uuid.uuid4())
        # try to acquire lock
        claimed = schedules_col.update_one(
            {"_id": job["_id"], "lock_token": job.get("lock_token")},
            {"$set": {"lock_token": lock_token, "locked_at": now, "state": "running", "started_at": now}}
        )
        if not claimed.modified_count:
            continue

        # Execute
        try:
            ids = []
            for s in (job.get("order_ids") or []):
                try:
                    ids.append(ObjectId(s))
                except Exception:
                    pass
            updated, errors = _apply_status_change(ids, job.get("status"), reason="scheduled", actor_admin_id=job.get("admin_id"))
            schedules_col.update_one(
                {"_id": job["_id"], "lock_token": lock_token},
                {"$set": {
                    "state": "done" if not errors else "error",
                    "finished_at": datetime.utcnow(),
                    "attempts": (job.get("attempts", 0) + 1),
                    "result": {"updated": updated, "error_count": len(errors), "errors": errors}
                }}
            )
        except Exception as e:
            schedules_col.update_one(
                {"_id": job["_id"], "lock_token": lock_token},
                {"$set": {
                    "state": "error",
                    "finished_at": datetime.utcnow(),
                    "attempts": (job.get("attempts", 0) + 1),
                    "result": {"updated": 0, "error_count": 1, "errors": [str(e)]}
                }}
            )

# =========================================================
#                       ROUTES
# =========================================================
@admin_orders_bp.route("/admin/orders")
def admin_view_orders():
    if not _require_admin():
        return redirect(url_for("login.login"))

    # Opportunistically run any due jobs (cheap)
    try:
        _process_due_jobs(max_batch=10)
    except Exception:
        pass

    sort = (request.args.get("sort") or "newest").strip().lower()
    if sort not in ALLOWED_SORTS:
        sort = "newest"

    try:
        per_page = int(request.args.get("per_page", DEFAULT_PER_PAGE))
        per_page = max(1, min(per_page, 100))
    except Exception:
        per_page = DEFAULT_PER_PAGE

    try:
        page = int(request.args.get("page", 1))
        page = max(1, page)
    except Exception:
        page = 1

    skip = (page - 1) * per_page
    query = _build_query_from_params(request.args)

    sort_spec = [("created_at", -1)]
    if sort == "oldest":
        sort_spec = [("created_at", 1)]
    elif sort == "amount_desc":
        sort_spec = [("total_amount", -1), ("created_at", -1)]
    elif sort == "amount_asc":
        sort_spec = [("total_amount", 1), ("created_at", -1)]

    try:
        total_orders = orders_col.count_documents(query)
        total_pages  = max(1, (total_orders + per_page - 1) // per_page)
        orders       = list(orders_col.find(query).sort(sort_spec).skip(skip).limit(per_page))
        export_services, export_networks, _ = _build_export_catalog()

        for o in orders:
            uid = o.get("user_id")
            if isinstance(uid, str):
                try:
                    uid = ObjectId(uid)
                except Exception:
                    uid = None
            o["user"] = users_col.find_one({"_id": uid}) if uid else {}
            # Derive API flags (UI-only) — FIXED
            valid_providers = {"justice"}

            # statuses that should NEVER be considered "passed through API"
            NOT_PASSED_STATUSES = {
                "", "skipped", "not_sent", "n/a", "na",
                "not_applicable", "not_applicable_network", "not_applicable_type_off",
                "not_applicable_type", "not_applicable_provider"
            }

            # statuses that ARE considered real provider outcomes (extend if needed)
            PASSED_STATUSES = {
                "success", "ok", "done", "completed", "delivered", "processing"
            }

            providers_seen = set()
            api_passed = False

            for item in (o.get("items") or []):
                prov = _normalize_provider(item.get("provider"))
                if prov:
                    item["provider_norm"] = prov
                if prov in valid_providers:
                    providers_seen.add(prov)

                api_status_raw = item.get("api_status")
                api_status = (str(api_status_raw).strip().lower() if api_status_raw is not None else "")
                line_status = _normalize_status_text(item.get("line_status") or item.get("status"))
                has_provider_ref = any(
                    item.get(k)
                    for k in (
                        "provider_request_order_id",
                        "provider_order_id",
                        "provider_reference",
                        "provider_transaction_id",
                        "provider_txn_id",
                        "provider_ref",
                        "provider_ref_id",
                    )
                )

                # Normalize: treat any "not_applicable*" as NOT passed
                if api_status.startswith("not_applicable"):
                    continue

                # Passed only if provider is known + status is meaningful
                if prov in valid_providers and api_status and api_status not in NOT_PASSED_STATUSES:
                    # stricter: require it to be an allowed passed status
                    if api_status in PASSED_STATUSES:
                        api_passed = True
                    else:
                        # fallback: allow unknown-but-not-not_applicable statuses
                        api_passed = True
                elif prov == "justice" and not api_status:
                    if has_provider_ref or line_status in PASSED_STATUSES:
                        api_passed = True

            o["api_providers"] = sorted(providers_seen)
            o["api_passed"] = api_passed
    except Exception:
        flash("Error loading orders.", "danger")
        orders, total_pages, total_orders = [], 1, 0
        export_services, export_networks = [], ["MTN", "Telecel", "AirtelTigo"]

    return render_template(
        "admin_orders.html",
        orders=orders,
        page=page, total_pages=total_pages, total_orders=total_orders,
        status_filter=(request.args.get("status") or "").strip().lower(),
        order_id_q=(request.args.get("order_id") or "").strip(),
        customer_q=(request.args.get("customer") or "").strip(),
        paid_from=(request.args.get("paid_from") or "").strip().lower(),
        min_total=(request.args.get("min_total") or "").strip(),
        max_total=(request.args.get("max_total") or "").strip(),
        date_from=(request.args.get("date_from") or "").strip(),
        date_to=(request.args.get("date_to") or "").strip(),
        sort=sort, per_page=per_page,
        item_service=(request.args.get("item_service") or "").strip(),
        item_offer=(request.args.get("item_offer") or "").strip(),
        item_phone=(request.args.get("item_phone") or "").strip(),
        filters_query=_build_preserved_query(request.args),
        export_services=export_services,
        export_networks=export_networks,
        export_sources=EXPORT_SOURCE_OPTIONS,
    )

@admin_orders_bp.route("/admin/orders/<order_id>/update", methods=["POST"])
def update_order_status(order_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in ALLOWED_STATUSES:
        flash("Invalid status.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    try:
        oid = ObjectId(order_id)
    except Exception:
        flash("Invalid order id.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    updated, errors = _apply_status_change([oid], new_status, reason="manual", actor_admin_id=session.get("user_id"))
    if updated:
        msg = {
            "processing": "✅ Order marked as Processing.",
            "delivered": "✅ Order marked as Delivered.",
            "failed": "✅ Order marked as Failed.",
            "refunded": "✅ Order marked as Refunded (wallet credited if not already).",
            "pending": "✅ Order marked as Pending.",
            "completed": "✅ Order marked as Completed.",
        }.get(new_status, "✅ Order updated.")
        flash(msg, "success")
    else:
        flash("⚠️ No change to order.", "warning")
    if errors:
        flash(" | ".join(errors[:3]), "warning")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/bulk-deliver", methods=["POST"])
def bulk_deliver_orders():
    """
    Existing behavior: mark all orders that match CURRENT FILTERS and are processing -> delivered.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))
    args = request.args.to_dict(flat=True)
    args["status"] = "processing"
    query = _build_query_from_params(args)

    try:
        now = datetime.utcnow()
        # Find ids first (so we can also update line_status)
        ids = [o["_id"] for o in orders_col.find(query, {"_id": 1})]
        if ids:
            orders_col.update_many({"_id": {"$in": ids}}, {"$set": {"status": "delivered", "delivered_at": now, "updated_at": now}})
            try:
                orders_col.update_many(
                    {"_id": {"$in": ids}},
                    {"$set": {"items.$[it].line_status": "delivered"}},
                    array_filters=[{"it.line_status": "processing"}]
                )
            except Exception:
                pass
        modified = len(ids)
        flash(f"✅ Marked {modified} processing order(s) as Delivered.", "success")
    except Exception:
        flash("❌ Bulk update failed.", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# NEW: mark SELECTED ids as delivered (from checkboxes / floating bar)
@admin_orders_bp.route("/admin/orders/bulk-deliver-selected", methods=["POST"])
def bulk_deliver_selected():
    if not _require_admin():
        return redirect(url_for("login.login"))

    # Accept: order_ids (comma string) OR order_ids[] OR order_id[]
    raw_list = []
    if "order_ids" in request.form:
        raw_list += [request.form.get("order_ids") or ""]
    raw_list += request.form.getlist("order_ids[]")
    raw_list += request.form.getlist("order_id[]")
    raw_list = ",".join([s for s in raw_list if s]).split(",")

    ids = []
    for s in raw_list:
        try:
            ids.append(ObjectId((s or "").strip()))
        except Exception:
            pass

    if not ids:
        flash("Please select at least one order.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    now = datetime.utcnow()
    try:
        orders_col.update_many({"_id": {"$in": ids}}, {"$set": {"status": "delivered", "delivered_at": now, "updated_at": now}})
        try:
            orders_col.update_many(
                {"_id": {"$in": ids}},
                {"$set": {"items.$[it].line_status": "delivered"}},
                array_filters=[{"it.line_status": "processing"}]
            )
        except Exception:
            pass
        flash(f"✅ Marked {len(ids)} selected order(s) as Delivered.", "success")
    except Exception:
        flash("❌ Failed to bulk deliver selected.", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/export-undelivered", methods=["POST"])
def export_undelivered_orders():
    if not _require_admin():
        return redirect(url_for("login.login"))

    fmt = (request.form.get("format") or "txt").strip().lower()
    if fmt not in {"txt", "excel", "pdf"}:
        flash("Invalid export format.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    try:
        rows, timeframe, service_names = _collect_undelivered_rows(request.form)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))
    except RuntimeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))
    except Exception:
        flash("Failed to prepare undelivered export.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    if not rows:
        flash("No undelivered order lines matched your export filters.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    network = request.form.get("network")
    source_filter = (request.form.get("source") or "all").strip().lower()

    try:
        batch = _save_export_batch(rows, fmt, network, source_filter, timeframe, service_names)
    except Exception:
        flash("Failed to save export batch history.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    if fmt == "txt":
        file_obj = _render_txt_export(rows, network)
        filename = f"{batch.get('badge_no')}.txt"
        mimetype = "text/plain; charset=utf-8"
    elif fmt == "excel":
        try:
            file_obj = _render_excel_export(rows, network)
        except RuntimeError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
        filename = f"{batch.get('badge_no')}.xlsx"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        file_obj = _render_pdf_export(rows, network)
        filename = f"{batch.get('badge_no')}.pdf"
        mimetype = "application/pdf"

    return send_file(
        file_obj,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
    )

@admin_orders_bp.route("/admin/orders/export-batches", methods=["GET"])
def list_export_batches():
    if not _require_admin():
        return jsonify({"error": "unauthorized"}), 401

    batches = []
    for batch in export_batches_col.find({}).sort([("created_at", -1)]).limit(30):
        batches.append(_serialize_batch_summary(batch))
    return jsonify({"batches": batches})

@admin_orders_bp.route("/admin/orders/export-batches/<batch_id>", methods=["GET"])
def get_export_batch_detail(batch_id):
    if not _require_admin():
        return jsonify({"error": "unauthorized"}), 401

    batch = _load_export_batch(batch_id)
    if not batch:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"batch": _serialize_batch(batch)})

@admin_orders_bp.route("/admin/orders/export-batches/<batch_id>/mark-delivered", methods=["POST"])
def mark_export_batch_delivered(batch_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    batch = _load_export_batch(batch_id)
    if not batch:
        flash("Export batch not found.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    selected_line_ids = request.form.getlist("line_id")
    touched, changed_orders = _mark_batch_lines_delivered(batch, selected_line_ids)
    if touched:
        flash(
            f"Marked {touched} exported line(s) as delivered across {changed_orders} order(s).",
            "success",
        )
    else:
        flash("No eligible exported lines were updated.", "warning")
    return redirect(url_for("admin_orders.admin_view_orders"))

# =========================================================
#            DB-BACKED SCHEDULING ENDPOINTS (Admin)
# =========================================================
@admin_orders_bp.route("/admin/orders/schedule-status", methods=["POST"])
def schedule_status():
    """
    Form fields:
      - order_ids: comma-separated string OR multiple order_ids[] fields OR order_id[]
      - status: one of ALLOWED_STATUSES
      - delay_minutes: int (optional)
      - run_at: "YYYY-MM-DD HH:MM" (UTC, optional)
      - note: optional
    One of delay_minutes or run_at is required.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))

    status = (request.form.get("status") or "").strip().lower()
    if status not in ALLOWED_STATUSES:
        flash("Invalid status for scheduling.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    # collect ids
    raw_list = []
    if "order_ids" in request.form:
        raw_list += [request.form.get("order_ids") or ""]
    raw_list += request.form.getlist("order_ids[]")
    raw_list += request.form.getlist("order_id[]")
    raw_list = ",".join([s for s in raw_list if s]).split(",")

    order_id_strs = []
    bad_ids = []
    for s in raw_list:
        s2 = (s or "").strip()
        if not s2:
            continue
        try:
            ObjectId(s2)
            order_id_strs.append(s2)
        except Exception:
            bad_ids.append(s2)

    if not order_id_strs:
        flash("Please select at least one valid order.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    # compute run time
    delay_str  = (request.form.get("delay_minutes") or "").strip()
    run_at_str = (request.form.get("run_at") or "").strip()
    run_time   = None

    if delay_str:
        try:
            mins = int(delay_str)
            run_time = datetime.utcnow() + timedelta(minutes=max(0, mins))
        except Exception:
            flash("Invalid delay minutes.", "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
    elif run_at_str:
        dt = _parse_date(run_at_str)
        if not dt:
            flash("Invalid run_at datetime. Use 'YYYY-MM-DD HH:MM' (UTC).", "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
        run_time = dt
        if run_time < datetime.utcnow():
            flash("Run time must be in the future.", "warning")
            return redirect(url_for("admin_orders.admin_view_orders"))
    else:
        flash("Provide either delay_minutes or run_at.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    note = (request.form.get("note") or "").strip()
    admin_id = (session.get("user_id") or None)
    job = _enqueue_status_job(order_id_strs, status, run_time, str(admin_id) if admin_id else None, note)

    flash(f"⏱️ Scheduled {len(order_id_strs)} order(s) → {status} at {run_time.strftime('%Y-%m-%d %H:%M')} UTC.", "success")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/schedules", methods=["GET"])
def list_schedules():
    """Returns JSON of recent schedules (for the offcanvas in the UI)."""
    if not _require_admin():
        return redirect(url_for("login.login"))
    # Also opportunistically process due jobs when viewing the list
    try:
        _process_due_jobs(max_batch=25)
    except Exception:
        pass

    jobs = []
    for j in schedules_col.find({}).sort([("created_at", -1)]).limit(100):
        jobs.append({
            "id": str(j.get("_id")),
            "job_key": j.get("job_key"),
            "next_run_time": j.get("run_at").strftime("%Y-%m-%d %H:%M:%S UTC") if j.get("run_at") else None,
            "state": j.get("state"),
            "status": j.get("status"),
            "args": [j.get("order_ids"), j.get("status")],
            "result": j.get("result"),
            "attempts": j.get("attempts", 0),
        })
    return jsonify({"jobs": jobs})

@admin_orders_bp.route("/admin/orders/schedules/<job_id>/cancel", methods=["POST"])
def cancel_schedule(job_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    try:
        res = schedules_col.update_one({"_id": ObjectId(job_id)}, {"$set": {"state": "cancelled"}})
        if res.modified_count:
            flash("🗑️ Schedule cancelled.", "success")
        else:
            flash("Schedule not found.", "warning")
    except Exception as e:
        flash(f"Failed to cancel schedule: {e}", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# Optional: endpoint you can ping from Render Cron every minute
@admin_orders_bp.route("/admin/orders/schedules/run-due", methods=["POST", "GET"])
def run_due_schedules():
    if not _require_admin():
        # If you want cron w/o session, you can protect via secret token instead
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        _process_due_jobs(max_batch=50)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
