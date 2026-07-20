from datetime import datetime
import json
import re

from flask import Blueprint, jsonify, request

from db import db


callback_response_bp = Blueprint(
    "callback_response",
    __name__,
    url_prefix="/callback",
)

callback_response_col = db["callback_response"]
orders_col = db["orders"]


def _parse_payload(raw_body):
    parsed_payload = request.get_json(silent=True)
    if parsed_payload is not None:
        return parsed_payload

    form_payload = request.form.to_dict(flat=False)
    if form_payload:
        return form_payload

    return raw_body


def normalize_gh_phone(phone):
    digits = re.sub(r"\D+", "", str(phone or ""))

    if len(digits) == 12 and digits.startswith("233"):
        return "0" + digits[3:]
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    if len(digits) == 9:
        return "0" + digits

    return None


def phone_variants(phone):
    local = normalize_gh_phone(phone)
    if not local:
        return []

    international = "233" + local[1:]
    without_prefix = local[1:]
    return list(dict.fromkeys([local, international, without_prefix]))


def has_any_previous_order(phone):
    variants = phone_variants(phone)
    if not variants:
        return False

    query = {
        "$or": [
            {"customer_phone": {"$in": variants}},
            {"phone": {"$in": variants}},
            {"recipient_phone": {"$in": variants}},
            {"phones": {"$in": variants}},
            {"items.phone": {"$in": variants}},
        ]
    }

    try:
        return orders_col.find_one(query, {"_id": 1}) is not None
    except Exception as e:
        print(f"[callback_response] order lookup failed: {e}")
        return False


def _extract_phone(payload, raw_body):
    phone_keys = ("msisdn", "phone", "phoneNumber", "customer_phone")

    if isinstance(payload, dict):
        for key in phone_keys:
            value = payload.get(key)
            if value:
                return value

    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                for key in phone_keys:
                    value = parsed.get(key)
                    if value:
                        return value
        except Exception:
            pass

    for key in phone_keys:
        match = re.search(rf"{key}\s*[:=]\s*['\"]?(\+?\d{{9,12}})", raw_body or "", re.IGNORECASE)
        if match:
            return match.group(1)

    return None


@callback_response_bp.get("/test")
def test_callback():
    return jsonify({
        "success": True,
        "message": "Callback URL is working",
    })


@callback_response_bp.post("/response")
def save_callback_response():
    raw_body = request.get_data(as_text=True)
    parsed_payload = _parse_payload(raw_body)

    document = {
        "payload": parsed_payload,
        "raw_body": raw_body,
        "headers": dict(request.headers),
        "method": request.method,
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "content_type": request.content_type,
        "created_at": datetime.utcnow(),
    }

    try:
        callback_response_col.insert_one(document)
    except Exception as e:
        print(f"[callback_response] insert failed: {e}")
        pass

    phone = _extract_phone(parsed_payload, raw_body)
    if not phone or not has_any_previous_order(phone):
        return jsonify({
            "message": "unauthorized number",
            "reply": False,
        }), 200

    return jsonify({
        "message": "authorized number",
        "reply": False,
    }), 200
