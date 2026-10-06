from flask import Blueprint, request, jsonify, session, render_template, abort
from bson import ObjectId
from datetime import datetime, timedelta
import random, traceback, json, ast, re, os, uuid
import requests

from db import db
from phone_number_guard import evaluate_phone_order_eligibility

checkout_bp = Blueprint("checkout", __name__)

# MongoDB Collections
balances_col        = db["balances"]
orders_col          = db["orders"]
transactions_col    = db["transactions"]
services_col        = db["services"]
service_profits_col = db["service_profits"]  # per-customer overrides
users_col           = db["users"]  # ✅ for invoice view
blocked_phone_numbers_col = db["blocked_phone_numbers"]
carts_col           = db["carts"]

# Network ID fallback (internal use)
NETWORK_ID_FALLBACK = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}
EXTERNAL_PROVIDER_NAMES = {"skplug", "dataconnect"}
AFA_TALKTIME_SERVICE_ID = "689c5b6c9b03dc7fd3b6094b"
EXTERNAL_PROVIDER_SERVICE_IDS = {
    AFA_TALKTIME_SERVICE_ID,
    "6a299f7472e6d9d109a67ad8",
}
EXTERNAL_PROVIDER_SERVICE_NAMES = {
    "afa talktime",
    "mtn mashup data",
}
_VOL_GB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*G(?:B|IG)?\b", re.IGNORECASE)
_VOL_MB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*MB\b", re.IGNORECASE)

# ===== Tiny JSON logger =======================================================
def jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")


# ===== Helpers ================================================================
def generate_order_id():
    return f"SPD_{random.randint(0, 999_999):06d}"


def _money(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _clear_customer_cart(user_id):
    try:
        carts_col.update_one(
            {"user_id": user_id},
            {"$set": {"items": [], "updated_at": datetime.utcnow()}},
            upsert=True,
        )
    except Exception:
        pass


def _to_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def _coerce_value_obj(v):
    """
    Accepts dict, JSON string, or python-dict-like string.
    Returns a dict (possibly empty).
    """
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    s = str(v).strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:
            try:
                d = ast.literal_eval(s)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
    return {}


def _normalize_phone_for_blocklist(phone: str) -> str:
    """
    Normalize Ghana phone numbers to local format for blocklist lookups.
    Examples:
      233549869925 -> 0549869925
      +233549869925 -> 0549869925
      0549869925 -> 0549869925
    """
    digits = re.sub(r"\D+", "", str(phone or ""))
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    if digits.startswith("0") and len(digits) == 10:
        return digits
    return digits


def _service_provider_name(svc_doc: dict | None, item: dict | None = None) -> str | None:
    raw = None
    if svc_doc:
        raw = svc_doc.get("provider")
    if not raw and item:
        raw = item.get("provider")
    value = str(raw or "").strip().lower()
    return value or None


def _service_measure_unit(
    svc_doc: dict | None,
    item: dict | None = None,
    service_id_raw: str | None = None,
) -> str:
    sid = str(service_id_raw or (svc_doc or {}).get("_id") or (item or {}).get("serviceId") or "").strip()
    name = str((svc_doc or {}).get("name") or (item or {}).get("serviceName") or "").strip().lower()
    unit = str((svc_doc or {}).get("unit") or (item or {}).get("unit") or "").strip().lower()
    if sid == AFA_TALKTIME_SERVICE_ID or name == "afa talktime" or unit in {"min", "mins", "minute", "minutes"}:
        return "minutes"
    return "data"


def _format_external_size(value_obj: dict | None, raw_value: object, unit: str = "data") -> str | None:
    volume = None
    if isinstance(value_obj, dict):
        volume = _to_float(value_obj.get("volume"))
    if volume is not None and volume > 0:
        if unit == "minutes":
            return f"{int(round(volume))} mins" if abs(volume - round(volume)) < 1e-9 else f"{volume:g} mins"
        if volume >= 1000:
            gb = volume / 1000.0
            return f"{int(gb)}GB" if abs(gb - round(gb)) < 1e-9 else f"{gb:.2f}GB"
        if abs(volume - round(volume)) < 1e-9:
            return f"{int(round(volume))}MB"
        return f"{volume:.2f}MB"

    raw = str(raw_value or "").strip()
    if not raw:
        return None

    if unit == "minutes":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|mins|minute|minutes)\b", raw, re.IGNORECASE)
        if m:
            return f"{float(m.group(1)):g} mins"
    else:
        m = _VOL_GB_RE.search(raw)
        if m:
            gb = float(m.group(1))
            return f"{int(gb)}GB" if abs(gb - round(gb)) < 1e-9 else f"{gb:.2f}GB"

        m = _VOL_MB_RE.search(raw)
        if m:
            mb = float(m.group(1))
            return f"{int(mb)}MB" if abs(mb - round(mb)) < 1e-9 else f"{mb:.2f}MB"

    return raw or None


