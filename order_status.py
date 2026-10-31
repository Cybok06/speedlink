from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Blueprint, jsonify

from db import db


order_status_bp = Blueprint("order_status", __name__)

orders_col = db["orders"]
afa_col = db["afa_registrations"]

FINAL_ORDER_STATUS = "delivered"
ACTIVE_ORDER_STATUSES = {"pending", "processing"}
ACTIVE_LINE_STATUSES = {"pending", "processing", "queued", "submitted"}
EXTERNAL_PROVIDERS = {"skplug", "dataconnect", "emma_data"}
AFA_ACTIVE_STATUSES = {"pending", "processing"}

PROVIDER_BASE_URL = (os.getenv("CAMPUS_DATA_BASE_URL") or "https://campus-data-2i8o.onrender.com").strip().rstrip("/")
PROVIDER_API_KEY = (os.getenv("CAMPUS_DATA_API_KEY") or "").strip()
PROVIDER_TIMEOUT = int((os.getenv("CAMPUS_DATA_TIMEOUT") or "30").strip() or "30")
SYNC_INTERVAL_MINUTES = max(int((os.getenv("ORDER_STATUS_SYNC_INTERVAL_MINUTES") or "3").strip() or "3"), 1)
SYNC_STALE_MINUTES = max(int((os.getenv("ORDER_STATUS_SYNC_STALE_MINUTES") or "2").strip() or "2"), 1)
SYNC_BATCH_LIMIT = max(min(int((os.getenv("ORDER_STATUS_SYNC_BATCH_LIMIT") or "25").strip() or "25"), 100), 1)


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "completed":
        return "delivered"
    return text


def _provider_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {PROVIDER_API_KEY}",
        "Accept": "application/json",
    }


def _compute_order_status_from_items(items: List[Dict[str, Any]], current_status: Any = None) -> str:
    if _normalize_status(current_status) == FINAL_ORDER_STATUS:
        return FINAL_ORDER_STATUS

    statuses = [_normalize_status(item.get("line_status")) for item in (items or [])
                if not str(item.get("line_status") or "").startswith("skipped_")]
    if not statuses:
        return "processing"
    if all(status == "delivered" for status in statuses):
        return "delivered"
    if all(status == "failed" for status in statuses):
        return "failed"
    if all(status == "pending" for status in statuses):
        return "pending"
    if any(status in {"processing", "queued"} for status in statuses):
        return "processing"
    if any(status == "pending" for status in statuses):
        return "processing"
    return "processing"


def _parse_json_response(resp: requests.Response) -> Dict[str, Any]:
    text = resp.text or ""
    try:
        body = resp.json() if text.strip() else {}
    except Exception:
        body = {"raw": text} if text else {}
    return body if isinstance(body, dict) else {"data": body}


def _status_url_for_item(item: Dict[str, Any]) -> str:
    if str(item.get("provider") or "").strip().lower() == "emma_data":
        # Emma requires our original purchase ref, rather than the returned EDK id.
        reference = str(item.get("provider_request_order_id") or "").strip()
        return f"https://emmadataking.com/api/v1/order-status/{quote(reference, safe='')}" if reference else ""
    api_response = item.get("api_response") or {}
    response_block = api_response.get("response") if isinstance(api_response, dict) else {}
    status_url = ""
    if isinstance(response_block, dict):
        status_url = str(response_block.get("status_url") or "").strip()
    if status_url:
        return status_url

    provider_order_id = str(item.get("provider_order_id") or item.get("provider_reference") or "").strip()
    if not provider_order_id:
        return ""
    return f"{PROVIDER_BASE_URL}/api/external/orders/{provider_order_id}"


