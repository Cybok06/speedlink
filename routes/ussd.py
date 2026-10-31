from __future__ import annotations

import ast
import json
import os
import random
import re
import requests
import string
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from flask import Blueprint, jsonify, request

from db import db
from checkout import (
    _build_bundle_key,
    _has_processing_conflict_strict,
    _resolve_network_id,
    _resolve_network_slug,
    _service_unavailability_reason,
    generate_order_id,
    jlog,
)


ussd_bp = Blueprint("ussd", __name__, url_prefix="/ussd")

services_col = db["services"]
orders_col = db["orders"]
ussd_sessions_col = db["ussd_sessions"]
ussd_orders_col = db["ussd_orders"]
ussd_logs_col = db["ussd_logs"]
moolre_payment_logs_col = db["moolre_payment_logs"]
ussd_payment_sessions_col = db["ussd_payment_sessions"]

MOOLRE_BASE_URL = os.getenv("MOOLRE_BASE_URL", "https://api.moolre.com")
MOOLRE_API_USER = os.getenv("MOOLRE_API_USER", "")
MOOLRE_PUB_KEY = os.getenv("MOOLRE_PUB_KEY", "")
MOOLRE_ACCOUNT_NUMBER = os.getenv("MOOLRE_ACCOUNT_NUMBER", "")
MOOLRE_SECRET_KEY = os.getenv("MOOLRE_SECRET_KEY", "")

SESSION_TTL_SECONDS = 300
OFFERS_PER_PAGE = 5

MENU_NETWORK = "MENU_NETWORK"
MENU_AT_SERVICE = "MENU_AT_SERVICE"
MENU_RECIPIENT = "MENU_RECIPIENT"
INPUT_PHONE = "INPUT_PHONE"
MENU_OFFERS = "MENU_OFFERS"
CONFIRM_ORDER = "CONFIRM_ORDER"
COMPLETE = "COMPLETE"

try:
    ussd_sessions_col.create_index("session_id", unique=True)
    ussd_sessions_col.create_index("expires_at", expireAfterSeconds=0)
    ussd_orders_col.create_index("order_id", unique=True)
    ussd_logs_col.create_index([("created_at", -1)])
    moolre_payment_logs_col.create_index([("created_at", -1)])
    ussd_payment_sessions_col.create_index("externalref", unique=True)
    ussd_payment_sessions_col.create_index([("created_at", -1)])
except Exception as e:
    print(f"[ussd] index setup failed: {e}")


def _now() -> datetime:
    return datetime.utcnow()


def _expires_at() -> datetime:
    return _now() + timedelta(seconds=SESSION_TTL_SECONDS)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = re.sub(r"[^0-9.\-]+", "", value)
        return float(value)
    except Exception:
        return default


def normalize_gh_phone(phone):
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    if len(digits) == 9:
        return "0" + digits
    if digits.startswith("0") and len(digits) == 10:
        return digits
    return digits


def phone_variants(phone):
    local = normalize_gh_phone(phone)
    if not local:
        return []
    variants = {local}
    if local.startswith("0") and len(local) == 10:
        variants.add("233" + local[1:])
        variants.add(local[1:])
    return list(variants)


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
        print(f"[ussd] previous order lookup failed: {e}")
        return False


def is_payment_webhook(payload):
    if not isinstance(payload, dict):
        return False

    keys = {str(k).lower() for k in payload.keys()}
    ussd_keys = {"sessionid", "msisdn", "message", "new", "network"}

    if "externalref" in keys or "transactionid" in keys or "transaction_id" in keys:
        return True
    if "amount" in keys and "status" in keys and "sessionid" not in keys:
        return True

    data = _parse_nested_data(payload.get("data"))
    data_keys = {str(k).lower() for k in data.keys()}
    if data_keys.intersection({"externalref", "transactionid", "transaction_id", "reference"}):
        return True

    return bool(keys.intersection({"amount", "status", "code", "reference", "payer", "accountnumber"}) and not keys.intersection(ussd_keys))


