# index.py — Public landing page ONLY (no checkout / no orders)
from __future__ import annotations

from flask import Blueprint, render_template, jsonify, request, abort
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import json, ast, re, os
import requests
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from db import db
from paystack_config import get_paystack_keys
from phone_number_guard import evaluate_phone_order_eligibility

index_bp = Blueprint("index", __name__)

# --- DB collections ---
services_col = db["services"]
orders_col = db["orders"]
transactions_col = db["transactions"]
payment_sessions_col = db["payment_sessions"]

PUBLIC_PAYSTACK_FEE_RATE = 0.02

try:
    payment_sessions_col.create_index([("reference", 1)], unique=True)
    payment_sessions_col.create_index([("updated_at", -1)])
    orders_col.create_index(
        [("paystack_reference", 1)],
        unique=True,
        partialFilterExpression={"paystack_reference": {"$exists": True, "$type": "string"}},
    )
except Exception:
    pass

# Store-host guard: hansmart.store should not serve the public landing page
STORE_PUBLIC_HOST = (os.getenv("STORE_PUBLIC_HOST", "www.hansmart.store") or "").strip().lower()
_STORE_HOSTS = {STORE_PUBLIC_HOST, STORE_PUBLIC_HOST.lstrip("www.")}

def _host_only(v: str) -> str:
    return (v or "").split(":", 1)[0].strip().lower()

try:
    from checkout import (  # type: ignore
        _coerce_value_obj,
        _resolve_network_id,
        _resolve_network_slug,
        _service_unavailability_reason,
        _build_bundle_key,
        _has_processing_conflict_strict,
        _dispatch_service_order,
        _service_provider_name,
        _service_uses_external_provider,
        EXTERNAL_PROVIDER_NAMES,
        generate_order_id,
        _money,
        jlog,
    )
except Exception:  # pragma: no cover
    from .checkout import (  # type: ignore
        _coerce_value_obj,
        _resolve_network_id,
        _resolve_network_slug,
        _service_unavailability_reason,
        _build_bundle_key,
        _has_processing_conflict_strict,
        _dispatch_service_order,
        _service_provider_name,
        _service_uses_external_provider,
        EXTERNAL_PROVIDER_NAMES,
        generate_order_id,
        _money,
        jlog,
    )

# ---------------- small helpers (local, no checkout imports) ----------------

def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

def _money(v: Any) -> float:
    """Simple money normalizer -> 2dp float."""
    try:
        return round(float(v or 0.0), 2)
    except Exception:
        return 0.0

_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)
_mapping_like = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)

def _service_unit(svc: Dict[str, Any]) -> str:
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes"):
        return "minutes"
    if name == "afa talktime":
        return "minutes"
    return "data"

def _parse_value_field(value: Any) -> Any:
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        vt = value.strip()
        if vt.startswith("{") and vt.endswith("}"):
            try:
                data = json.loads(vt)
                if isinstance(data, dict):
                    return data
            except Exception:
                try:
                    if _mapping_like.match(vt):
                        data = ast.literal_eval(vt)
                        if isinstance(data, dict):
                            return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    """
    For unit == 'data' -> we treat volume as MB.
    For unit == 'minutes' -> we treat volume as minutes.
    """
    if isinstance(value, dict):
        vol = value.get("volume")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            return float(vol)
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m: return float(m.group(1))
            if _NUM.match(vol_s): return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m: return float(m.group(1))
            if _NUM.match(vol_s): return float(vol_s)
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m: return float(m.group(1))
            if _NUM.match(s): return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m: return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m: return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m: return float(m.group(1))
            if _NUM.match(s2): return float(s2)
            return None

    return None

def _format_volume_unit(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    if v >= 1000:
        gb = v / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(v)}MB"

def _value_text_for_display(value: Any, unit: str) -> str:
    if isinstance(value, dict):
        vol = _extract_volume(value, unit)
        return _format_volume_unit(vol, unit) if vol is not None else "-"
    if isinstance(value, str):
        cleaned = _PKG_TAIL.sub("", value).strip()
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"

def _norm(s: str) -> str:
    return (s or "").strip().lower()

PREFERRED_ORDER: List[str] = ["MTN NORMAL", "MTN", "AT - iShare", "AT - BigTime", "AFA TALKTIME"]