def _extract_remote_reference(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    for key in ("id", "order_id", "orderId", "reference", "client_reference", "clientReference"):
        value = payload.get(key)
        if value not in (None, "", []):
            return str(value)

    for block_key in ("data", "payload", "order", "result"):
        block = payload.get(block_key)
        if not isinstance(block, dict):
            continue
        for key in ("id", "order_id", "orderId", "reference", "client_reference", "clientReference"):
            value = block.get(key)
            if value not in (None, "", []):
                return str(value)

    return None


def _build_external_client_reference(order_id: str, idx: int) -> str:
    return f"SL-{order_id}-{idx}-{uuid.uuid4().hex[:8].upper()}"


def _external_api_config() -> tuple[str, str, int]:
    base_url = (os.getenv("CAMPUS_DATA_BASE_URL") or "https://campus-data-guce.onrender.com").strip().rstrip("/")
    api_key = (os.getenv("CAMPUS_DATA_API_KEY") or "campapi_7zp-YCnATNWD6Q0HcZzvV6osrrb1sbo9Tk5Ki0QG-PM").strip()
    timeout = int((os.getenv("CAMPUS_DATA_TIMEOUT") or "30").strip() or "30")
    return base_url, api_key, timeout


def _service_uses_external_provider(
    *,
    svc_doc: dict | None,
    item: dict | None,
    svc_type: str | None,
    service_id_raw: str | None,
    phone: str,
    base_amount: float,
) -> bool:
    provider = _service_provider_name(svc_doc, item)
    if provider not in EXTERNAL_PROVIDER_NAMES or not phone or base_amount <= 0 or not service_id_raw:
        return False

    sid = str(service_id_raw).strip()
    if sid in EXTERNAL_PROVIDER_SERVICE_IDS:
        return True

    name = str((svc_doc or {}).get("name") or (item or {}).get("serviceName") or "").strip().lower()
    if name in EXTERNAL_PROVIDER_SERVICE_NAMES:
        return True

    svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
    return svc_type_flag == "API"


def _submit_external_data_order(
    *,
    service_id: str,
    phone: str,
    size: str,
    base_amount: float,
    client_reference: str,
    order_id: str | None = None,
) -> dict:
    base_url, api_key, timeout = _external_api_config()
    payload = {
        "service_id": str(service_id),
        "phone": str(phone).strip(),
        "size": str(size).strip(),
        "base_amount": round(float(base_amount), 2),
        "client_reference": str(client_reference).strip(),
    }

    if not api_key:
        return {
            "ok": False,
            "http_status": 500,
            "request": payload,
            "response": {"message": "CAMPUS_DATA_API_KEY is not configured"},
        }

    url = f"{base_url}/api/external/orders"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    jlog(
        "external_order_request",
        order_id=order_id,
        service_id=service_id,
        client_reference=client_reference,
        phone=phone,
        size=size,
        base_amount=payload["base_amount"],
        url=url,
    )

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        text = resp.text or ""
        try:
            body = resp.json() if text.strip() else {}
        except Exception:
            body = {"raw": text} if text else {}

        result = {
            "ok": 200 <= resp.status_code < 300,
            "http_status": resp.status_code,
            "request": payload,
            "response": body,
        }
        jlog(
            "external_order_response",
            order_id=order_id,
            service_id=service_id,
            client_reference=client_reference,
            ok=result["ok"],
            http_status=resp.status_code,
            response=body,
        )
        return result
    except requests.RequestException as exc:
        jlog(
            "external_order_network_error",
            order_id=order_id,
            service_id=service_id,
            client_reference=client_reference,
            error=str(exc),
        )
        return {
            "ok": False,
            "http_status": 599,
            "request": payload,
            "response": {"message": str(exc)},
        }


def _dispatch_service_order(
    *,
    order_id: str,
    idx: int,
    item: dict,
    svc_doc: dict | None,
    svc_type: str | None,
    service_id_raw: str | None,
    phone: str,
    base_amount: float,
    resolved_network: str | None,
) -> dict:
    provider = _service_provider_name(svc_doc, item)
    svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
    wants_external = _service_uses_external_provider(
        svc_doc=svc_doc,
        item=item,
        svc_type=svc_type,
        service_id_raw=service_id_raw,
        phone=phone,
        base_amount=base_amount,
    )

    if not wants_external:
        return {
            "provider": "manual",
            "provider_network": resolved_network,
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": None,
            "line_status": "processing",
            "api_status": "manual_processing",
            "api_response": {
                "note": "Automatic provider ordering is disabled for this line; queued for manual processing.",
                "resolved_network": resolved_network,
                "service_type_flag": svc_type_flag,
                "provider": provider or "manual",
            },
        }

    measure_unit = _service_measure_unit(svc_doc, item, service_id_raw)
    size = _format_external_size(
        _coerce_value_obj(item.get("value_obj") or item.get("value")),
        item.get("value"),
        unit=measure_unit,
    )
    client_reference = _build_external_client_reference(order_id, idx)
    if not size:
        return {
            "provider": provider,
            "provider_network": resolved_network,
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": client_reference,
            "line_status": "pending",
            "api_status": "submit_failed",
            "api_response": {
                "note": "External API submission failed because the offer size could not be derived.",
                "resolved_network": resolved_network,
                "service_type_flag": svc_type_flag,
            },
        }

    submit_result = _submit_external_data_order(
        service_id=service_id_raw,
        phone=phone,
        size=size,
        base_amount=base_amount,
        client_reference=client_reference,
        order_id=order_id,
    )
    remote_ref = _extract_remote_reference(submit_result.get("response"))

    if submit_result.get("ok"):
        return {
            "provider": provider,
            "provider_network": resolved_network,
            "provider_reference": remote_ref,
            "provider_order_id": remote_ref,
            "provider_request_order_id": client_reference,
            "line_status": "processing",
            "api_status": "submitted",
            "api_response": {
                **submit_result,
                "resolved_network": resolved_network,
                "service_type_flag": svc_type_flag,
                "size": size,
            },
        }

    return {
        "provider": provider,
        "provider_network": resolved_network,
        "provider_reference": remote_ref,
        "provider_order_id": remote_ref,
        "provider_request_order_id": client_reference,
        "line_status": "pending",
        "api_status": "submit_failed",
        "api_response": {
            **submit_result,
            "note": "External API submission failed; follow-up may be required.",
            "resolved_network": resolved_network,
            "service_type_flag": svc_type_flag,
            "size": size,
        },
    }


# ===== Profit helpers (absolute profit amount) ================================
def _get_service_default_profit_percent(service_doc):
    return _to_float(service_doc.get("default_profit_percent"), 0.0) or 0.0


def _get_customer_profit_override_percent(service_id, customer_id_obj):
    ov = service_profits_col.find_one({"service_id": service_id, "customer_id": customer_id_obj})
    return _to_float(ov.get("profit_percent"), None) if ov else None


def _get_customer_offer_override_doc(service_id, customer_id_obj):
    ov = service_profits_col.find_one({"service_id": service_id, "customer_id": customer_id_obj})
    if ov:
        return ov
    return service_profits_col.find_one({"service_id": service_id, "customer_id": None})


def _effective_profit_percent(service_doc, customer_id_obj):
    override = _get_customer_profit_override_percent(service_doc["_id"], customer_id_obj)
    return override if override is not None else _get_service_default_profit_percent(service_doc)


def _find_service_offer_index(svc_doc, value_obj, raw_value):
    try:
        offers = svc_doc.get("offers") or []
        vid = (value_obj or {}).get("id")
        vvol = (value_obj or {}).get("volume")
        for idx, of in enumerate(offers):
            of_val = of.get("value")
            if isinstance(of_val, str) and of_val.strip().startswith("{") and of_val.strip().endswith("}"):
                try:
                    of_val = json.loads(of_val)
                except Exception:
                    try:
                        of_val = ast.literal_eval(of_val)
                    except Exception:
                        pass
            if isinstance(of_val, dict):
                if (vid is not None and of_val.get("id") == vid) or (vvol is not None and of_val.get("volume") == vvol):
                    return idx
            else:
                if raw_value is not None and of_val == raw_value:
                    return idx
    except Exception:
        pass
    return None


def _pick_offer_base_amount_from_service(svc_doc, value_obj, raw_value):
    """
    Try to recover the base (wholesale) amount from the selected offer in svc_doc.offers.
    """
    try:
        offers = svc_doc.get("offers") or []
        vid = (value_obj or {}).get("id")
        vvol = (value_obj or {}).get("volume")
        for of in offers:
            of_val = of.get("value")
            of_amt = _to_float(of.get("amount"))
            if isinstance(of_val, str) and of_val.strip().startswith("{") and of_val.strip().endswith("}"):
                try:
                    of_val = json.loads(of_val)
                except Exception:
                    try:
                        of_val = ast.literal_eval(of_val)
                    except Exception:
                        pass
            if isinstance(of_val, dict):
                if (vid is not None and of_val.get("id") == vid) or (vvol is not None and of_val.get("volume") == vvol):
                    return of_amt
            else:
                if raw_value is not None and of_val == raw_value:
                    return of_amt
    except Exception:
        pass
    return None


def _pick_customer_offer_total_from_service(svc_doc, customer_id_obj, value_obj, raw_value):
    if not svc_doc or not customer_id_obj:
        return None
    idx = _find_service_offer_index(svc_doc, value_obj, raw_value)
    if idx is None:
        return None
    override_doc = _get_customer_offer_override_doc(svc_doc["_id"], customer_id_obj)
    offers = (override_doc or {}).get("offers") or []
    for offer in offers:
        try:
            if int(offer.get("index")) != idx:
                continue
        except Exception:
            continue
        total = _to_float(offer.get("total"))
        if total is not None and total >= 0:
            return round(float(total), 2)
    return None


def _derive_base_profit(amount_total, base_amount_hint, eff_percent):
    a = _money(amount_total)
    if a <= 0:
        return 0.0, 0.0
    if base_amount_hint is not None and base_amount_hint > 0:
        base = float(base_amount_hint)
        profit = round(a - base, 2)
        if profit < 0:
            profit = 0.0
            base = a
        return round(base, 2), profit
    p = _to_float(eff_percent, 0.0) or 0.0
    try:
        base = round(a / (1.0 + (p / 100.0)), 2) if p > 0 else a
    except Exception:
        base = a
    profit = round(a - base, 2)
    if profit < 0:
        profit = 0.0
        base = a
    return base, profit


def _reprice_customer_cart(cart: list, customer_id_obj):
    repriced = []
    for item in cart:
        if not isinstance(item, dict):
            continue
        out = dict(item)
        value_obj = _coerce_value_obj(out.get("value_obj") or out.get("value"))
        service_id_raw = out.get("serviceId")
        svc_doc = None
        if service_id_raw:
            try:
                svc_doc = services_col.find_one({"_id": ObjectId(service_id_raw)}, {"offers": 1})
            except Exception:
                svc_doc = None
        effective_total = None
        if svc_doc:
            effective_total = _pick_customer_offer_total_from_service(
                svc_doc, customer_id_obj, value_obj, out.get("value")
            )
        if effective_total is not None:
            out["amount"] = effective_total
            out["total"] = effective_total
        repriced.append(out)
    return repriced


# ===== Field resolvers =======================================================
def _resolve_network_id(item: dict, value_obj: dict, svc_doc: dict | None):
    """
    Internal numeric network ID, used only for duplicate guards / reporting.
    Not sent to providers.
    """
    nid = (item or {}).get("network_id") or (value_obj or {}).get("network_id")
    if nid not in (None, "", []):
        try:
            return int(nid)
        except Exception:
            pass
    if svc_doc:
        try:
            if "network_id" in svc_doc and svc_doc["network_id"] not in (None, ""):
                return int(svc_doc["network_id"])
            guess = (svc_doc.get("name") or svc_doc.get("network") or "").strip().upper()
            if guess and guess in NETWORK_ID_FALLBACK:
                return int(NETWORK_ID_FALLBACK[guess])
        except Exception:
            pass
    if not svc_doc:
        name = (item.get("serviceName") or "").strip().upper()
        if name in NETWORK_ID_FALLBACK:
            return int(NETWORK_ID_FALLBACK[name])
    return None


def _resolve_network_slug(svc_doc: dict | None, item: dict) -> str | None:
    """
    Resolve generic 'network' slug we also reuse:
      - 'mtn'
      - 'telecel'
      - 'airteltigo'
    Used for routing.
    """
    doc = svc_doc

    # Fallback: look up by service name if svc_doc is missing
    if not doc:
        sname = (item.get("serviceName") or "").strip()
        if sname:
            try:
                doc = services_col.find_one(
                    {"name": sname},
                    {"service_network": 1, "network": 1, "name": 1},
                )
            except Exception:
                doc = None

    candidates = []
    if doc:
        candidates.append(doc.get("service_network"))
        candidates.append(doc.get("network"))
        candidates.append(doc.get("name"))

    candidates.append(item.get("network"))
    candidates.append(item.get("network_name"))
    candidates.append(item.get("serviceName"))

    joined = " ".join(str(c) for c in candidates if c).lower()

    if "mtn" in joined:
        return "mtn"

    # Telecel / Vodafone rebrand
    if "telecel" in joined or "vodafone" in joined:
        return "telecel"

    # AirtelTigo / AT / iShare
    if (
        "airteltigo" in joined
        or "airtel tigo" in joined
        or "airtel-tigo" in joined
        or "at - ishare" in joined
        or "i share" in joined
        or "ishare" in joined
    ):
        return "airteltigo"

    return None


def _build_bundle_key(value_obj: dict, item: dict):
    """
    Build a stable duplicate-detection key for a line item.
    Prefer offer/package IDs when present; otherwise fall back to volume.
    """
    value_obj = value_obj if isinstance(value_obj, dict) else {}

    for key in ("id", "package_id", "pkg_id", "bundle_id", "shared_bundle"):
        val = value_obj.get(key)
        if val not in (None, "", []):
            try:
                return ("offer", int(float(val)))
            except Exception:
                pass

    vol = value_obj.get("volume")
    if vol not in (None, "", []):
        try:
            return ("vol", int(float(vol)))
        except Exception:
            pass

    raw_val = str((item or {}).get("value") or "").strip()
    if raw_val:
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return ("vol", int(float(m.group(1)) * 1000))
            except Exception:
                pass
        m = re.search(r"(\d+(?:\.\d+)?)\s*mb", raw_val.lower())
        if m:
            try:
                return ("vol", int(float(m.group(1))))
            except Exception:
                pass

    return None


# ===== Unavailability checker ================================================
def _service_unavailability_reason(svc_doc: dict):
    """
    Returns (is_unavailable, reason_text)
    """
    if not svc_doc:
        return True, "Closed"

    status = (svc_doc.get("status") or "").strip().upper()
    availability = (svc_doc.get("availability") or "").strip().upper()

    if availability in {"OUT_OF_STOCK", "OUT OF STOCK", "OUTOFSTOCK"}:
        return True, "Out of stock"

    if status == "CLOSED":
        return True, "Closed"

    return False, ""


# ===== Duplicate-in-processing guard =========================================
DUP_WINDOW_MINUTES = 30


def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0


def _has_processing_conflict_strict(
    phone: str,
    service_id_raw: str | None,
    svc_name: str | None,
    network_id: int | None,
    bundle_key: tuple | None,
    amount_key: float,
) -> bool:
    if not phone or network_id is None or bundle_key is None:
        return False

    window_start = datetime.utcnow() - timedelta(minutes=DUP_WINDOW_MINUTES)
    kind, bval = bundle_key

    elem = {
        "phone": phone,
        "network_id": network_id,
        "bundle_key.kind": kind,
        "bundle_key.value": bval,
        "amount": amount_key,
    }
    if service_id_raw:
        elem["serviceId"] = service_id_raw

    q = {
        "status": {"$in": ["processing", "Pending", "pending"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": elem},
    }
    if orders_col.find_one(q, {"_id": 1}):
        return True

    alt = {
        "phone": phone,
        "network_id": network_id,
        "amount": amount_key,
    }
    if kind == "offer":
        alt["value_obj.id"] = bval
    else:
        alt["value_obj.volume"] = bval
    if service_id_raw:
        alt["serviceId"] = service_id_raw

    q2 = {
        "status": {"$in": ["processing", "Pending", "pending"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": alt},
    }
    return bool(orders_col.find_one(q2, {"_id": 1}))


@checkout_bp.route("/checkout", methods=["POST"])
def process_checkout():
    try:
        user_id = session.get("user_id")
        if not user_id:
            jlog("checkout_auth_fail", session_keys=list(session.keys()))
            return jsonify({"success": False, "message": "Login required"}), 401

        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            return jsonify({"success": False, "message": "Invalid user session"}), 401

        data = request.get_json(silent=True) or {}
        cart = data.get("cart") or []
        jlog("checkout_incoming", payload=data)

        if not isinstance(cart, list) or not cart:
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        cart = _reprice_customer_cart(cart, user_obj_id)

        balance_doc = balances_col.find_one({"user_id": user_obj_id}) or {}
        current_balance = round(_money(balance_doc.get("amount")), 2)
        total_requested = round(sum(_money((it or {}).get("amount")) for it in cart if isinstance(it, dict)), 2)
        order_id = generate_order_id()
        jlog("checkout_balance", order_id=order_id, balance=current_balance, total=total_requested)

        if total_requested <= 0:
            return jsonify({"success": False, "message": "Cart total is invalid"}), 400
        if current_balance < total_requested:
            return jsonify({"success": False, "message": "Insufficient wallet balance"}), 400

        results = []
        debug_events = []
        total_processing_amount = 0.0
        profit_amount_total = 0.0
        has_processing = False
        seen_keys = set()

        blocked_phone_map = {}
        try:
            cart_phone_keys = {
                _normalize_phone_for_blocklist((it.get("phone") or "").strip())
                for it in cart
                if isinstance(it, dict) and (it.get("phone") or "").strip()
            }
            cart_phone_keys.discard("")
            if cart_phone_keys:
                blocked_docs = blocked_phone_numbers_col.find(
                    {"is_active": True, "normalized_phone": {"$in": list(cart_phone_keys)}},
                    {"normalized_phone": 1, "reason": 1, "_id": 0},
                )
                blocked_phone_map = {
                    d.get("normalized_phone"): (d.get("reason") or "")
                    for d in blocked_docs
                    if d.get("normalized_phone")
                }
        except Exception:
            blocked_phone_map = {}

        for idx, item in enumerate(cart, start=1):
            if not isinstance(item, dict):
                continue

            phone = (item.get("phone") or "").strip()
            eligibility = evaluate_phone_order_eligibility(phone)
            if not eligibility.get("allowed"):
                return jsonify({"success": False, "message": eligibility.get("message") or "Can't Place Order now, Please Contact Admin"}), 400
            amt_total = round(_money(item.get("amount")), 2)
            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
            service_id_raw = item.get("serviceId")
            svc_name = item.get("serviceName") or None
            svc_doc = None
            svc_type = None

            if service_id_raw:
                try:
                    svc_doc = services_col.find_one({"_id": ObjectId(service_id_raw)})
                except Exception:
                    svc_doc = None

            if svc_doc:
                svc_name = svc_doc.get("name") or svc_name
                svc_type = svc_doc.get("type")
            else:
                svc_type = item.get("service_type")

            if svc_doc:
                unavailable, reason = _service_unavailability_reason(svc_doc)
                if unavailable:
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
                            "network_id": _resolve_network_id(item, value_obj, svc_doc),
                            "bundle_key": None,
                            "line_amount_key": _normalize_amount_key(amt_total),
                            "line_status": "skipped_unavailable",
                            "api_status": "not_applicable",
                            "api_response": {"note": reason},
                            "provider": "manual",
                            "provider_request_order_id": None,
                            "provider_reference": None,
                            "provider_order_id": None,
                        }
                    )
                    continue

            customer_profit_percent = _effective_profit_percent(svc_doc, user_obj_id) if svc_doc else 0.0
            base_hint = _to_float(item.get("base_amount"))
            if base_hint is None and svc_doc:
                base_hint = _pick_offer_base_amount_from_service(svc_doc, value_obj, item.get("value"))
            base_amount, profit_amount = _derive_base_profit(amt_total, base_hint, customer_profit_percent)
            profit_percent_used = round((profit_amount / base_amount) * 100.0, 2) if base_amount > 0 else 0.0
            profit_amount_total += profit_amount

            network_id = _resolve_network_id(item, value_obj, svc_doc)
            bundle_key = _build_bundle_key(value_obj, item)
            amount_key = _normalize_amount_key(amt_total)
            dedupe_key = (phone, network_id, bundle_key, amount_key, service_id_raw)
            if dedupe_key in seen_keys:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": profit_percent_used,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_in_cart",
                        "api_status": "skipped",
                        "api_response": {"note": "Duplicate line in cart; skipped."},
                        "provider": "manual",
                        "provider_request_order_id": None,
                        "provider_reference": None,
                        "provider_order_id": None,
                    }
                )
                continue
            seen_keys.add(dedupe_key)

            if _has_processing_conflict_strict(phone, service_id_raw, svc_name, network_id, bundle_key, amount_key):
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": profit_percent_used,
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
                        "api_response": {"note": "Same number + bundle already processing; skipping."},
                        "provider": "manual",
                        "provider_request_order_id": None,
                        "provider_reference": None,
                        "provider_order_id": None,
                    }
                )
                continue

            normalized_phone = _normalize_phone_for_blocklist(phone)
            if normalized_phone and normalized_phone in blocked_phone_map:
                debug_events.append(
                    {
                        "when": datetime.utcnow(),
                        "stage": "checkout_blocked_phone_manual",
                        "phone": normalized_phone,
                        "reason": blocked_phone_map.get(normalized_phone) or "",
                    }
                )

            resolved_network = _resolve_network_slug(svc_doc, item)
            selected_provider = _service_provider_name(svc_doc, item) or "manual"
            api_allowed = _service_uses_external_provider(
                svc_doc=svc_doc,
                item=item,
                svc_type=svc_type,
                service_id_raw=service_id_raw,
                phone=phone,
                base_amount=base_amount,
            )

            jlog(
                "checkout_line_routing",
                order_id=order_id,
                idx=idx,
                serviceId=service_id_raw,
                serviceName=svc_name,
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
                base_amount=base_amount,
                resolved_network=resolved_network,
            )

            has_processing = True
            total_processing_amount += amt_total
            results.append(
                {
                    "phone": phone,
                    "base_amount": base_amount,
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

        total_to_charge_now = round(total_processing_amount, 2)
        if total_to_charge_now <= 0 or not has_processing:
            return jsonify({"success": False, "message": "No valid order lines to process", "items": results}), 400

        balances_col.update_one(
            {"user_id": user_obj_id},
            {"$set": {"amount": round(current_balance - total_to_charge_now, 2), "updated_at": datetime.utcnow()}},
            upsert=True,
        )

        status = "pending"
        order_doc = {
            "user_id": user_obj_id,
            "order_id": order_id,
            "items": results,
            "total_amount": round(total_requested, 2),
            "charged_amount": total_to_charge_now,
            "profit_amount_total": round(profit_amount_total, 2),
            "status": status,
            "paid_from": "wallet",
            "source": "customer_dashboard",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "debug": {"events": debug_events[-10:]},
        }
        orders_col.insert_one(order_doc)

        transactions_col.insert_one(
            {
                "user_id": user_obj_id,
                "amount": total_to_charge_now,
                "reference": order_id,
                "status": "success",
                "type": "purchase",
                "gateway": "Wallet",
                "currency": "GHS",
                "created_at": datetime.utcnow(),
                "verified_at": datetime.utcnow(),
                "meta": {
                    "order_status": status,
                    "api_delivered_amount": 0.0,
                    "processing_amount": total_to_charge_now,
                    "profit_amount_total": round(profit_amount_total, 2),
                },
            }
        )

        _clear_customer_cart(user_obj_id)

        skipped_count = sum(
            1 for it in results if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )
        processing_count = sum(1 for it in results if it.get("line_status") == "processing")
        msg = f"Order received and is processing. We've charged your wallet. Order ID: {order_id}"

        return (
            jsonify(
                {
                    "success": True,
                    "message": msg,
                    "order_id": order_id,
                    "redirect_url": f"/invoice/{order_id}",
                    "status": status,
                    "charged_amount": total_to_charge_now,
                    "profit_amount_total": round(profit_amount_total, 2),
                    "processing_count": processing_count,
                    "skipped_count": skipped_count,
                    "items": results,
                }
            ),
            200,
        )

    except Exception:
        jlog("checkout_uncaught", error=traceback.format_exc())
        return jsonify({"success": False, "message": "Server error"}), 500


# ===== Invoice view (same blueprint) =========================================
@checkout_bp.route("/invoice/<order_id>")
def invoice_view(order_id):
    """
    Render a single invoice by internal order ID (e.g. NAN12345)
    Uses invoice.html template you already created.
    """
    order = orders_col.find_one({"order_id": order_id})
    if not order:
        abort(404)

    user = {}
    try:
        uid = order.get("user_id")
        if uid:
            user = users_col.find_one({"_id": uid}) or {}
    except Exception:
        user = {}

    customer_name = (
        user.get("name")
        or user.get("full_name")
        or user.get("username")
        or "Customer"
    )

    return render_template(
        "invoice.html",
        order=order,
        user=user,
        customer=customer_name,
    )