def _fetch_provider_order_status(item: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    if str(item.get("provider") or "").strip().lower() == "emma_data":
        return _fetch_emma_order_status(item)
    if not PROVIDER_API_KEY:
        return False, {"success": False, "message": "CAMPUS_DATA_API_KEY is not configured", "http_status": 500}

    url = _status_url_for_item(item)
    if not url:
        return False, {"success": False, "message": "Missing provider order reference", "http_status": 400}

    try:
        resp = requests.get(url, headers=_provider_headers(), timeout=PROVIDER_TIMEOUT)
        body = _parse_json_response(resp)
        body.setdefault("http_status", resp.status_code)
        ok = 200 <= resp.status_code < 300 and body.get("success") is True
        return ok, body
    except requests.RequestException as exc:
        return False, {"success": False, "message": str(exc), "http_status": 599}


def _fetch_emma_order_status(item: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    api_key = (os.getenv("EMMA_DATA_API_KEY") or "").strip()
    if not api_key:
        return False, {"message": "EMMA_DATA_API_KEY is not configured", "http_status": 500}
    url = _status_url_for_item(item)
    if not url:
        return False, {"message": "Missing Emma Data purchase reference", "http_status": 400}
    try:
        timeout = float(os.getenv("EMMA_DATA_TIMEOUT") or "45")
        if not 0 < timeout <= 300:
            raise ValueError
    except ValueError:
        return False, {"message": "Invalid EMMA_DATA_TIMEOUT", "http_status": 500}
    try:
        resp = requests.get(url, headers={"Content-Type": "application/json", "X-API-Key": api_key},
                            timeout=(10, timeout), allow_redirects=False)
        try:
            body = resp.json()
        except ValueError:
            return False, {"message": "Emma Data returned invalid JSON", "http_status": resp.status_code}
        if not isinstance(body, dict):
            return False, {"message": "Emma Data returned an invalid status response", "http_status": resp.status_code}
        body["http_status"] = resp.status_code
        data = body.get("data")
        valid_statuses = {"completed", "delivered", "success", "successful", "failed", "cancelled", "canceled",
                          "refunded", "reversed", "pending", "queued", "submitted", "processing", "in_progress", "inprogress"}
        ok = (200 <= resp.status_code < 300 and body.get("status") == "success"
              and isinstance(data, dict) and str(data.get("status") or "").strip().lower() in valid_statuses)
        if ok and data.get("reference") and str(data["reference"]) != str(item.get("provider_request_order_id")):
            return False, {"message": "Emma Data returned a different purchase reference", "http_status": resp.status_code}
        return ok, body
    except requests.RequestException:
        return False, {"message": "Could not retrieve Emma Data order status", "http_status": 599}


def _fetch_afa_registration_status(registration: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    if not PROVIDER_API_KEY:
        return False, {"success": False, "message": "CAMPUS_DATA_API_KEY is not configured", "http_status": 500}

    client_reference = str(registration.get("provider_client_reference") or "").strip()
    if not client_reference:
        return False, {"success": False, "message": "Missing provider client reference", "http_status": 400}

    url = f"{PROVIDER_BASE_URL}/api/external/afa/registrations/{client_reference}"
    try:
        resp = requests.get(url, headers=_provider_headers(), timeout=PROVIDER_TIMEOUT)
        body = _parse_json_response(resp)
        body.setdefault("http_status", resp.status_code)
        ok = 200 <= resp.status_code < 300 and body.get("success") is True
        return ok, body
    except requests.RequestException as exc:
        return False, {"success": False, "message": str(exc), "http_status": 599}


def _map_provider_status(status_raw: Any) -> Tuple[str, str]:
    status = _normalize_status(status_raw)

    if status in {"delivered", "success", "successful"}:
        return "delivered", "success"
    if status in {"failed", "cancelled", "canceled", "refunded", "reversed"}:
        return "failed", "failed"
    if status in {"pending"}:
        return "pending", "pending"
    if status in {"queued", "submitted"}:
        return "processing", "submitted"
    if status in {"processing", "in_progress", "inprogress"}:
        return "processing", "processing"
    return "processing", "processing"


def _extract_line_payload(payload: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    lines = payload.get("items")
    if not isinstance(lines, list) or not lines:
        return {}

    service_id = str(item.get("serviceId") or "").strip()
    phone = str(item.get("phone") or "").strip()
    size = str(item.get("value") or "").strip()

    for line in lines:
        if not isinstance(line, dict):
            continue
        if service_id and str(line.get("service_id") or "").strip() != service_id:
            continue
        if phone and str(line.get("phone") or "").strip() != phone:
            continue
        if size and str(line.get("size") or "").strip() != size:
            continue
        return line

    first = lines[0]
    return first if isinstance(first, dict) else {}


def _apply_provider_status_to_item(item: Dict[str, Any], payload: Dict[str, Any], checked_at: datetime) -> bool:
    current_line_status = _normalize_status(item.get("line_status"))
    if current_line_status == FINAL_ORDER_STATUS:
        return False

    is_emma = str(item.get("provider") or "").strip().lower() == "emma_data"
    status_payload = payload.get("data", {}) if is_emma else payload
    line_payload = {} if is_emma else _extract_line_payload(payload, item)
    status_candidate = (
        line_payload.get("line_status")
        or status_payload.get("status")
        or line_payload.get("api_status")
        or payload.get("api_status")
    )
    new_line_status, new_api_status = _map_provider_status(status_candidate)

    changed = False

    updates = {
        "provider_status_checked_at": checked_at,
        "provider_status_payload": payload,
        "api_response": {**(item.get("api_response") or {}), "status_response": payload} if is_emma else payload,
    }
    if is_emma:
        updates.update(
            provider_status=status_payload.get("status"),
            provider_follow_up_required=new_line_status == "failed",
        )
        for source, target in (("transaction_code", "provider_transaction_id"), ("amount", "provider_amount"),
                               ("delivered_at", "provider_delivered_at")):
            if status_payload.get(source) is not None:
                updates[target] = status_payload[source]

    for key, value in updates.items():
        if item.get(key) != value:
            item[key] = value
            changed = True

    if item.get("line_status") != new_line_status:
        item["line_status"] = new_line_status
        changed = True

    if item.get("api_status") != new_api_status:
        item["api_status"] = new_api_status
        changed = True

    provider_order_id = str(status_payload.get("id" if is_emma else "order_id") or "").strip()
    if provider_order_id and item.get("provider_order_id") != provider_order_id:
        item["provider_order_id"] = provider_order_id
        changed = True

    provider_reference = provider_order_id
    if provider_reference and item.get("provider_reference") != provider_reference:
        item["provider_reference"] = provider_reference
        changed = True

    provider_network = str(status_payload.get("network" if is_emma else "resolved_network") or item.get("provider_network") or "").strip()
    if provider_network and item.get("provider_network") != provider_network:
        item["provider_network"] = provider_network
        changed = True

    if line_payload:
        amount = line_payload.get("amount")
        if amount is not None and item.get("provider_amount") != amount:
            item["provider_amount"] = amount
            changed = True

    return changed


def _candidate_query(now: datetime) -> Dict[str, Any]:
    stale_before = now - timedelta(minutes=SYNC_STALE_MINUTES)
    references = []
    if PROVIDER_API_KEY:
        references.append({"provider": {"$in": ["skplug", "dataconnect"]},
                           "provider_order_id": {"$exists": True, "$nin": [None, ""]}})
    if (os.getenv("EMMA_DATA_API_KEY") or "").strip():
        references.append({"provider": "emma_data",
                           "provider_request_order_id": {"$exists": True, "$nin": [None, ""]}})
    if not references:
        return {"_id": {"$in": []}}
    return {
        "status": {"$in": list(ACTIVE_ORDER_STATUSES)},
        "items": {
            "$elemMatch": {
                "line_status": {"$in": list(ACTIVE_LINE_STATUSES)},
                "$and": [{"$or": references}],
                "$or": [
                    {"provider_status_checked_at": {"$exists": False}},
                    {"provider_status_checked_at": {"$lt": stale_before}},
                ],
            }
        },
    }


def _afa_candidate_query(now: datetime) -> Dict[str, Any]:
    stale_before = now - timedelta(minutes=SYNC_STALE_MINUTES)
    return {
        "status": {"$in": list(AFA_ACTIVE_STATUSES)},
        "provider": "dataconnect",
        "provider_service_id": "AFA_REGISTRATION",
        "provider_client_reference": {"$exists": True, "$ne": None, "$nin": [""]},
        "$or": [
            {"provider_status_checked_at": {"$exists": False}},
            {"provider_status_checked_at": {"$lt": stale_before}},
        ],
    }


def _apply_afa_status_to_registration(registration: Dict[str, Any], payload: Dict[str, Any], checked_at: datetime) -> bool:
    current_status = _normalize_status(registration.get("status"))
    if current_status == FINAL_ORDER_STATUS:
        return False

    new_status, new_api_status = _map_provider_status(payload.get("status"))
    changed = False

    updates = {
        "provider_status_checked_at": checked_at,
        "provider_status_payload": payload,
        "external_api_response": payload,
    }
    for key, value in updates.items():
        if registration.get(key) != value:
            registration[key] = value
            changed = True

    if registration.get("status") != new_status:
        registration["status"] = new_status
        changed = True

    if registration.get("external_api_status") != new_api_status:
        registration["external_api_status"] = new_api_status
        changed = True

    provider_reference = str(payload.get("registration_id") or registration.get("provider_reference") or "").strip()
    if provider_reference and registration.get("provider_reference") != provider_reference:
        registration["provider_reference"] = provider_reference
        changed = True

    return changed


def _run_order_status_sync() -> Dict[str, Any]:
    now = datetime.utcnow()
    summary = {
        "checked_orders": 0,
        "checked_lines": 0,
        "updated_orders": 0,
        "updated_lines": 0,
        "checked_afa_registrations": 0,
        "updated_afa_registrations": 0,
        "timestamp": now.isoformat() + "Z",
        "batch_limit": SYNC_BATCH_LIMIT,
        "stale_minutes": SYNC_STALE_MINUTES,
    }

    cursor = (
        orders_col.find(_candidate_query(now))
        .sort([("updated_at", 1), ("created_at", 1)])
        .limit(SYNC_BATCH_LIMIT)
    )

    emma_checks = 0
    emma_rate_limited = False
    for order in cursor:
        if _normalize_status(order.get("status")) == FINAL_ORDER_STATUS:
            continue

        summary["checked_orders"] += 1
        items = order.get("items") or []
        # Retain the original snapshot for the conditional write below.
        original_items = deepcopy(items)
        order_changed = False

        for item in items:
            provider = str(item.get("provider") or "").strip().lower()
            line_status = _normalize_status(item.get("line_status"))
            reference_key = "provider_request_order_id" if provider == "emma_data" else "provider_order_id"
            provider_order_id = str(item.get(reference_key) or "").strip()

            if provider not in EXTERNAL_PROVIDERS:
                continue
            if line_status not in ACTIVE_LINE_STATUSES:
                continue
            if not provider_order_id:
                continue
            last_checked = item.get("provider_status_checked_at")
            if isinstance(last_checked, datetime) and last_checked >= now - timedelta(minutes=SYNC_STALE_MINUTES):
                continue
            if provider == "emma_data":
                if not (os.getenv("EMMA_DATA_API_KEY") or "").strip() or emma_rate_limited or emma_checks >= 200:
                    continue
                emma_checks += 1
            elif not PROVIDER_API_KEY:
                continue

            checked_at = now
            summary["checked_lines"] += 1
            ok, payload = _fetch_provider_order_status(item)

            if not ok:
                if provider == "emma_data" and payload.get("http_status") == 429:
                    emma_rate_limited = True
                if item.get("provider_status_checked_at") != checked_at:
                    item["provider_status_checked_at"] = checked_at
                    item["provider_status_payload"] = payload
                    order_changed = True
                continue

            if _apply_provider_status_to_item(item, payload, checked_at):
                summary["updated_lines"] += 1
                order_changed = True

        if not order_changed:
            continue

        new_status = _compute_order_status_from_items(items, current_status=order.get("status"))
        update_filter: Dict[str, Any] = {"_id": order["_id"], "status": order.get("status"), "items": original_items}
        result = orders_col.update_one(
            update_filter,
            {"$set": {"items": items, "status": new_status, "updated_at": now}},
        )
        if result.modified_count:
            summary["updated_orders"] += 1

    afa_cursor = (
        afa_col.find(_afa_candidate_query(now))
        .sort([("updated_at", 1), ("created_at", 1)])
        .limit(SYNC_BATCH_LIMIT)
    )

    for registration in afa_cursor:
        if not PROVIDER_API_KEY:
            break
        if _normalize_status(registration.get("status")) == FINAL_ORDER_STATUS:
            continue

        summary["checked_afa_registrations"] += 1
        checked_at = now
        ok, payload = _fetch_afa_registration_status(registration)

        if not ok:
            update_doc = {
                "provider_status_checked_at": checked_at,
                "provider_status_payload": payload,
                "updated_at": checked_at,
            }
            afa_col.update_one({"_id": registration["_id"], "status": {"$ne": FINAL_ORDER_STATUS}}, {"$set": update_doc})
            continue

        if not _apply_afa_status_to_registration(registration, payload, checked_at):
            continue

        result = afa_col.update_one(
            {"_id": registration["_id"], "status": {"$ne": FINAL_ORDER_STATUS}},
            {"$set": {
                "status": registration.get("status"),
                "external_api_status": registration.get("external_api_status"),
                "external_api_response": registration.get("external_api_response"),
                "provider_reference": registration.get("provider_reference"),
                "provider_status_checked_at": registration.get("provider_status_checked_at"),
                "provider_status_payload": registration.get("provider_status_payload"),
                "updated_at": checked_at,
            }},
        )
        if result.modified_count:
            summary["updated_afa_registrations"] += 1

    return summary


@order_status_bp.route("/order-status-sync", methods=["GET"])
def sync_order_status():
    summary = _run_order_status_sync()
    return jsonify({"success": True, "summary": summary}), 200


def _scheduled_sync_job() -> None:
    try:
        _run_order_status_sync()
    except Exception:
        return


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    _scheduled_sync_job,
    "interval",
    minutes=SYNC_INTERVAL_MINUTES,
    max_instances=1,
    coalesce=True,
    id="campus_external_order_status_sync",
    replace_existing=True,
)

try:
    scheduler.start()
except Exception:
    pass