def _name_rank(name: str) -> Optional[int]:
    n = _norm(name)
    for i, want in enumerate(PREFERRED_ORDER):
        if _norm(want) == n:
            return i
    n2 = " ".join(n.split())
    for i, want in enumerate(PREFERRED_ORDER):
        if " ".join(_norm(want).split()) == n2:
            return i
    return None

def _created_ts(service_doc: Dict[str, Any]) -> float:
    ca = service_doc.get("created_at")
    if isinstance(ca, datetime):
        return ca.timestamp()
    try:
        val = float(ca)
        if val > 1e12:
            return val / 1000.0
        return val
    except Exception:
        return 0.0

def _service_priority_tuple(svc: Dict[str, Any]):
    prio = _to_float(svc.get("priority"))
    prio = prio if prio is not None else float("inf")
    name = svc.get("name") or ""
    nrank = _name_rank(name)
    nrank = nrank if nrank is not None else 10_000
    display_order = _to_float(svc.get("display_order"))
    display_order = display_order if display_order is not None else float("inf")
    ts = -_created_ts(svc)
    alpha = _norm(name)
    return (prio, nrank, display_order, ts, alpha)

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()
    availability = (svc.get("availability") or "AVAILABLE").upper()
    closed_msg = (svc.get("closed_message") or "This service is temporarily closed.")
    oos_msg = (svc.get("out_of_stock_message") or "This service is currently out of stock.")
    can_order = (status == "OPEN" and availability == "AVAILABLE")
    disabled_reason = None
    if not can_order:
        if status != "OPEN":
            disabled_reason = closed_msg
        elif availability != "AVAILABLE":
            disabled_reason = oos_msg
        else:
            disabled_reason = "This service is currently unavailable."
    return {
        "type": t,
        "status": status,
        "availability": availability,
        "closed_message": closed_msg,
        "out_of_stock_message": oos_msg,
        "can_order": can_order,
        "disabled_reason": disabled_reason,
    }