def _parse_nested_data(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


def _payload_value(payload, *keys):
    if not isinstance(payload, dict):
        return None

    lower_map = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lower_map.get(str(key).lower())
        if value not in (None, ""):
            return value

    data = _parse_nested_data(payload.get("data"))
    lower_data = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lower_data.get(str(key).lower())
        if value not in (None, ""):
            return value

    return None


def _response_code(payload):
    value = _payload_value(payload, "code", "statusCode", "status_code")
    return str(value or "").strip().upper()


def _payment_externalref(payload):
    return _payload_value(
        payload,
        "externalref",
        "externalRef",
        "reference",
        "transactionid",
        "transaction_id",
    )


def _payment_is_success(payload):
    status = str(_payload_value(payload, "status") or "").strip().lower()
    code = str(_payload_value(payload, "code") or "").strip().lower()
    success_values = {"1", "success", "successful", "paid", "completed", "complete", "approved"}
    success_codes = {"1", "00", "000", "200", "ok", "success", "successful"}
    return status in success_values or code in success_codes


def _payment_is_failed(payload):
    status = str(_payload_value(payload, "status") or "").strip().lower()
    code = str(_payload_value(payload, "code") or "").strip().lower()
    failed_values = {"0", "failed", "failure", "error", "cancelled", "canceled", "declined"}
    failed_codes = {"0", "99", "400", "401", "500", "failed", "error"}
    return status in failed_values or code in failed_codes


def _save_moolre_payment_log(payload, direction="webhook", extra=None):
    doc = {
        "direction": direction,
        "payload": payload,
        "raw_body": request.get_data(as_text=True) if request else "",
        "headers": dict(request.headers) if request else {},
        "content_type": request.content_type if request else None,
        "created_at": _now(),
    }
    if extra:
        doc.update(extra)
    try:
        moolre_payment_logs_col.insert_one(doc)
    except Exception as e:
        print(f"[moolre] payment log failed: {e}")


def handle_moolre_payment_webhook(payload):
    _save_moolre_payment_log(payload, direction="webhook")

    externalref = _payment_externalref(payload)
    amount = _payload_value(payload, "amount")
    status_value = _payload_value(payload, "status")
    code_value = _payload_value(payload, "code")

    if not externalref:
        return jsonify({"message": "payment webhook received", "reply": False}), 200

    session_doc = ussd_payment_sessions_col.find_one({"externalref": str(externalref)})
    if not session_doc:
        return jsonify({"message": "payment webhook received", "reply": False}), 200

    update_doc = {
        "provider_payload": payload,
        "provider_amount": amount,
        "provider_status": status_value,
        "provider_code": code_value,
        "updated_at": _now(),
    }

    if _payment_is_success(payload):
        update_doc["status"] = "paid"
        update_doc["paid_at"] = _now()
        update_doc["todo"] = "create/process real order after successful Moolre payment"
    elif _payment_is_failed(payload):
        update_doc["status"] = "failed"
        update_doc["failed_at"] = _now()
    else:
        update_doc["status"] = "webhook_received"

    try:
        ussd_payment_sessions_col.update_one(
            {"externalref": str(externalref)},
            {"$set": update_doc},
        )
    except Exception as e:
        print(f"[moolre] payment session update failed: {e}")

    return jsonify({"message": "payment webhook received", "reply": False}), 200


def moolre_channel_from_ussd_network(network):
    mapping = {
        "3": "13",
        3: "13",
        "5": "7",
        5: "7",
        "6": "6",
        6: "6",
    }
    return mapping.get(network)


def initiate_moolre_payment(msisdn, amount, externalref, session_id, network, description):
    url = f"{MOOLRE_BASE_URL.rstrip('/')}/open/transact/payment"
    payer_phone = normalize_gh_phone(msisdn)
    if not payer_phone.startswith("0") or len(payer_phone) != 10:
        return {
            "status": "0",
            "code": "LOCAL_VALIDATION",
            "message": "Invalid payer phone number format",
        }

    body = {
        "type": 1,
        "channel": moolre_channel_from_ussd_network(network),
        "currency": "GHS",
        "payer": payer_phone,
        "amount": str(amount),
        "externalref": externalref,
        "reference": description,
        "sessionid": session_id,
        "accountnumber": MOOLRE_ACCOUNT_NUMBER,
    }
    headers = {
        "Content-Type": "application/json",
        "X-API-USER": MOOLRE_API_USER,
        "X-API-PUBKEY": MOOLRE_PUB_KEY,
    }

    log_extra = {"url": url, "request_body": body}
    try:
        response = requests.post(url, headers=headers, json=body, timeout=15)
        try:
            response_payload = response.json()
        except Exception:
            response_payload = {"text": response.text}
        _save_moolre_payment_log(
            response_payload,
            direction="initiate_response",
            extra={**log_extra, "http_status": response.status_code},
        )
        return response_payload
    except Exception as e:
        error_payload = {"error": str(e)}
        _save_moolre_payment_log(error_payload, direction="initiate_error", extra=log_extra)
        return error_payload


def create_ussd_payment_session(session_doc: Dict[str, Any]) -> Dict[str, Any]:
    offer = session_doc.get("selected_offer") or {}
    amount = round(_to_float(offer.get("amount")), 2)
    session_id = str(session_doc.get("session_id") or "")
    msisdn = str(session_doc.get("msisdn") or "")
    network = session_doc.get("moolre_network")
    externalref = f"POV-USSD-{re.sub(r'[^A-Za-z0-9]+', '', session_id)[-12:]}-{uuid.uuid4().hex[:8]}"
    description = f"SpeedLink {offer.get('package_label') or 'Data'}"

    doc = {
        "externalref": externalref,
        "session_id": session_id,
        "msisdn": msisdn,
        "network": network,
        "amount": amount,
        "status": "payment_pending",
        "selected_network": session_doc.get("selected_network"),
        "selected_service_id": session_doc.get("selected_service_id"),
        "selected_service_name": session_doc.get("selected_service_name"),
        "recipient_number": session_doc.get("recipient_number"),
        "selected_offer": offer,
        "created_at": _now(),
        "updated_at": _now(),
    }
    ussd_payment_sessions_col.insert_one(doc)
    response_payload = initiate_moolre_payment(
        msisdn=msisdn,
        amount=amount,
        externalref=externalref,
        session_id=session_id,
        network=network,
        description=description,
    )
    ussd_payment_sessions_col.update_one(
        {"externalref": externalref},
        {"$set": {"initiation_response": response_payload, "updated_at": _now()}},
    )
    doc["initiation_response"] = response_payload
    return doc


def _read_callback_payload() -> Dict[str, Any]:
    raw = request.get_data(as_text=True).strip()

    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    if raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    if request.form:
        form_data = request.form.to_dict(flat=True)

        if len(form_data) == 1:
            only_key = next(iter(form_data.keys()))
            only_value = form_data.get(only_key)

            if str(only_value or "") == "" and str(only_key or "").strip().startswith("{"):
                try:
                    parsed = json.loads(only_key)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass

        return form_data

    if request.values:
        return request.values.to_dict(flat=True)

    raw = request.get_data(as_text=True).strip()
    if raw:
        return {"message": raw}

    return {}


def _ussd_response(message: str, reply: bool = True, payload: Optional[Dict[str, Any]] = None):
    body = {"message": message, "reply": bool(reply)}
    _log_ussd(payload or {}, body)
    return jsonify(body), 200


def _log_ussd(payload: Dict[str, Any], response_body: Dict[str, Any]) -> None:
    try:
        ussd_logs_col.insert_one({
            "payload": payload,
            "raw_body": request.get_data(as_text=True),
            "response": response_body,
            "headers": dict(request.headers),
            "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
            "content_type": request.content_type,
            "created_at": _now(),
        })
    except Exception as e:
        print(f"[ussd] log failed: {e}")


def _service_tokens(service: Dict[str, Any]) -> set[str]:
    values = [
        service.get("name"),
        service.get("network"),
        service.get("service_network"),
        service.get("network_id"),
        service.get("provider_network"),
    ]
    tokens = {_norm(v) for v in values if v is not None}
    joined = _norm(" ".join(str(v) for v in values if v is not None))
    if joined:
        tokens.add(joined)
    return tokens


def _service_is_open(service: Dict[str, Any]) -> bool:
    return str(service.get("status") or "OPEN").strip().upper() == "OPEN"


def _service_is_available(service: Dict[str, Any]) -> bool:
    availability = str(service.get("availability") or "AVAILABLE").strip().upper()
    return availability not in {"OUT_OF_STOCK", "OUT OF STOCK", "OUTOFSTOCK"}


def _service_sort_key(service: Dict[str, Any]) -> Tuple[int, str]:
    name = str(service.get("name") or "")
    priority = service.get("priority")
    try:
        priority_val = int(priority)
    except Exception:
        priority_val = 9999
    return priority_val, name.lower()


def _matches_service(service: Dict[str, Any], aliases: set[str]) -> bool:
    tokens = _service_tokens(service)
    if tokens.intersection(aliases):
        return True
    joined = " ".join(tokens)
    return any(len(alias) > 2 and alias in joined for alias in aliases)


def _open_service_candidates() -> List[Dict[str, Any]]:
    services = list(services_col.find(
        {},
        {
            "name": 1,
            "network": 1,
            "service_network": 1,
            "network_id": 1,
            "provider_network": 1,
            "status": 1,
            "availability": 1,
            "public_offers": 1,
            "provider": 1,
            "priority": 1,
            "display_order": 1,
            "created_at": 1,
        },
    ))
    return [service for service in services if _service_is_open(service)]


def get_available_services_for_network(network_key: str) -> List[Dict[str, Any]]:
    network_key = _norm(network_key)
    alias_map = {
        "mtn": {"mtn", "mtnnormal", "3"},
        "telecel": {"telecel", "vodafone", "2"},
        "at": {"airteltigo", "at", "airteltigoishare", "airteltigobigtime", "atishare", "atbigtime", "1"},
        "at_ishare": {"airteltigoishare", "atishare"},
        "at_bigtime": {"airteltigobigtime", "atbigtime"},
    }
    aliases = alias_map.get(network_key, {network_key})
    services = [s for s in _open_service_candidates() if _matches_service(s, aliases)]

    if network_key == "at_ishare":
        services = [s for s in services if "bigtime" not in _norm(s.get("name"))]
    if network_key == "at_bigtime":
        services = [s for s in services if "bigtime" in _norm(s.get("name"))]
    if network_key == "mtn":
        normal = [s for s in services if "express" not in _norm(s.get("name"))]
        services = normal or services

    return sorted([s for s in services if _service_is_available(s)], key=_service_sort_key)


def _find_open_services_including_oos(network_key: str) -> List[Dict[str, Any]]:
    network_key = _norm(network_key)
    alias_map = {
        "telecel": {"telecel", "vodafone", "2"},
        "mtn": {"mtn", "mtnnormal", "3"},
        "at": {"airteltigo", "at", "1"},
    }
    aliases = alias_map.get(network_key, {network_key})
    return sorted([s for s in _open_service_candidates() if _matches_service(s, aliases)], key=_service_sort_key)


def _parse_value(value: Any) -> Any:
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                try:
                    parsed = ast.literal_eval(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
        return text
    return value


def _extract_volume_mb(value: Any) -> Optional[float]:
    parsed = _parse_value(value)
    if isinstance(parsed, dict):
        return _extract_volume_mb(parsed.get("volume"))
    if isinstance(parsed, (int, float)):
        return float(parsed)
    text = str(parsed or "").strip()
    if not text:
        return None
    gb_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:gb|gig)", text, re.I)
    if gb_match:
        return float(gb_match.group(1)) * 1000
    mb_match = re.search(r"(\d+(?:\.\d+)?)\s*mb", text, re.I)
    if mb_match:
        return float(mb_match.group(1))
    number_match = re.search(r"\d+(?:\.\d+)?", text)
    if number_match:
        return float(number_match.group(0))
    return None


def format_volume(value: Any) -> str:
    volume = _extract_volume_mb(value)
    if volume is None:
        parsed = _parse_value(value)
        return str(parsed or "-")
    if volume >= 1000:
        gb = volume / 1000
        return f"{int(gb)}GB" if gb.is_integer() else f"{gb:.2f}GB"
    return f"{int(volume)}MB" if volume.is_integer() else f"{volume:.2f}MB"


def format_price(amount: Any) -> str:
    price = _to_float(amount)
    if price.is_integer():
        return f"GHS {int(price)}"
    return f"GHS {price:.2f}"


def _public_offers(service: Dict[str, Any]) -> List[Dict[str, Any]]:
    offers = service.get("public_offers")
    if not isinstance(offers, list):
        return []

    clean_offers = [o for o in offers if isinstance(o, dict)]
    clean_offers.sort(key=lambda o: (
        _extract_volume_mb(o.get("value")) if _extract_volume_mb(o.get("value")) is not None else float("inf"),
        _to_float(o.get("amount")),
    ))
    return clean_offers


def normalize_phone(phone: Any) -> Optional[str]:
    local = normalize_gh_phone(phone)
    if len(local) == 10 and local.startswith("0"):
        digits = "233" + local[1:]
    elif len(local) == 12 and local.startswith("233"):
        digits = local
    else:
        return None

    prefixes = {"20", "24", "26", "27", "53", "54", "55", "57"}
    if len(digits) == 12 and digits.startswith("233") and digits[3:5] in prefixes:
        return digits
    return None


def get_or_create_session(session_id: str, msisdn: str, is_new: bool = False) -> Dict[str, Any]:
    now = _now()
    if not is_new:
        existing = ussd_sessions_col.find_one({"session_id": session_id, "expires_at": {"$gt": now}})
        if existing:
            return existing

    session_doc = {
        "session_id": session_id,
        "msisdn": msisdn,
        "stage": MENU_NETWORK,
        "selected_network": None,
        "selected_service_id": None,
        "selected_service_name": None,
        "recipient_number": None,
        "recipient_type": None,
        "selected_offer": None,
        "moolre_network": None,
        "offer_page": 0,
        "created_at": now,
        "updated_at": now,
        "expires_at": _expires_at(),
    }
    ussd_sessions_col.update_one(
        {"session_id": session_id},
        {"$set": session_doc},
        upsert=True,
    )
    return session_doc


def update_session(session_id: str, updates: Dict[str, Any]) -> None:
    updates["updated_at"] = _now()
    updates["expires_at"] = _expires_at()
    ussd_sessions_col.update_one({"session_id": session_id}, {"$set": updates}, upsert=False)


def _load_service(service_id: Any) -> Optional[Dict[str, Any]]:
    try:
        return services_col.find_one({"_id": ObjectId(str(service_id))})
    except Exception:
        return None


def _generate_order_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"USSD-{_now().strftime('%Y%m%d')}-{suffix}"


def create_ussd_order(session_doc: Dict[str, Any]) -> Dict[str, Any]:
    offer = session_doc.get("selected_offer") or {}
    service_id_raw = session_doc.get("selected_service_id")
    svc_doc = _load_service(service_id_raw)
    if not svc_doc:
        raise ValueError("Service currently unavailable.")

    is_unavailable, reason_text = _service_unavailability_reason(svc_doc)
    if is_unavailable:
        raise ValueError(reason_text)

    raw_value = offer.get("raw_offer_value")
    value_obj = _parse_value(raw_value)
    if not isinstance(value_obj, dict):
        value_obj = {"volume": offer.get("package_volume")}

    amount = round(_to_float(offer.get("amount")), 2)
    recipient_phone = normalize_gh_phone(session_doc.get("recipient_number"))
    if not recipient_phone:
        raise ValueError("Invalid recipient number.")

    order_id = generate_order_id()
    service_name = svc_doc.get("name") or session_doc.get("selected_service_name") or "Service"
    service_type = svc_doc.get("type")
    item = {
        "phone": recipient_phone,
        "base_amount": amount,
        "amount": amount,
        "profit_amount": 0.0,
        "profit_percent_used": 0.0,
        "value": offer.get("package_label") or format_volume(raw_value),
        "value_obj": value_obj,
        "serviceId": str(svc_doc.get("_id")),
        "serviceName": service_name,
        "service_type": service_type,
        "network": session_doc.get("selected_network"),
    }

    network_id = _resolve_network_id(item, value_obj, svc_doc)
    bundle_key = _build_bundle_key(value_obj, item)
    amount_key = round(float(amount), 2)
    line_status = "processing"
    api_status = "not_applicable_provider"
    api_response = {"note": "Queued for manual processing."}
    provider = "manual"
    provider_network = None
    external_ref = None

    if _has_processing_conflict_strict(recipient_phone, str(svc_doc.get("_id")), service_name, network_id, bundle_key, amount_key):
        line_status = "skipped_duplicate_processing"
        api_status = "skipped"
        api_response = {"note": "Same line already processing; skipping."}
        amount_to_charge = 0.0
        status = "skipped"
    else:
        amount_to_charge = amount
        status = "pending"
        svc_type_flag = (service_type or "").strip().upper() if isinstance(service_type, str) else ""

        jlog(
            "ussd_line_routing",
            order_id=order_id,
            serviceId=str(svc_doc.get("_id")),
            serviceName=service_name,
            resolved_network=_resolve_network_slug(svc_doc, item),
            api_allowed=False,
            selected_provider="manual",
        )

        provider = "manual"
        provider_network = _resolve_network_slug(svc_doc, item)
        api_status = "manual_processing"
        api_response = {
            "note": "Automatic provider ordering is disabled for USSD; queued for manual processing.",
            "service_type_flag": svc_type_flag,
            "resolved_network": provider_network,
        }

    line_record = {
        **item,
        "provider": provider,
        "provider_network": provider_network,
        "provider_reference": None,
        "provider_order_id": None,
        "provider_request_order_id": external_ref,
        "network_id": network_id,
        "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None,
        "line_amount_key": amount_key,
        "line_status": line_status,
        "api_status": api_status,
        "api_response": api_response,
    }

    order_doc = {
        "user_id": None,
        "order_id": order_id,
        "items": [line_record],
        "total_amount": amount,
        "charged_amount": round(amount_to_charge, 2),
        "profit_amount_total": 0.0,
        "status": status,
        "paid_from": "ussd",
        "source": "USSD",
        "ussd": {
            "session_id": session_doc.get("session_id"),
            "customer_msisdn": normalize_phone(session_doc.get("msisdn")) or session_doc.get("msisdn"),
        },
        "created_at": _now(),
        "updated_at": _now(),
        "debug": {"events": []},
    }
    orders_col.insert_one(order_doc)

    return order_doc


def _network_menu() -> str:
    return "Welcome to SpeedLink\nSelect Network:\n1. MTN\n2. Telecel\n3. AirtelTigo"


def _at_service_menu() -> str:
    return "AirtelTigo Services:\n1. AT iShare\n2. AT BigTime\n0. Back"


def _display_local_phone(phone: Any) -> str:
    local = normalize_gh_phone(phone)
    return local if local.startswith("0") and len(local) == 10 else str(phone or "")


def _recipient_menu(session_doc: Dict[str, Any]) -> str:
    self_phone = _display_local_phone(session_doc.get("msisdn"))
    label = f"Self ({self_phone})" if self_phone else "Self"
    return f"Recipient:\n1. {label}\n2. Other number\n0. Back"


def build_offer_menu(service: Dict[str, Any], page: int = 0) -> str:
    offers = _public_offers(service)
    if not offers:
        return "No packages available for this network."

    start = max(0, int(page or 0)) * OFFERS_PER_PAGE
    page_offers = offers[start:start + OFFERS_PER_PAGE]
    lines = ["Select data package:"]
    for idx, offer in enumerate(page_offers, start=1):
        lines.append(f"{idx}. {format_volume(offer.get('value'))} - {format_price(offer.get('amount'))}")
    if start + OFFERS_PER_PAGE < len(offers):
        lines.append("9. More")
    lines.append("0. Back")
    return "\n".join(lines)


def _select_service_for_network(session_id: str, network_key: str, display_network: str) -> Tuple[Optional[str], bool]:
    if network_key == "telecel":
        open_services = _find_open_services_including_oos("telecel")
        if open_services and not any(_service_is_available(s) for s in open_services):
            return "Telecel is currently unavailable. Please try another network.", True

    services = get_available_services_for_network(network_key)
    if not services:
        return "Service currently unavailable. Please try again later.", True

    service = services[0]
    update_session(session_id, {
        "stage": MENU_RECIPIENT,
        "selected_network": display_network,
        "selected_service_id": str(service.get("_id")),
        "selected_service_name": service.get("name"),
        "selected_service_provider": service.get("provider"),
        "offer_page": 0,
    })
    session_doc = {
        "session_id": session_id,
        "msisdn": "",
    }
    try:
        session_doc = ussd_sessions_col.find_one({"session_id": session_id}) or session_doc
    except Exception:
        pass
    return _recipient_menu(session_doc), True


def _handle_network_menu(session_doc: Dict[str, Any], message: str) -> Tuple[str, bool]:
    session_id = session_doc["session_id"]
    if message == "1":
        return _select_service_for_network(session_id, "mtn", "MTN")
    if message == "2":
        return _select_service_for_network(session_id, "telecel", "Telecel")
    if message == "3":
        at_services = get_available_services_for_network("at")
        if not at_services:
            return "Service currently unavailable. Please try again later.", True
        update_session(session_id, {"stage": MENU_AT_SERVICE, "selected_network": "AirtelTigo"})
        return _at_service_menu(), True
    return "Invalid option. Please try again.\n" + _network_menu(), True


def _handle_at_service_menu(session_doc: Dict[str, Any], message: str) -> Tuple[str, bool]:
    session_id = session_doc["session_id"]
    if message == "0":
        update_session(session_id, {"stage": MENU_NETWORK})
        return _network_menu(), True

    if message not in {"1", "2"}:
        return "Invalid option. Please try again.\n" + _at_service_menu(), True

    network_key = "at_ishare" if message == "1" else "at_bigtime"
    display_name = "AirtelTigo"
    return _select_service_for_network(session_id, network_key, display_name)


def _go_to_offer_menu(session_doc: Dict[str, Any], recipient_number: str, recipient_type: str) -> Tuple[str, bool]:
    service = _load_service(session_doc.get("selected_service_id"))
    if not service:
        return "Service currently unavailable. Please try again later.", True
    if not _service_is_available(service):
        return "This network is currently out of stock. Please choose another network.", True
    if not _public_offers(service):
        return "No packages available for this network.", True

    update_session(session_doc["session_id"], {
        "stage": MENU_OFFERS,
        "recipient_number": recipient_number,
        "recipient_type": recipient_type,
        "offer_page": 0,
    })
    return build_offer_menu(service, 0), True


def _handle_recipient_menu(session_doc: Dict[str, Any], message: str) -> Tuple[str, bool]:
    if message == "0":
        if session_doc.get("selected_network") == "AirtelTigo":
            update_session(session_doc["session_id"], {"stage": MENU_AT_SERVICE})
            return _at_service_menu(), True
        update_session(session_doc["session_id"], {"stage": MENU_NETWORK})
        return _network_menu(), True

    if message == "1":
        normalized = normalize_phone(session_doc.get("msisdn"))
        if not normalized:
            return "Your number is not valid. Choose Other number.\n" + _recipient_menu(session_doc), True
        return _go_to_offer_menu(session_doc, normalized, "self")

    if message == "2":
        update_session(session_doc["session_id"], {"stage": INPUT_PHONE, "recipient_type": "other"})
        return "Enter recipient phone number:", True

    return "Invalid option. Please try again.\n" + _recipient_menu(session_doc), True


def _handle_phone_input(session_doc: Dict[str, Any], message: str) -> Tuple[str, bool]:
    if message == "0":
        update_session(session_doc["session_id"], {"stage": MENU_RECIPIENT})
        return _recipient_menu(session_doc), True

    normalized = normalize_phone(message)
    if not normalized:
        return "Invalid phone number. Enter a valid Ghana number like 0530393625.", True

    return _go_to_offer_menu(session_doc, normalized, "other")


def _handle_offer_menu(session_doc: Dict[str, Any], message: str) -> Tuple[str, bool]:
    session_id = session_doc["session_id"]
    service = _load_service(session_doc.get("selected_service_id"))
    if not service:
        return "Service currently unavailable. Please try again later.", True
    offers = _public_offers(service)
    if not offers:
        return "No packages available for this network.", True

    page = int(session_doc.get("offer_page") or 0)
    if message == "0":
        update_session(session_id, {"stage": MENU_RECIPIENT, "offer_page": 0})
        return _recipient_menu(session_doc), True
    if message == "9":
        next_page = page + 1
        if next_page * OFFERS_PER_PAGE >= len(offers):
            next_page = 0
        update_session(session_id, {"offer_page": next_page})
        return build_offer_menu(service, next_page), True

    try:
        selected_num = int(message)
    except Exception:
        return "Invalid option. Please try again.\n" + build_offer_menu(service, page), True

    if selected_num < 1 or selected_num > OFFERS_PER_PAGE:
        return "Invalid option. Please try again.\n" + build_offer_menu(service, page), True

    offer_idx = page * OFFERS_PER_PAGE + (selected_num - 1)
    if offer_idx >= len(offers):
        return "Invalid option. Please try again.\n" + build_offer_menu(service, page), True

    offer = offers[offer_idx]
    amount = _to_float(offer.get("amount"))
    selected_offer = {
        "package_volume": _extract_volume_mb(offer.get("value")),
        "package_label": format_volume(offer.get("value")),
        "amount": round(amount, 2),
        "raw_offer_value": offer.get("value"),
    }
    update_session(session_id, {"stage": CONFIRM_ORDER, "selected_offer": selected_offer})

    return (
        "Confirm order:\n"
        f"Network: {session_doc.get('selected_network')}\n"
        f"Number: {_display_local_phone(session_doc.get('recipient_number'))}\n"
        f"Package: {selected_offer['package_label']}\n"
        f"Amount: {format_price(amount)}\n"
        "1. Confirm\n"
        "2. Cancel"
    ), True


def _handle_confirm_order(session_doc: Dict[str, Any], message: str) -> Tuple[str, bool]:
    if message == "2":
        update_session(session_doc["session_id"], {"stage": COMPLETE})
        return "Order cancelled. Thank you for using SpeedLink.", False
    if message != "1":
        return "Invalid option. Please try again.\n1. Confirm\n2. Cancel", True

    try:
        payment_doc = create_ussd_payment_session(session_doc)
        update_session(session_doc["session_id"], {"stage": COMPLETE})
    except Exception as e:
        print(f"[ussd] payment initiation failed: {e}")
        return "Payment request could not be sent. Please try again later.", False

    if _response_code(payment_doc.get("initiation_response")) == "TR03":
        return "Payment request failed because your phone number format is invalid. Please try again later.", False

    return "Payment request sent. Please approve the prompt on your phone.", False


@ussd_bp.post("/callback")
def ussd_callback():
    payload = _read_callback_payload()

    if is_payment_webhook(payload):
        return handle_moolre_payment_webhook(payload)

    session_id = str(payload.get("sessionid") or payload.get("sessionId") or "").strip()
    msisdn = str(payload.get("msisdn") or "").strip()
    message = str(payload.get("message") or "").strip()
    network = payload.get("network")
    is_new = _truthy(payload.get("new"))
    caller_phone = (
        payload.get("msisdn")
        or payload.get("phone")
        or payload.get("phoneNumber")
        or payload.get("customer_phone")
    )

    authorized = has_any_previous_order(caller_phone)
    print("USSD caller_phone:", caller_phone)
    print("USSD phone_variants:", phone_variants(caller_phone))
    print("USSD authorized:", authorized)

    if not authorized:
        return _ussd_response("", False, payload)

    if not session_id:
        return _ussd_response("Service currently unavailable. Please try again later.", False, payload)

    try:
        session_doc = get_or_create_session(session_id, msisdn, is_new=is_new)
        session_updates = {"msisdn": msisdn}
        if network not in (None, ""):
            session_updates["moolre_network"] = network
        update_session(session_id, session_updates)
        session_doc.update(session_updates)

        if is_new or session_doc.get("stage") == MENU_NETWORK:
            if is_new or not message:
                update_session(session_id, {"stage": MENU_NETWORK, "msisdn": msisdn})
                return _ussd_response(_network_menu(), True, payload)
            response_message, reply = _handle_network_menu(session_doc, message)
            return _ussd_response(response_message, reply, payload)

        stage = session_doc.get("stage")
        if stage == MENU_AT_SERVICE:
            response_message, reply = _handle_at_service_menu(session_doc, message)
        elif stage == MENU_RECIPIENT:
            response_message, reply = _handle_recipient_menu(session_doc, message)
        elif stage == INPUT_PHONE:
            response_message, reply = _handle_phone_input(session_doc, message)
        elif stage == MENU_OFFERS:
            response_message, reply = _handle_offer_menu(session_doc, message)
        elif stage == CONFIRM_ORDER:
            response_message, reply = _handle_confirm_order(session_doc, message)
        else:
            update_session(session_id, {"stage": MENU_NETWORK})
            response_message, reply = _network_menu(), True

        return _ussd_response(response_message, reply, payload)
    except Exception as e:
        print(f"[ussd] callback failed: {e}")
        return _ussd_response("Service currently unavailable. Please try again later.", False, payload)