def _public_offers_list(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    public_offers = svc.get("public_offers")
    if isinstance(public_offers, list) and public_offers:
        return public_offers
    offers = svc.get("offers")
    if isinstance(offers, list) and offers:
        return offers
    return []

def _offer_base_amount(of: Dict[str, Any]) -> Optional[float]:
    if not isinstance(of, dict):
        return None
    return _to_float(of.get("amount"))

def _canonical_public_total_for_offer(
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    offers = _public_offers_list(svc_doc)
    if not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")
    for idx, of in enumerate(offers):
        parsed = _parse_value_field(of.get("value"))
        vol = _extract_volume(parsed, unit)
        if vol_needed is not None and vol is not None:
            diff = abs(float(vol) - float(vol_needed))
            if diff < best_diff:
                best_idx, best_diff = idx, diff
        elif best_idx is None:
            best_idx = idx

    if best_idx is None:
        return None
    base_amount = _offer_base_amount(offers[best_idx])
    return round(float(base_amount), 2) if base_amount is not None else None


def _canonical_system_base_for_offer(
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    offers = svc_doc.get("offers")
    if not isinstance(offers, list) or not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")
    for idx, of in enumerate(offers):
        parsed = _parse_value_field(of.get("value"))
        vol = _extract_volume(parsed, unit)
        if vol_needed is not None and vol is not None:
            diff = abs(float(vol) - float(vol_needed))
            if diff < best_diff:
                best_idx, best_diff = idx, diff
        elif best_idx is None:
            best_idx = idx

    if best_idx is None:
        return None
    base_amount = _offer_base_amount(offers[best_idx])
    return round(float(base_amount), 2) if base_amount is not None else None

# ------------------ data prep for landing page ------------------

def load_services_for_landing() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load all services, normalize offers for display only.
    No wallet, no orders, no external providers.
    """
    exclude_ids = {"689614b822955887cad51b7d"}
    exclude_names = {"mtn express", "afa talktime"}
    raw = list(services_col.find({}))
    raw.sort(key=_service_priority_tuple)

    services: List[Dict[str, Any]] = []
    for s in raw:
        if str(s.get("_id")) in exclude_ids:
            continue
        if _norm(s.get("name") or "") in exclude_names:
            continue
        s = dict(s)
        s["_id_str"] = str(s["_id"])
        st = _service_state(s)
        s.update(st)

        unit = _service_unit(s)
        offers = _public_offers_list(s)

        normalized_offers: List[Dict[str, Any]] = []
        for of in offers:
            parsed_value = _parse_value_field(of.get("value"))
            vol_num = _extract_volume(parsed_value, unit)
            value_text = _value_text_for_display(parsed_value, unit)

            amount = _to_float(of.get("amount"))
            total = amount if amount is not None else None

            normalized_offers.append(
                {
                    "amount": amount,
                    "value": parsed_value,
                    "value_text": value_text,
                    "total": total,
                    "_sort_vol": vol_num if vol_num is not None else float("inf"),
                    "_sort_amt": amount if amount is not None else float("inf"),
                }
            )

        normalized_offers.sort(key=lambda x: (x["_sort_vol"], x["_sort_amt"]))
        s["offers"] = [
            {k: v for k, v in o.items() if not k.startswith("_sort_")}
            for o in normalized_offers
        ]
        s["unit"] = unit

        services.append(s)

    return services, []


def _verify_paystack(reference: str) -> Tuple[bool, Dict[str, Any], str]:
    paystack_secret_key = (get_paystack_keys().get("secret_key") or "").strip()
    if not paystack_secret_key:
        return (False, {}, "Payment processor not configured.")
    try:
        headers = {"Authorization": f"Bearer {paystack_secret_key}"}
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        r = requests.get(url, headers=headers, timeout=25)
        result = r.json()
        if not result.get("status"):
            return (False, result, result.get("message") or "Verification failed.")
        data = result.get("data") or {}
        ok = data.get("status") == "success"
        if not ok:
            return (False, data, data.get("gateway_response") or "Payment not successful.")
        return (True, data, "")
    except Exception as e:
        return (False, {}, f"Verify error: {str(e)}")

def _paid_enough(paid_pesewas: int, expected_pesewas: int) -> bool:
    return int(paid_pesewas or 0) >= int(expected_pesewas or 0)

def _calc_public_paystack_totals(base_total: float) -> Dict[str, float]:
    base = round(float(base_total or 0.0), 2)
    fee = round(base * PUBLIC_PAYSTACK_FEE_RATE, 2)
    total = round(base + fee, 2)
    return {"base_total": base, "fee": fee, "paystack_total": total}

def _load_payment_session(reference: str) -> Optional[Dict[str, Any]]:
    return payment_sessions_col.find_one({"reference": reference})

def _append_processing_note(reference: str, note: str):
    try:
        payment_sessions_col.update_one(
            {"reference": reference},
            {
                "$push": {"processing_notes": {"at": datetime.utcnow(), "note": note}},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )
    except Exception:
        pass

def _find_public_order_by_reference(reference: str) -> Optional[Dict[str, Any]]:
    if not reference:
        return None
    return orders_col.find_one({"paystack_reference": reference, "paid_from": "public_paystack"})

def _reprice_public_cart(cart: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], float]:
    server_cart: List[Dict[str, Any]] = []
    total_requested = 0.0

    for item in (cart or []):
        service_id_raw = item.get("serviceId")
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
        svc_doc = None
        if service_id_raw:
            try:
                svc_doc = services_col.find_one({"_id": ObjectId(service_id_raw)})
            except Exception:
                svc_doc = None

        canonical = _canonical_public_total_for_offer(
            svc_doc or {}, value_obj, item.get("value")
        ) if svc_doc else None
        if canonical is None:
            canonical = _money(item.get("amount"))

        server_item = dict(item)
        server_item["amount"] = canonical
        server_cart.append(server_item)
        total_requested += canonical

    return server_cart, round(total_requested, 2)

def _upsert_payment_session(reference: str, cart: List[Dict[str, Any]], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    for item in (cart or []):
        phone = str((item or {}).get("phone") or "").strip()
        if not phone:
            continue
        eligibility = evaluate_phone_order_eligibility(phone)
        if not eligibility.get("allowed"):
            raise ValueError(eligibility.get("message") or "Can't Place Order now, Please Contact Admin")

    server_cart, total_requested = _reprice_public_cart(cart)
    totals = _calc_public_paystack_totals(total_requested)
    phones = [str(it.get("phone") or "").strip() for it in cart if str(it.get("phone") or "").strip()]
    now = datetime.utcnow()
    payment_sessions_col.update_one(
        {"reference": reference},
        {
            "$set": {
                "cart_snapshot": cart,
                "server_cart_snapshot": server_cart,
                "customer_phone": phones[0] if phones else "",
                "phones": phones,
                "total_expected": totals["base_total"],
                "paystack_total_expected": totals["paystack_total"],
                "fee_expected": totals["fee"],
                "public_payload": payload or {},
                "updated_at": now,
            },
            "$setOnInsert": {
                "status": "initialized",
                "order_id": None,
                "processing_notes": [],
                "created_at": now,
            },
        },
        upsert=True,
    )
    return payment_sessions_col.find_one({"reference": reference}) or {}

def _ensure_public_transaction(reference: str, paid_ghs: float, total_expected: float, verify_data: Dict[str, Any]) -> None:
    existing = transactions_col.find_one({"reference": reference, "source": "paystack_inline", "status": "success"})
    if existing:
        return
    transactions_col.insert_one(
        {
            "user_id": None,
            "amount": round(paid_ghs, 2),
            "reference": reference,
            "status": "success",
            "type": "debit",
            "source": "paystack_inline",
            "gateway": "Paystack",
            "currency": verify_data.get("currency"),
            "channel": verify_data.get("channel"),
            "verified_at": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "raw": verify_data,
            "meta": {
                "public_checkout": True,
                "expected_pay_total_ghs": total_expected,
                "paid_total_ghs": paid_ghs,
            },
        }
    )

def _build_public_receipt(order_doc: Dict[str, Any], verify_data: Optional[Dict[str, Any]] = None, session_doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    items = order_doc.get("items") or []
    service_summary = []
    phones = []
    for item in items:
        phone = str(item.get("phone") or "").strip()
        service_summary.append(
            {
                "service": str(item.get("serviceName") or "Service").strip(),
                "offer": str(item.get("value") or "").strip(),
                "phone": phone,
                "amount": round(float(item.get("amount") or 0.0), 2),
            }
        )
        if phone:
            phones.append(phone)

    payment_amount = None
    payment_reference = order_doc.get("paystack_reference")
    if verify_data:
        try:
            payment_amount = round((float(verify_data.get("amount") or 0.0) / 100.0), 2)
        except Exception:
            payment_amount = None
        payment_reference = verify_data.get("reference") or payment_reference
    if payment_amount is None and session_doc:
        payment_amount = round(float(session_doc.get("paystack_total_expected") or 0.0), 2)
    if payment_amount is None:
        payment_amount = round(float(order_doc.get("charged_amount") or order_doc.get("total_amount") or 0.0), 2)

    return {
        "order_id": order_doc.get("order_id"),
        "payment_reference": payment_reference,
        "amount": payment_amount,
        "service_summary": service_summary,
        "phones": phones,
        "created_at": order_doc.get("created_at").isoformat() if order_doc.get("created_at") else "",
        "current_order_status": order_doc.get("status") or "pending",
        "receipt_url": f"/invoice/{order_doc.get('order_id')}" if order_doc.get("order_id") else "",
        "status_url": f"/check-status?phone={phones[0]}" if phones else "/check-status",
    }

def _json_safe_public_result(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        safe = {}
        for k, v in value.items():
            if k == "order_doc":
                continue
            safe[k] = _json_safe_public_result(v)
        return safe
    if isinstance(value, list):
        return [_json_safe_public_result(v) for v in value]
    return value

def _create_public_order_from_verified_payment(
    reference: str,
    verify_data: Dict[str, Any],
    server_cart: List[Dict[str, Any]],
    total_requested: float,
) -> Dict[str, Any]:
    order_id = generate_order_id()
    results: List[Dict[str, Any]] = []
    debug_events: List[Dict[str, Any]] = []
    total_processing_amount = 0.0
    seen_keys = set()

    for idx, item in enumerate(server_cart, start=1):
        phone = (item.get("phone") or "").strip()
        service_id_raw = item.get("serviceId")
        svc_name = item.get("serviceName") or ""
        amt_total = _money(item.get("amount"))
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

        svc_doc = None
        svc_type = None
        if service_id_raw:
            try:
                svc_doc = services_col.find_one({"_id": ObjectId(service_id_raw)})
                if svc_doc:
                    svc_type = svc_doc.get("type")
                    svc_name = svc_doc.get("name") or svc_name
            except Exception:
                svc_doc = None
                svc_type = None

        is_unavail, reason_text = _service_unavailability_reason(svc_doc)
        if is_unavail:
            raise ValueError(reason_text)

        system_base_amount = None
        if svc_doc:
            system_base_amount = _canonical_system_base_for_offer(
                svc_doc,
                value_obj,
                item.get("value"),
            )
        if system_base_amount is None:
            system_base_amount = amt_total

        profit_amount = max(0.0, round(float(amt_total) - float(system_base_amount), 2))
        profit_percent_used = round((profit_amount / float(system_base_amount)) * 100.0, 2) if float(system_base_amount) > 0 else 0.0

        network_id = _resolve_network_id(item, value_obj, svc_doc) if svc_doc else None
        bundle_key = _build_bundle_key(value_obj, item)
        amount_key = round(float(amt_total), 2)

        if phone and (network_id is not None) and (bundle_key is not None):
            cart_key = (phone, int(network_id), str(bundle_key), amount_key)
            if cart_key in seen_keys:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None,
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_in_cart",
                        "api_status": "skipped",
                        "api_response": {"note": "Duplicate line in this cart."},
                    }
                )
                continue
            seen_keys.add(cart_key)

        is_dup_strict = _has_processing_conflict_strict(
            phone, service_id_raw, svc_name, network_id, bundle_key, amount_key
        )
        if is_dup_strict:
            results.append(
                {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "skipped_duplicate_processing",
                    "api_status": "skipped",
                    "api_response": {"note": "Same line already processing; skipping."},
                }
            )
            continue

        resolved_network = _resolve_network_slug(svc_doc, item)
        selected_provider = _service_provider_name(svc_doc, item) or "manual"
        api_allowed = _service_uses_external_provider(
            svc_doc=svc_doc,
            item=item,
            svc_type=svc_type,
            service_id_raw=service_id_raw,
            phone=phone,
            base_amount=amt_total,
        )

        jlog(
            "checkout_line_routing",
            order_id=order_id,
            serviceName=svc_name,
            serviceId=service_id_raw,
            resolved_network=resolved_network,
            api_allowed=api_allowed,
            selected_provider=selected_provider,
        )

        provider_result = _dispatch_service_order(
            order_id=order_id,
            idx=idx,
            item=item,
            svc_doc=svc_doc,
            svc_type=svc_type,
            service_id_raw=service_id_raw,
            phone=phone,
            base_amount=system_base_amount,
            resolved_network=resolved_network,
        )

        total_processing_amount += amt_total
        results.append(
            {
                "phone": phone,
                "base_amount": system_base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key,
                **provider_result,
            }
        )

    for it in (results or []):
        if not it.get("line_status"):
            it["line_status"] = "pending"
        if isinstance(it.get("value"), (dict, list)):
            it["value"] = ""
        if not it.get("value"):
            vo = it.get("value_obj") or {}
            vol = vo.get("volume")
            if isinstance(vol, (int, float)) and vol > 0:
                it["value"] = f"{(vol / 1000):g}GB"
            else:
                it["value"] = "N/A"

    order_doc = {
        "user_id": None,
        "order_id": order_id,
        "items": results,
        "total_amount": total_requested,
        "charged_amount": round(total_processing_amount, 2),
        "profit_amount_total": round(sum(_money(it.get("profit_amount")) for it in results), 2),
        "status": "pending",
        "paid_from": "public_paystack",
        "paystack_reference": reference,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "debug": {"events": debug_events},
    }

    try:
        orders_col.insert_one(order_doc)
    except DuplicateKeyError:
        existing = _find_public_order_by_reference(reference)
        if existing:
            return {
                "success": True,
                "status": "already_created",
                "order_id": existing.get("order_id"),
                "message": "Order already placed",
                "receipt": _build_public_receipt(existing, verify_data=verify_data),
                "order_doc": existing,
            }
        raise

    return {
        "success": True,
        "status": "completed",
        "order_id": order_id,
        "message": f"Order received and is processing. Order ID: {order_id}",
        "receipt": _build_public_receipt(order_doc, verify_data=verify_data),
        "order_doc": order_doc,
    }

def finalize_paid_order(reference: str, payload_or_session_data: Optional[Dict[str, Any]] = None, source: str = "normal") -> Dict[str, Any]:
    reference = (reference or "").strip()
    if not reference:
        return {"success": False, "status": "failed", "message": "Payment reference is required."}

    session_doc = _load_payment_session(reference)
    incoming_cart = (payload_or_session_data or {}).get("cart") if isinstance(payload_or_session_data, dict) else None
    if isinstance(incoming_cart, list) and incoming_cart:
        session_doc = _upsert_payment_session(reference, incoming_cart, payload_or_session_data)

    existing_order = _find_public_order_by_reference(reference)
    if existing_order:
        payment_sessions_col.update_one(
            {"reference": reference},
            {
                "$set": {
                    "status": existing_order.get("status") or "processing",
                    "order_id": existing_order.get("order_id"),
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )
        return {
            "success": True,
            "status": "already_created",
            "order_id": existing_order.get("order_id"),
            "message": "Order already placed",
            "receipt": _build_public_receipt(existing_order, session_doc=session_doc),
        }

    ok, verify_data, fail_reason = _verify_paystack(reference)
    if not ok:
        payment_sessions_col.update_one(
            {"reference": reference},
            {"$set": {"status": "verification_failed_recoverable", "last_error": fail_reason, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        return {
            "success": False,
            "status": "verification_failed_recoverable",
            "message": fail_reason or "Payment verification is pending.",
            "next_action": "retry_or_check_status",
        }

    paid_pes = int(verify_data.get("amount") or 0)
    paid_ghs = round(paid_pes / 100.0, 2)
    currency = (verify_data.get("currency") or "GHS").upper()
    if paid_pes <= 0 or currency != "GHS":
        return {"success": False, "status": "failed", "message": "Invalid payment amount/currency."}

    if not session_doc:
        return {
            "success": False,
            "status": "recoverable_failure",
            "message": "Payment was received, but the checkout recovery record is missing.",
            "next_action": "check_status",
        }

    total_expected = round(float(session_doc.get("total_expected") or 0.0), 2)
    paystack_total_expected = round(float(session_doc.get("paystack_total_expected") or 0.0), 2)
    if paystack_total_expected <= 0:
        return {"success": False, "status": "failed", "message": "Saved checkout amount is invalid."}
    if not _paid_enough(paid_pes, int(round(paystack_total_expected * 100))):
        return {"success": False, "status": "failed", "message": "Payment amount is less than required."}

    server_cart = session_doc.get("server_cart_snapshot") or []
    if not server_cart:
        raw_cart = session_doc.get("cart_snapshot") or []
        server_cart, total_expected = _reprice_public_cart(raw_cart)
        totals = _calc_public_paystack_totals(total_expected)
        payment_sessions_col.update_one(
            {"reference": reference},
            {
                "$set": {
                    "server_cart_snapshot": server_cart,
                    "total_expected": totals["base_total"],
                    "paystack_total_expected": totals["paystack_total"],
                    "fee_expected": totals["fee"],
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    _ensure_public_transaction(reference, paid_ghs, total_expected, verify_data)
    payment_sessions_col.update_one(
        {"reference": reference},
        {
            "$set": {
                "status": "processing",
                "payment_verified": True,
                "verify_payload": {
                    "status": verify_data.get("status"),
                    "amount": verify_data.get("amount"),
                    "currency": verify_data.get("currency"),
                    "channel": verify_data.get("channel"),
                },
                "updated_at": datetime.utcnow(),
            }
        },
    )
    _append_processing_note(reference, f"Finalize attempt from {source}")

    try:
        created = _create_public_order_from_verified_payment(reference, verify_data, server_cart, total_expected)
    except ValueError as exc:
        payment_sessions_col.update_one(
            {"reference": reference},
            {"$set": {"status": "recoverable_failure", "last_error": str(exc), "updated_at": datetime.utcnow()}},
        )
        return {
            "success": False,
            "status": "recoverable_failure",
            "message": str(exc),
            "next_action": "retry_or_check_status",
        }
    except Exception as exc:
        payment_sessions_col.update_one(
            {"reference": reference},
            {"$set": {"status": "recoverable_failure", "last_error": str(exc), "updated_at": datetime.utcnow()}},
        )
        return {
            "success": False,
            "status": "recoverable_failure",
            "message": "Payment was received, but order processing did not finish. Do not pay again.",
            "next_action": "retry_or_check_status",
        }

    payment_sessions_col.update_one(
        {"reference": reference},
        {
            "$set": {
                "status": created.get("status") or "completed",
                "order_id": created.get("order_id"),
                "payment_verified": True,
                "updated_at": datetime.utcnow(),
                "finalized_at": datetime.utcnow(),
            }
        },
    )
    return created

def _public_status_by_reference(reference: str) -> Dict[str, Any]:
    reference = (reference or "").strip()
    if not reference:
        return {
            "payment_found": False,
            "payment_verified": False,
            "order_exists": False,
            "status": "failed",
            "message": "Payment reference is required.",
            "next_action": "none",
        }

    session_doc = _load_payment_session(reference)
    order_doc = _find_public_order_by_reference(reference)
    ok, verify_data, fail_reason = _verify_paystack(reference)

    if order_doc:
        order_status = (order_doc.get("status") or "").lower()
        return {
            "payment_found": True,
            "payment_verified": ok,
            "order_exists": True,
            "order_id": order_doc.get("order_id"),
            "status": "processing" if order_status in {"pending", "processing"} else "already_created",
            "message": "Order already placed",
            "next_action": "view_receipt",
            "receipt": _build_public_receipt(order_doc, verify_data=verify_data if ok else None, session_doc=session_doc),
        }

    if ok:
        return {
            "payment_found": True,
            "payment_verified": True,
            "order_exists": False,
            "order_id": (session_doc or {}).get("order_id"),
            "status": "pending",
            "message": "Payment found. You can safely reprocess this order.",
            "next_action": "reprocess",
        }

    return {
        "payment_found": bool(session_doc),
        "payment_verified": False,
        "order_exists": False,
        "order_id": (session_doc or {}).get("order_id"),
        "status": "pending" if session_doc else "recoverable_failure",
        "message": fail_reason or "Payment could not be confirmed yet.",
        "next_action": "retry_or_check_status",
    }

# ------------------ routes ------------------

@index_bp.route("/", methods=["GET"])
def landing():
    """
    Simple public landing:
    - Loads services for display.
    - Public checkout is handled by /public-checkout.
    """
    # hansmart.store should not serve the landing page without a slug
    if _host_only(request.host) in _STORE_HOSTS:
        abort(404, description="Store not found")
    try:
        services, _ = load_services_for_landing()
    except Exception:
        services = []
    paystack_keys = get_paystack_keys()

    return render_template(
        "index.html",
        services=services,
        paystack_pk=paystack_keys.get("public_key", ""),
    )

@index_bp.route("/public-payment-session", methods=["POST"])
def public_payment_session():
    data = request.get_json(silent=True) or {}
    cart = data.get("cart") or []
    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify({"success": False, "message": "Payment reference is required"}), 400
    if not cart or not isinstance(cart, list):
        return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

    try:
        session_doc = _upsert_payment_session(reference, cart, data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    return jsonify(
        {
            "success": True,
            "status": "initialized",
            "reference": reference,
            "total_expected": session_doc.get("total_expected"),
            "paystack_total_expected": session_doc.get("paystack_total_expected"),
        }
    ), 200

@index_bp.route("/public-reprocess-order", methods=["POST"])
def public_reprocess_order():
    data = request.get_json(silent=True) or {}
    reference = (data.get("reference") or "").strip()
    result = finalize_paid_order(reference, payload_or_session_data=data, source="reprocess")
    status_code = 200 if result.get("success") or result.get("status") in {"verification_failed_recoverable", "recoverable_failure", "pending"} else 400
    return jsonify(_json_safe_public_result(result)), status_code

@index_bp.route("/public-order-status-by-ref", methods=["GET"])
def public_order_status_by_ref():
    reference = (request.args.get("reference") or "").strip()
    result = _public_status_by_reference(reference)
    return jsonify(_json_safe_public_result(result)), 200


@index_bp.route("/public-checkout", methods=["POST"])
def public_checkout():
    data = request.get_json(silent=True) or {}
    cart = data.get("cart") or []
    ps_info = data.get("paystack") or {}
    ps_ref = (ps_info.get("reference") or "").strip()

    if not cart or not isinstance(cart, list):
        return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400
    if not ps_ref:
        return jsonify({"success": False, "message": "Payment reference is required"}), 400
    try:
        _upsert_payment_session(ps_ref, cart, data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    result = finalize_paid_order(ps_ref, payload_or_session_data=data, source="normal")
    status_code = 200 if result.get("success") or result.get("status") in {"verification_failed_recoverable", "recoverable_failure", "pending"} else 400
    return jsonify(_json_safe_public_result(result)), status_code
