# routes/store_page.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import os, json, re, ast, traceback, threading, uuid

import requests
from bson import ObjectId
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_file,
    abort,
)

from db import db
import gridfs
from paystack_config import get_paystack_keys, is_public_key, is_secret_key


# ---------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------
services_col = db["services"]
stores_col = db["stores"]
balances_col = db["balances"]
orders_col = db["orders"]
transactions_col = db["transactions"]
users_col = db["users"]
store_accounts_col = db["store_accounts"]

# ✅ PRIMARY: Store products collection used by /api/store-products/*
store_products_col = db["store_products"]

# ✅ Legacy products collection (optional fallback)
products_col = db.get_collection("products")

# --- GridFS bucket ---
fs = gridfs.GridFS(db)

stores_bp = Blueprint("stores", __name__)


# ---------------------------------------------------------------------
# Import helpers from checkout.py (keep compatibility)
# ---------------------------------------------------------------------
_checkout_helpers: Dict[str, Any] = {}
try:
    from checkout import (  # type: ignore
        _effective_profit_percent,
        _derive_base_profit,
        _coerce_value_obj,
        _to_float,
        _money,
        generate_order_id,
        _service_unavailability_reason,
        _resolve_network_slug,
        _dispatch_service_order,
        _service_provider_name,
        _service_uses_external_provider,
        EXTERNAL_PROVIDER_NAMES,
        jlog,
    )
    try:
        from checkout import _insert_transaction_doc_like_checkout  # type: ignore
        _checkout_helpers["txn_fn"] = _insert_transaction_doc_like_checkout
    except Exception:
        pass
    try:
        from checkout import _insert_order_doc_like_checkout  # type: ignore
        _checkout_helpers["order_fn"] = _insert_order_doc_like_checkout
    except Exception:
        pass
except Exception:  # pragma: no cover
    from .checkout import (  # type: ignore
        _effective_profit_percent,
        _derive_base_profit,
        _coerce_value_obj,
        _to_float,
        _money,
        generate_order_id,
        _service_unavailability_reason,
        _resolve_network_slug,
        _dispatch_service_order,
        _service_provider_name,
        _service_uses_external_provider,
        EXTERNAL_PROVIDER_NAMES,
        jlog,
    )
    try:
        from .checkout import _insert_transaction_doc_like_checkout  # type: ignore
        _checkout_helpers["txn_fn"] = _insert_transaction_doc_like_checkout
    except Exception:
        pass
    try:
        from .checkout import _insert_order_doc_like_checkout  # type: ignore
        _checkout_helpers["order_fn"] = _insert_order_doc_like_checkout
    except Exception:
        pass


# ---------------------------------------------------------------------
# Config (ENV)
# ---------------------------------------------------------------------
TARGET_STORE_HOST: str = (os.getenv("STORE_PUBLIC_HOST", "") or "").strip()
STORE_PATH_PREFIXES: Tuple[str, ...] = ("/s/",)

# Canonical store host enforcement (public store + store tools).
# If STORE_PUBLIC_HOST is unset, use the current app host instead of redirecting.
_CANONICAL_STORE_HOST = TARGET_STORE_HOST.strip().lower()
_DEV_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}

def _host_only(v: str) -> str:
    return (v or "").split(":", 1)[0].strip().lower()

def _store_host_redirect():
    if not _CANONICAL_STORE_HOST:
        return None
    host = _host_only(request.host)
    if not host or host in _DEV_HOSTS or host.endswith(".local"):
        return None
    if host != _host_only(_CANONICAL_STORE_HOST):
        q = request.query_string.decode("utf-8")
        url = f"https://{_CANONICAL_STORE_HOST}{request.path}" + (f"?{q}" if q else "")
        return redirect(url, code=301)
    return None

NETWORK_ID_FALLBACK: Dict[str, int] = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}


# ---------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------
def _norm(s: str) -> str:
    return (s or "").strip().lower()

PREFERRED_ORDER: List[str] = ["MTN NORMAL", "MTN", "MTN MASHUP DATA", "AT - iShare", "AT - BigTime", "AFA TALKTIME"]

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

def _slugify(s: str) -> str:
    s2 = (s or "").lower().strip()
    s2 = re.sub(r"[^a-z0-9]+", "-", s2).strip("-")
    return s2 or "store"

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()
    availability = (svc.get("availability") or "AVAILABLE").upper()
    closed_msg = svc.get("closed_message") or "This service is temporarily closed."
    oos_msg = svc.get("out_of_stock_message") or "This service is currently out of stock."
    can_order = t in {"API", "OFF", "MANUAL"} and status == "OPEN" and availability == "AVAILABLE"
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
        "can_order": can_order,
        "disabled_reason": disabled_reason,
    }

def _sorted_services(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def prio_tuple(s: Dict[str, Any]) -> Tuple[float, float, float, str]:
        prio = _to_float(s.get("priority")) or float("inf")
        nrank = _name_rank(s.get("name") or "")
        nrank = nrank if nrank is not None else 10_000
        display_order = _to_float(s.get("display_order")) or float("inf")
        created = s.get("created_at")
        ts = 0.0
        if isinstance(created, datetime):
            ts = -created.timestamp()
        else:
            try:
                v = float(created)
                ts = -(v / 1000.0 if v > 1e12 else v)
            except Exception:
                ts = 0.0
        alpha = _norm(s.get("name") or "")
        return (prio, nrank, display_order, ts, alpha)

    raw.sort(key=prio_tuple)
    return raw


# ---------------------------------------------------------------------
# ✅ WhatsApp helpers
# ---------------------------------------------------------------------
def _wa_digits(v: Any) -> str:
    d = re.sub(r"\D+", "", str(v or ""))
    if d.startswith("0") and len(d) == 10:
        return "233" + d[1:]
    if d.startswith("233") and len(d) == 12:
        return d
    return d

def _wa_link_from_number(raw: Any, text: str = "") -> str:
    d = _wa_digits(raw)
    if not d:
        return ""
    msg = (text or "").strip()
    if msg:
        try:
            from urllib.parse import quote
            return f"https://wa.me/{d}?text={quote(msg)}"
        except Exception:
            return f"https://wa.me/{d}"
    return f"https://wa.me/{d}"

def _extract_store_whatsapp(store_doc: Dict[str, Any]) -> Dict[str, str]:
    def pick(*paths) -> Any:
        for p in paths:
            cur = store_doc
            ok = True
            for key in p:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur.get(key)
            if ok and cur not in (None, "", [], {}):
                return cur
        return ""

    wa_number = pick(
        ("whatsapp_number",),
        ("contact", "whatsapp_number"),
        ("hero", "whatsapp_number"),
        ("theme", "whatsapp_number"),
        ("whatsapp", "number"),
    )
    wa_group = pick(
        ("whatsapp_group",),
        ("contact", "whatsapp_group"),
        ("hero", "whatsapp_group"),
        ("theme", "whatsapp_group"),
        ("whatsapp", "group"),
        ("whatsapp_group_link",),
        ("contact", "whatsapp_group_link"),
    )

    wa_number_str = str(wa_number or "").strip()
    wa_group_str = str(wa_group or "").strip()

    return {
        "number_raw": wa_number_str,
        "number_digits": _wa_digits(wa_number_str),
        "number_link": _wa_link_from_number(
            wa_number_str, f"Hello {store_doc.get('name','')}, I want to order."
        ),
        "group_link": wa_group_str,
    }


# =====================================================================
# ✅ Offers source:
# - Page pricing: store_offers authoritative, fallback to offers
# =====================================================================
def _svc_offers_list(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    so = svc.get("store_offers")
    if isinstance(so, list) and so:
        return so
    off = svc.get("offers")
    if isinstance(off, list) and off:
        return off
    return []

def _offer_base_amount(of: Dict[str, Any]) -> Optional[float]:
    if not isinstance(of, dict):
        return None
    v = of.get("store_amount")
    base = _to_float(v)
    if base is not None:
        return base
    return _to_float(of.get("amount"))


# =====================================================================
# ✅ NEW PROFIT RULE HELPERS (PRO, SAFE)
# =====================================================================
def _effective_store_profit_percent(svc_doc: Optional[Dict[str, Any]]) -> float:
    """
    Store checkout profit percent.
    Priority:
      1) svc_doc.store_offers_profit
      2) svc_doc.default_profit_percent
      3) 0.0
    """
    if not svc_doc:
        return 0.0
    try:
        v = svc_doc.get("store_offers_profit")
        if v is not None and str(v).strip() != "":
            return float(v)
    except Exception:
        pass
    try:
        v2 = svc_doc.get("default_profit_percent")
        if v2 is not None and str(v2).strip() != "":
            return float(v2)
    except Exception:
        pass
    return 0.0


# ✅ UPDATED: products loader (NOW loads from store_products_col first)
def _load_store_products(store_doc: Dict[str, Any], wa_number_raw: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def _safe_float(v: Any) -> float:
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return 0.0

    def _safe_int(v: Any) -> int:
        try:
            return int(float(str(v).replace(",", "").strip()))
        except Exception:
            return 0

    def _pick_img(p: Dict[str, Any]) -> str:
        return (
            (p.get("image_url") or p.get("image") or p.get("img") or p.get("photo") or "")
            if isinstance(p, dict)
            else ""
        )

    def _pick_name(p: Dict[str, Any]) -> str:
        return (p.get("name") or p.get("title") or p.get("product_name") or "Product").strip()

    def _pick_desc(p: Dict[str, Any]) -> str:
        return (p.get("description") or p.get("desc") or "").strip()

    def _pick_price(p: Dict[str, Any]) -> float:
        for k in ("price", "amount", "selling_price", "unit_price"):
            if k in p and p.get(k) not in (None, ""):
                return _safe_float(p.get(k))
        return 0.0

    def _pick_qty(p: Dict[str, Any]) -> int:
        for k in ("quantity", "qty", "stock"):
            if k in p and p.get(k) not in (None, ""):
                return _safe_int(p.get(k))
        return 0

    def _product_order_link(pname: str, price: float) -> str:
        msg = f"Hello {store_doc.get('name','')}, I want to order: {pname}"
        if price and price > 0:
            msg += f" (GHS {price:.2f})"
        msg += "."
        return _wa_link_from_number(wa_number_raw, msg)

    slug = store_doc.get("slug")
    owner_id = store_doc.get("owner_id")
    store_id = store_doc.get("_id")

    # 1) ✅ MAIN: store_products collection
    try:
        q_candidates: List[Dict[str, Any]] = []
        if slug:
            q_candidates.append({"store_slug": slug, "status": {"$ne": "deleted"}})
        if store_id:
            q_candidates.append({"store_id": store_id, "status": {"$ne": "deleted"}})
            q_candidates.append({"store_id": str(store_id), "status": {"$ne": "deleted"}})
        if owner_id:
            q_candidates.append({"owner_id": owner_id, "status": {"$ne": "deleted"}})
            q_candidates.append({"owner_id": str(owner_id), "status": {"$ne": "deleted"}})

        fields = {
            "_id": 1,
            "store_slug": 1,
            "store_id": 1,
            "owner_id": 1,
            "manager_id": 1,
            "name": 1,
            "description": 1,
            "image_url": 1,
            "price": 1,
            "quantity": 1,
            "status": 1,
            "created_at": 1,
            "updated_at": 1,
        }

        found: List[Dict[str, Any]] = []
        for q in q_candidates:
            try:
                if store_products_col.count_documents(q, limit=1) > 0:
                    found = list(store_products_col.find(q, fields).sort("created_at", -1))
                    break
            except Exception:
                continue

        if found:
            for p in found:
                pname = _pick_name(p)
                price = _pick_price(p)
                out.append(
                    {
                        "_id_str": str(p.get("_id") or ""),
                        "name": pname,
                        "description": _pick_desc(p),
                        "image_url": _pick_img(p),
                        "price": round(price, 2),
                        "quantity": _pick_qty(p),
                        "created_at": p.get("created_at") or None,
                        "order_link": _product_order_link(pname, price) if wa_number_raw else "",
                    }
                )
            return out
    except Exception:
        pass

    # 2) embedded on store doc (if any)
    embedded = store_doc.get("products")
    if isinstance(embedded, list) and embedded:
        for p in embedded:
            if not isinstance(p, dict):
                continue
            pname = _pick_name(p)
            price = _pick_price(p)
            out.append(
                {
                    "_id_str": str(p.get("_id") or ""),
                    "name": pname,
                    "description": _pick_desc(p),
                    "image_url": _pick_img(p),
                    "price": round(price, 2),
                    "quantity": _pick_qty(p),
                    "created_at": p.get("created_at") or None,
                    "order_link": _product_order_link(pname, price) if wa_number_raw else "",
                }
            )
        return out

    # 3) legacy: products collection fallback
    try:
        q_candidates2: List[Dict[str, Any]] = []
        if slug:
            q_candidates2.append({"store_slug": slug, "status": {"$ne": "deleted"}})
        if store_id:
            q_candidates2.append({"store_id": store_id, "status": {"$ne": "deleted"}})
            q_candidates2.append({"store_id": str(store_id), "status": {"$ne": "deleted"}})
        if owner_id:
            q_candidates2.append({"owner_id": owner_id, "status": {"$ne": "deleted"}})
            q_candidates2.append({"owner_id": str(owner_id), "status": {"$ne": "deleted"}})

        fields2 = {
            "_id": 1,
            "name": 1,
            "title": 1,
            "description": 1,
            "image_url": 1,
            "image": 1,
            "price": 1,
            "amount": 1,
            "selling_price": 1,
            "unit_price": 1,
            "quantity": 1,
            "created_at": 1,
            "status": 1,
        }

        found2: List[Dict[str, Any]] = []
        for q in q_candidates2:
            try:
                if products_col.count_documents(q, limit=1) > 0:
                    found2 = list(products_col.find(q, fields2).sort("created_at", -1))
                    break
            except Exception:
                continue

        for p in found2:
            pname = (p.get("name") or p.get("title") or "Product").strip()
            price = 0.0
            for k in ("price", "amount", "selling_price", "unit_price"):
                if k in p and p.get(k) not in (None, ""):
                    try:
                        price = float(str(p.get(k)).replace(",", "").strip())
                    except Exception:
                        price = 0.0
                    break
            out.append(
                {
                    "_id_str": str(p.get("_id") or ""),
                    "name": pname,
                    "description": (p.get("description") or "").strip(),
                    "image_url": (p.get("image_url") or p.get("image") or "").strip(),
                    "price": round(price, 2),
                    "quantity": 0,
                    "created_at": p.get("created_at") or None,
                    "order_link": _wa_link_from_number(
                        wa_number_raw,
                        f"Hello {store_doc.get('name','')}, I want to order: {pname} (GHS {price:.2f}).",
                    )
                    if wa_number_raw
                    else "",
                }
            )
    except Exception:
        return []

    return out


# ---------------------------------------------------------------------
# Parse + labels
# ---------------------------------------------------------------------
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)

def _service_unit(svc: Dict[str, Any]) -> str:
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes") or name == "afa talktime":
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
                    data = ast.literal_eval(vt)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    if isinstance(value, dict):
        vol = value.get("volume") or value.get("offer") or value.get("gb")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            v = float(vol)
            if unit == "minutes":
                return v
            vol_s = str(vol).upper()
            if "GB" in vol_s:
                return v * 1000.0
            if "MB" in vol_s:
                return v
            return v
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m:
                return float(m.group(1))
            if _NUM.match(s):
                return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m:
                return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m:
                return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m:
                return float(m.group(1))
            if _NUM.match(s2):
                return float(s2)
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
        parsed = _parse_value_field(cleaned)
        if isinstance(parsed, dict):
            vol = _extract_volume(parsed, unit)
            return _format_volume_unit(vol, unit) if vol is not None else "-"
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"


# ---------- pricing map builder ----------
def _build_pricing_map(pricing: Dict[str, Any]) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    percent_default = float(pricing.get("percent_default") or 0.0)
    per_map: Dict[str, Dict[str, Any]] = {}
    for x in (pricing.get("per_service") or []):
        sid = str(x.get("service_id") or "")
        if not sid:
            continue
        entry: Dict[str, Any] = {"percent": None, "offers": {}}
        if x.get("percent") is not None:
            try:
                entry["percent"] = float(x.get("percent"))
            except Exception:
                entry["percent"] = None
        for o in (x.get("offers") or []):
            try:
                idx = int(o.get("index"))
                tot = _to_float(o.get("total"))
                if tot is not None:
                    entry["offers"][idx] = float(tot)
            except Exception:
                continue
        per_map[sid] = entry
    return percent_default, per_map


def _customer_offer_override_map(customer_id: Optional[ObjectId]) -> Dict[str, Dict[int, float]]:
    out: Dict[str, Dict[int, float]] = {}
    try:
        service_profits = db["service_profits"]
        cursor = service_profits.find({"customer_id": None}, {"service_id": 1, "offers": 1})
        for row in cursor:
            sid = str(row.get("service_id") or "")
            if not sid:
                continue
            offers_map: Dict[int, float] = {}
            for offer in (row.get("offers") or []):
                try:
                    idx = int(offer.get("index"))
                    total = _to_float(offer.get("total"))
                except Exception:
                    continue
                if total is not None:
                    offers_map[idx] = round(float(total), 2)
            if offers_map:
                out[sid] = offers_map
        if customer_id:
            cursor = service_profits.find({"customer_id": customer_id}, {"service_id": 1, "offers": 1})
            for row in cursor:
                sid = str(row.get("service_id") or "")
                if not sid:
                    continue
                offers_map: Dict[int, float] = {}
                for offer in (row.get("offers") or []):
                    try:
                        idx = int(offer.get("index"))
                        total = _to_float(offer.get("total"))
                    except Exception:
                        continue
                    if total is not None:
                        offers_map[idx] = round(float(total), 2)
                if offers_map:
                    out[sid] = offers_map
    except Exception:
        return {}
    return out


# ---------- apply pricing to a service (for page render) ----------
def _offer_value_text(o: Dict[str, Any], unit: str) -> str:
    vt = o.get("value_text")
    if isinstance(vt, str) and vt.strip():
        try:
            cleaned = _PKG_TAIL.sub("", vt).strip()
            vol = _extract_volume(cleaned, unit)
            if vol is not None:
                return _format_volume_unit(vol, unit)
        except Exception:
            pass
    lab = _value_text_for_display(o.get("value"), unit)
    return lab or "-"

def _apply_store_pricing_to_service(
    svc: Dict[str, Any],
    percent_default: float,
    per_service_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    s = dict(svc)
    unit = _service_unit(s)
    src_offers = _svc_offers_list(s)
    svc_id_str = str(s.get("_id"))
    per_entry = per_service_map.get(svc_id_str, {})
    svc_percent: Optional[float] = per_entry.get("percent")
    offer_overrides: Dict[int, float] = per_entry.get("offers") or {}

    norm_offers: List[Dict[str, Any]] = []
    for idx, of in enumerate(src_offers):
        base_amount = _offer_base_amount(of)
        if idx in offer_overrides:
            total = round(float(offer_overrides[idx]), 2)
        elif svc_percent is not None and base_amount is not None:
            total = round(base_amount + (base_amount * svc_percent / 100.0), 2)
        else:
            total = round(float(base_amount), 2) if base_amount is not None else None
        vt = _offer_value_text(of, unit)
        norm_offers.append(
            {
                "value_text": vt,
                "total": total,
                "amount": base_amount,
                "value": of.get("value"),
            }
        )

    s["offers"] = norm_offers
    s["offers_source"] = "store_offers" if (isinstance(s.get("store_offers"), list) and s.get("store_offers")) else "offers"
    return s


# ---------- DB loads for editor/view ----------
def _load_all_services_for_store_edit(customer_id: Optional[ObjectId] = None) -> List[Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    fields = {"_id": 1, "name": 1, "image_url": 1, "offers": 1, "store_offers": 1, "unit": 1}
    raw = list(services_col.find({}, fields))
    raw.sort(key=lambda x: _norm(x.get("name") or ""))
    override_map = _customer_offer_override_map(customer_id)

    clean: List[Dict[str, Any]] = []
    for r in raw:
        s: Dict[str, Any] = {
            "_id_str": str(r.get("_id")),
            "name": r.get("name") or "",
            "image_url": (r.get("image_url") or r.get("image") or "").strip(),
        }
        unit = _service_unit(r)
        src_offers = _svc_offers_list(r)

        new_off: List[Dict[str, Any]] = []
        svc_override = override_map.get(s["_id_str"], {})
        for idx, o in enumerate(src_offers):
            base_amount = _offer_base_amount(o)
            new_off.append(
                {
                    "amount": svc_override.get(idx, base_amount),
                    "value": o.get("value"),
                    "value_text": _offer_value_text(o, unit),
                }
            )

        s["offers"] = new_off
        s["offers_source"] = "store_offers" if (isinstance(r.get("store_offers"), list) and r.get("store_offers")) else "offers"
        clean.append(s)
    return clean

def _load_services_for_store_view(scope: str, ids: List[str]) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {}
    if scope == "selected" and ids:
        try:
            q = {"_id": {"$in": [ObjectId(x) for x in ids if x]}}
        except Exception:
            q = {"_id": {"$in": []}}

    fields = {
        "_id": 1,
        "name": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "image_url": 1,
        "offers": 1,
        "store_offers": 1,
        "store_offers_profit": 1,  # ✅ IMPORTANT for profit logic
        "service_category": 1,
        "category": 1,
        "provider": 1,
        "priority": 1,
        "display_order": 1,
        "created_at": 1,
        "updated_at": 1,
        "unit": 1,
        "default_profit_percent": 1,
        "network_id": 1,
        "network": 1,
        "closed_message": 1,
        "out_of_stock_message": 1,
    }
    raw = list(services_col.find(q, fields)) if q else list(services_col.find({}, fields))
    raw = _sorted_services(raw)
    for s in raw:
        s["_id_str"] = str(s["_id"])
        s.update(_service_state(s))
    return raw

def _load_products_as_services_fallback(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        q: Dict[str, Any] = {"status": {"$ne": "deleted"}}
        if store_doc.get("slug"):
            q_alt = {"store_slug": store_doc.get("slug"), "status": {"$ne": "deleted"}}
            if products_col.count_documents(q_alt, limit=1) > 0:
                q = q_alt
        if store_doc.get("owner_id"):
            q_owner = {"owner_id": store_doc.get("owner_id"), "status": {"$ne": "deleted"}}
            if products_col.count_documents(q_owner, limit=1) > 0:
                q = q_owner

        fields = {"_id": 1, "name": 1, "title": 1, "image_url": 1, "price": 1, "amount": 1, "created_at": 1}
        prods = list(products_col.find(q, fields).sort("created_at", -1))
        out: List[Dict[str, Any]] = []
        for p in prods:
            name = (p.get("name") or p.get("title") or "Product").strip()
            price = _to_float(p.get("price")) or _to_float(p.get("amount")) or 0.0
            svc = {
                "_id": p.get("_id"),
                "_id_str": str(p.get("_id")),
                "name": name,
                "type": "MANUAL",
                "status": "OPEN",
                "availability": "AVAILABLE",
                "image_url": p.get("image_url"),
                "service_category": "product",
                "priority": None,
                "display_order": None,
                "created_at": p.get("created_at") or datetime.utcnow(),
                "unit": "item",
                "offers": [
                    {
                        "value_text": "1 item",
                        "total": round(float(price), 2),
                        "amount": round(float(price), 2),
                        "value": {"volume": 1},
                    }
                ],
            }
            svc.update(_service_state(svc))
            out.append(svc)
        return out
    except Exception:
        return []


# ---------- NEW: safe ObjectId + user lookup (NO status filter) ----------
def _safe_oid(v: Any) -> Optional[ObjectId]:
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, str):
        try:
            return ObjectId(v)
        except Exception:
            return None
    return None

def _lookup_user_any_status(user_id: Any) -> Dict[str, Any]:
    """
    Fetch user by _id WITHOUT filtering status.
    """
    oid = _safe_oid(user_id)
    if not oid:
        return {}
    try:
        u = users_col.find_one(
            {"_id": oid},
            {"email": 1, "phone": 1, "username": 1, "first_name": 1, "last_name": 1, "name": 1, "status": 1},
        )
        return u or {}
    except Exception:
        return {}

def _user_first_last(u: Dict[str, Any]) -> Tuple[str, str]:
    """
    Derive first/last from first_name/last_name, or from 'name' if present.
    """
    first = (u.get("first_name") or "").strip()
    last = (u.get("last_name") or "").strip()
    if first or last:
        return first, last

    full = (u.get("name") or u.get("username") or "").strip()
    if not full:
        return "", ""
    parts = [p for p in re.split(r"\s+", full) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ---------- JSON-safe converter (UPDATED to include owner email/phone safely) ----------
def _store_to_client(s: Optional[dict]) -> dict:
    if not s:
        return {}
    out: Dict[str, Any] = {}
    for k, v in s.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [
                (str(x) if isinstance(x, ObjectId) else x.isoformat() if isinstance(x, datetime) else x)
                for x in v
            ]
        elif isinstance(v, dict):
            if k == "pricing":
                per = []
                for row in (v.get("per_service") or []):
                    row2 = dict(row)
                    if isinstance(row2.get("service_id"), ObjectId):
                        row2["service_id"] = str(row2["service_id"])
                    per.append(row2)
                out[k] = {**v, "per_service": per}
            else:
                out[k] = {
                    kk: (
                        str(vv)
                        if isinstance(vv, ObjectId)
                        else vv.isoformat()
                        if isinstance(vv, datetime)
                        else vv
                    )
                    for kk, vv in v.items()
                }
        else:
            out[k] = v
    if "service_ids" in out:
        out["service_ids"] = [str(x) for x in (out.get("service_ids") or [])]

    # ✅ attach owner info from users collection (even if user.status == 'deleted')
    try:
        u = _lookup_user_any_status(s.get("owner_id"))
        out["owner_email"] = (u.get("email") or "").strip()
        out["owner_phone"] = (u.get("phone") or "").strip()
        out["owner_username"] = (u.get("username") or "").strip()
        out["owner_status"] = (u.get("status") or "").strip()
        fn, ln = _user_first_last(u or {})
        out["owner_first_name"] = fn
        out["owner_last_name"] = ln
    except Exception:
        out["owner_email"] = out.get("owner_email") or ""
        out["owner_phone"] = out.get("owner_phone") or ""
        out["owner_first_name"] = out.get("owner_first_name") or ""
        out["owner_last_name"] = out.get("owner_last_name") or ""

    return out


# ---------- helper: find current user's store ----------
def _find_user_store(user_id: ObjectId, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    q: Dict[str, Any] = {"owner_id": user_id, "status": {"$ne": "deleted"}}
    if slug:
        q["slug"] = slug
    return stores_col.find_one(q, sort=[("updated_at", -1), ("created_at", -1)])


# ---------- compatibility helper: _find (some files import it) ----------
def _find(col, q: dict, projection: Optional[dict] = None, sort: Optional[list] = None):
    """
    Compatibility helper (kept to prevent ImportError in files that do:
      from .store_page import _find
    """
    try:
        if sort:
            return col.find_one(q, projection or None, sort=sort)
        return col.find_one(q, projection or None)
    except Exception:
        return None


# ---------- helper: store owner's email (UPDATED: no status filter) ----------
def _get_owner_email_for_store(store_doc: Dict[str, Any]) -> str:
    try:
        oid2 = _safe_oid(store_doc.get("owner_id"))
        if not oid2:
            return ""
        u = users_col.find_one({"_id": oid2}, {"email": 1})
        if not u:
            return ""
        return (u.get("email") or "").strip()
    except Exception:
        return ""

def _get_owner_identity_for_store(store_doc: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    ✅ Paystack payer identity MUST come from DB (no fallback defaults).
    Returns (email, first_name, last_name)
    """
    try:
        u = _lookup_user_any_status(store_doc.get("owner_id"))
        email = (u.get("email") or "").strip()
        first, last = _user_first_last(u or {})
        return email, (first or "").strip(), (last or "").strip()
    except Exception:
        return "", "", ""


# ---------- shared upsert ----------
def _upsert_store_from_payload(owner_id: ObjectId, data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    name = (data.get("name") or "").strip()
    slug = _slugify(data.get("slug") or name)
    status = (data.get("status") or "published").strip()
    if not name or not slug:
        return False, {"message": "Name and slug are required"}

    existing = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if existing and str(existing.get("owner_id")) != str(owner_id):
        return False, {"message": "Slug already taken"}

    doc = {
        "owner_id": owner_id,
        "name": name,
        "slug": slug,
        "logo_url": (data.get("logo_url") or "").strip(),
        "layout": (data.get("layout") or "grid-2").strip(),
        "theme": data.get("theme") or {},
        "hero": data.get("hero") or {},
        "service_scope": data.get("service_scope") or "all",
        "service_ids": data.get("service_ids") or [],
        "pricing": data.get("pricing") or {"mode": "percent", "percent_default": 0.0, "per_service": []},
        "products": data.get("products") or data.get("store_products") or data.get("items") or [],
        "whatsapp_number": (data.get("whatsapp_number") or data.get("whatsapp") or "").strip()
        if isinstance(data.get("whatsapp_number") or data.get("whatsapp") or "", str)
        else data.get("whatsapp_number") or data.get("whatsapp"),
        "whatsapp_group": (data.get("whatsapp_group") or data.get("whatsapp_group_link") or "").strip(),
        "status": status,
        "updated_at": datetime.utcnow(),
    }
    stores_col.update_one(
        {"slug": slug, "owner_id": owner_id},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return True, {"slug": slug, "status": status}


# =====================================================================
# PAGES (PUBLIC)
# =====================================================================
@stores_bp.route("/s/<slug>", methods=["GET"])
def store_public_page(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir
    store_doc = stores_col.find_one(
        {"slug": slug, "status": {"$regex": r"^published$", "$options": "i"}}
    )
    if not store_doc:
        # allow preview=1 for logged-in owner
        if request.args.get("preview") == "1" and session.get("user_id"):
            store_doc = stores_col.find_one(
                {"slug": slug, "owner_id": ObjectId(session["user_id"]), "status": {"$ne": "deleted"}}
            )
            if not store_doc:
                return "Store not found", 404
        else:
            return "Store not found", 404

    scope = store_doc.get("service_scope") or "all"
    service_ids = store_doc.get("service_ids") or []
    services = _load_services_for_store_view(scope, service_ids)

    # legacy fallback (only if you were using products as services)
    if not services:
        services = _load_products_as_services_fallback(store_doc)

    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    priced = [_apply_store_pricing_to_service(s, percent_default, per_map) for s in services]

    q = request.query_string.decode("utf-8")
    canonical_host = TARGET_STORE_HOST or request.host
    canonical_url = f"{request.scheme}://{canonical_host}{request.path}" + (f"?{q}" if q else "")

    wa = _extract_store_whatsapp(store_doc)

    # ✅ REAL products list for the Products tab
    products = _load_store_products(store_doc, wa.get("number_raw") or "")

    # ✅ Ensure we never pass secret key to frontend
    paystack_keys = get_paystack_keys()
    pk_for_frontend = paystack_keys.get("public_key", "") if is_public_key(paystack_keys.get("public_key", "")) else ""

    # ✅ Fetch owner identity for Paystack payer identity (NO DEFAULTS)
    ps_email, ps_first, ps_last = _get_owner_identity_for_store(store_doc)

    # ✅ Fetch email (store email + owner email) for extra context if needed
    owner_email = _get_owner_email_for_store(store_doc)

    return render_template(
        "store_page.html",
        store=store_doc,
        services=priced,
        products=products,
        paystack_pk=pk_for_frontend,
        canonical_url=canonical_url,
        whatsapp_number=wa.get("number_raw") or "",
        whatsapp_number_digits=wa.get("number_digits") or "",
        whatsapp_number_link=wa.get("number_link") or "",
        whatsapp_group_link=wa.get("group_link") or "",
        # extra fields (won't break template even if unused)
        store_email=(store_doc.get("email") or "").strip(),
        owner_email=owner_email,

        # ✅ REQUIRED by your HTML scripts (NO DEFAULTS)
        paystack_payer_email=ps_email,
        paystack_payer_first=ps_first,
        paystack_payer_last=ps_last,
    )


# ✅ API: fetch store email (and owner email) without touching HTML
@stores_bp.route("/api/store-email/<slug>", methods=["GET"])
def api_store_email(slug: str):
    try:
        store_doc = stores_col.find_one(
            {"slug": slug, "status": {"$ne": "deleted"}},
            {"email": 1, "owner_id": 1, "slug": 1, "name": 1},
        )
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404
        owner_email = _get_owner_email_for_store(store_doc)
        return jsonify(
            {
                "success": True,
                "slug": slug,
                "store_name": store_doc.get("name") or "",
                "store_email": (store_doc.get("email") or "").strip(),
                "owner_email": owner_email,
            }
        ), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


# ✅ API: Store products payload builder
def _products_payload(store_doc: Dict[str, Any]) -> Dict[str, Any]:
    wa = _extract_store_whatsapp(store_doc or {})
    products = _load_store_products(store_doc or {}, wa.get("number_raw") or "")
    return {
        "success": True,
        "store": {
            "slug": store_doc.get("slug") or "",
            "name": store_doc.get("name") or "",
            "logo_url": store_doc.get("logo_url") or "",
            "status": store_doc.get("status") or "",
            "owner_id": str(store_doc.get("owner_id")) if store_doc.get("owner_id") else "",
        },
        "whatsapp": {
            "number_raw": wa.get("number_raw") or "",
            "number_digits": wa.get("number_digits") or "",
            "number_link": wa.get("number_link") or "",
            "group_link": wa.get("group_link") or "",
        },
        "count": len(products),
        "products": products,
    }

@stores_bp.route("/api/store-products/<slug>", methods=["GET"])
def api_store_products_by_slug(slug: str):
    """
    Frontend-friendly products API.
    - Returns products created for this store (store_products primary, then fallbacks).
    - Optional: ?owner_id=<id> or ?manager_id=<id> (filters if you use those fields)
    """
    try:
        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        owner_id = (request.args.get("owner_id") or "").strip()
        manager_id = (request.args.get("manager_id") or "").strip()

        if owner_id or manager_id:
            q: Dict[str, Any] = {"store_slug": slug, "status": {"$ne": "deleted"}}
            if owner_id:
                q["owner_id"] = owner_id
            if manager_id:
                q["manager_id"] = manager_id

            fields = {
                "_id": 1,
                "name": 1,
                "description": 1,
                "image_url": 1,
                "price": 1,
                "quantity": 1,
                "created_at": 1,
                "updated_at": 1,
            }

            found = list(store_products_col.find(q, fields).sort("created_at", -1))
            wa = _extract_store_whatsapp(store_doc)
            products: List[Dict[str, Any]] = []
            for p in found:
                try:
                    price = float(str(p.get("price") or "0").replace(",", "").strip())
                except Exception:
                    price = 0.0
                pname = (p.get("name") or "Product").strip()
                qty_raw = p.get("quantity")
                try:
                    qty = int(float(str(qty_raw).replace(",", "").strip())) if str(qty_raw or "").strip() != "" else 0
                except Exception:
                    qty = 0

                products.append(
                    {
                        "_id_str": str(p.get("_id") or ""),
                        "name": pname,
                        "description": (p.get("description") or "").strip(),
                        "image_url": (p.get("image_url") or "").strip(),
                        "price": round(price, 2),
                        "quantity": qty,
                        "created_at": p.get("created_at") or None,
                        "order_link": _wa_link_from_number(
                            wa.get("number_raw") or "",
                            f"Hello {store_doc.get('name','')}, I want to order: {pname} (GHS {price:.2f}).",
                        )
                        if (wa.get("number_raw") or "")
                        else "",
                    }
                )

            payload = _products_payload(store_doc)
            payload["products"] = products
            payload["count"] = len(products)
            payload["filters"] = {"owner_id": owner_id, "manager_id": manager_id}
            return jsonify(payload), 200

        return jsonify(_products_payload(store_doc)), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500

@stores_bp.route("/api/store-products/by-owner/<owner_id>", methods=["GET"])
def api_store_products_by_owner(owner_id: str):
    """
    Useful for dashboards:
    GET /api/store-products/by-owner/<owner_id>
    Optional: ?slug=<store_slug>
    """
    try:
        owner_id = (owner_id or "").strip()
        if not owner_id:
            return jsonify({"success": False, "message": "owner_id required"}), 400

        slug = (request.args.get("slug") or "").strip()

        store_q: Dict[str, Any] = {"status": {"$ne": "deleted"}}
        oid = _safe_oid(owner_id)
        if oid:
            store_q["owner_id"] = oid
        else:
            store_q["owner_id"] = owner_id

        if slug:
            store_q["slug"] = slug

        store_doc = stores_col.find_one(store_q, sort=[("updated_at", -1), ("created_at", -1)])
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found for owner"}), 404

        return jsonify(_products_payload(store_doc)), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


# =====================================================================
# PAYSTACK FLOW (Store)
# =====================================================================
def _verify_paystack(reference: str) -> Tuple[bool, Dict[str, Any], str]:
    paystack_secret_key = (get_paystack_keys().get("secret_key") or "").strip()
    if not paystack_secret_key or not is_secret_key(paystack_secret_key):
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

DUP_WINDOW_MINUTES = 30

def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0

def _build_bundle_key(is_express: bool, shared_bundle, value_obj: dict):
    if is_express:
        val = shared_bundle
        if val is None:
            val = (value_obj or {}).get("id")
        try:
            return ("offer", int(val)) if val is not None else None
        except Exception:
            return None
    else:
        val = shared_bundle
        if val is None:
            val = (value_obj or {}).get("volume")
        try:
            return ("vol", int(val)) if val is not None else None
        except Exception:
            return None

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

    alt = {"phone": phone, "network_id": network_id, "amount": amount_key}
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

def _canonical_store_total_for_offer(
    store_doc: Dict[str, Any],
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    if not svc_doc:
        return None

    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    svc_id_str = str(svc_doc.get("_id"))
    per_entry = per_map.get(svc_id_str, {})
    svc_percent = per_entry.get("percent")

    offers = _svc_offers_list(svc_doc)
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
    if base_amount is None:
        return None

    offer_overrides = per_entry.get("offers") or {}
    if best_idx in offer_overrides:
        return round(float(offer_overrides[best_idx]), 2)

    if svc_percent is not None:
        return round(base_amount + (base_amount * float(svc_percent) / 100.0), 2)
    return round(float(base_amount), 2)

def _store_profit_percent_for_item(
    store_doc: Dict[str, Any],
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
    base_amount: float,
) -> float:
    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    if not svc_doc:
        return float(percent_default or 0.0)

    svc_id_str = str(svc_doc.get("_id") or "")
    per_entry = per_map.get(svc_id_str, {})
    svc_percent = per_entry.get("percent")
    if svc_percent is not None:
        try:
            return float(svc_percent)
        except Exception:
            return float(percent_default or 0.0)

    offer_overrides = per_entry.get("offers") or {}
    if offer_overrides:
        offers = _svc_offers_list(svc_doc)
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

        if best_idx is not None and best_idx in offer_overrides:
            override_total = _to_float(offer_overrides.get(best_idx))
            base = float(base_amount or 0.0)
            if base <= 0 and best_idx < len(offers):
                base = float(_offer_base_amount(offers[best_idx]) or 0.0)
            if override_total is not None and base > 0:
                return round(((float(override_total) - base) / base) * 100.0, 2)

    return 0.0

def _server_reprice_store_cart(
    store_doc: Dict[str, Any], cart: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], float]:
    revised: List[Dict[str, Any]] = []
    sys_total = 0.0
    for item in cart:
        service_id_raw = item.get("serviceId")
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

        svc_doc: Optional[Dict[str, Any]] = None
        if service_id_raw:
            try:
                svc_doc = services_col.find_one(
                    {"_id": ObjectId(service_id_raw)},
                    {
                        "offers": 1,
                        "store_offers": 1,
                        "unit": 1,
                        "name": 1,
                        "type": 1,
                        "service_category": 1,
                        "provider": 1,
                        "default_profit_percent": 1,
                        "store_offers_profit": 1,
                        "status": 1,
                        "availability": 1,
                        "network_id": 1,
                        "network": 1,
                        "service_network": 1,
                        "provider_network": 1,
                        "justice_network": 1,
                    },
                )
            except Exception:
                svc_doc = None

        canonical = _canonical_store_total_for_offer(
            store_doc or {}, svc_doc or {}, value_obj, item.get("value")
        )
        if canonical is None:
            canonical = _money(item.get("amount"))

        revised.append({**item, "amount": canonical})
        sys_total += canonical

    return revised, round(sys_total, 2)

def _resolve_network_id(item: dict, value_obj: dict, svc_doc: dict | None):
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


# =====================================================================
# ✅ IMPORTANT FIX: Profit MUST be computed from SYSTEM offers (svc.offers)
# - base_amount = svc.offers[].amount
# - profit% = svc.store_offers_profit (fallback default_profit_percent)
# - profit = base_amount * profit%
# =====================================================================
def _system_offer_base_amount_from_service(
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    """
    ✅ System base amount must come from svc_doc.offers (NOT store_offers).
    We match closest offer by volume/minutes.
    """
    if not svc_doc:
        return None

    offers = svc_doc.get("offers")
    if not isinstance(offers, list) or not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")

    for idx, of in enumerate(offers):
        try:
            parsed = _parse_value_field(of.get("value"))
            vol = _extract_volume(parsed, unit)
            if vol_needed is not None and vol is not None:
                diff = abs(float(vol) - float(vol_needed))
                if diff < best_diff:
                    best_idx, best_diff = idx, diff
            elif best_idx is None:
                best_idx = idx
        except Exception:
            continue

    if best_idx is None:
        return None

    return _to_float((offers[best_idx] or {}).get("amount"))


@stores_bp.route("/store-checkout/<slug>", methods=["POST"])
def store_checkout_paystack(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir
    try:
        body = request.get_json(silent=True) or {}
        cart = body.get("cart") or []
        method = (body.get("method") or "paystack_inline").strip().lower()
        ps_info = body.get("paystack") or {}
        ps_ref = (ps_info.get("reference") or "").strip()
        paystack_verified = False

        jlog("store_public_checkout_incoming", slug=slug, payload={"method": method, "has_ref": bool(ps_ref), "cart_len": len(cart) if isinstance(cart, list) else -1})

        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        # idempotency: same reference should not create multiple orders
        if ps_ref:
            prior = orders_col.find_one({"store_slug": slug, "paystack_reference": ps_ref})
            if prior:
                return jsonify(
                    {
                        "success": True,
                        "message": f"✅ Order already created. Order ID: {prior.get('order_id')}",
                        "order_id": prior.get("order_id"),
                        "status": prior.get("status"),
                        "charged_amount": prior.get("charged_amount"),
                        "profit_amount_total": prior.get("profit_amount_total", 0.0),
                        "items": prior.get("items", []),
                        "idempotent": True,
                    }
                ), 200

        # server-side repricing (prevents client tampering)
        cart, total_requested = _server_reprice_store_cart(store_doc, cart)
        if total_requested <= 0:
            return jsonify({"success": False, "message": "Total amount must be greater than zero"}), 400

        if method != "paystack_inline" or not ps_ref:
            return jsonify({"success": False, "message": "Payment missing. Please pay first."}), 400

        ok, verify_data, fail_reason = _verify_paystack(ps_ref)
        if not ok:
            return jsonify({"success": False, "message": f"Payment verification failed: {fail_reason}"}), 400
        paystack_verified = True

        paid_pes = int(verify_data.get("amount") or 0)
        paid_ghs = round(paid_pes / 100.0, 2)
        currency = (verify_data.get("currency") or "GHS").upper()
        if paid_pes <= 0 or currency != "GHS":
            return jsonify({"success": False, "message": "Invalid payment amount/currency."}), 400

        expected_pay_ghs = round(total_requested, 2)
        expected_pay_pes = int(round(expected_pay_ghs * 100))
        if not _paid_enough(paid_pes, expected_pay_pes):
            jlog(
                "store_public_checkout_amount_underpaid",
                slug=slug,
                paid_pes=paid_pes,
                expected_pes=expected_pay_pes,
                paid_ghs=paid_ghs,
                expected_ghs=expected_pay_ghs,
            )
            return jsonify(
                {
                    "success": False,
                    "message": "Payment amount is less than required. Please complete full payment.",
                    "paid": paid_ghs,
                    "required": expected_pay_ghs,
                }
            ), 400

        fee_delta_ghs = max(0.0, round(paid_ghs - expected_pay_ghs, 2))

        # transaction doc (align with checkout.py)
        txn_user_id = ObjectId(session["user_id"]) if session.get("user_id") else store_doc.get("owner_id")
        txn_doc = {
            "user_id": txn_user_id,
            "amount": round(paid_ghs, 2),
            "reference": ps_ref,
            "status": "success",
            "type": "debit",
            "source": "paystack_inline",
            "gateway": "Paystack",
            "currency": "GHS",
            "channel": verify_data.get("channel"),
            "verified_at": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "raw": verify_data,
            "meta": {
                "store_checkout": True,
                "store_slug": slug,
                "expected_pay_total_ghs": expected_pay_ghs,
                "paid_total_ghs": paid_ghs,
                "gateway_fee_overage_ghs": fee_delta_ghs,
                "note": "Customer payment captured via store inline checkout (server repriced).",
            },
        }

        if not transactions_col.find_one({"reference": ps_ref, "source": "paystack_inline", "status": "success"}):
            if _checkout_helpers.get("txn_fn"):
                try:
                    _checkout_helpers["txn_fn"](transactions_col, txn_doc)
                except Exception:
                    transactions_col.insert_one(txn_doc)
            else:
                transactions_col.insert_one(txn_doc)

        order_id = generate_order_id()
        results: List[Dict[str, Any]] = []
        debug_events: List[Dict[str, Any]] = []

        profit_amount_total = 0.0
        total_processing_amount = 0.0
        seen_keys = set()

        for idx, item in enumerate(cart, start=1):
            phone = (item.get("phone") or "").strip()
            amt_total = _money(item.get("amount"))
            amount_key = _normalize_amount_key(amt_total)

            service_id_raw = item.get("serviceId")
            svc_doc: Optional[Dict[str, Any]] = None
            svc_type: Optional[str] = None
            svc_name = item.get("serviceName") or None

            if service_id_raw:
                try:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {
                            "type": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "service_network": 1,
                            "offers": 1,
                            "store_offers": 1,
                            "store_offers_profit": 1,   # ✅ needed
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "provider": 1,
                            "provider_network": 1,
                            "justice_network": 1,
                            "status": 1,
                            "availability": 1,
                            "unit": 1,
                        },
                    )
                    if svc_doc:
                        st = svc_doc.get("type")
                        svc_type = st.strip().upper() if isinstance(st, str) else st
                        svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
                except Exception:
                    svc_doc = None
                    svc_type = None

            is_unavail, reason_text = _service_unavailability_reason(svc_doc)
            if is_unavail:
                return jsonify(
                    {
                        "success": False,
                        "message": reason_text,
                        "unavailable": {"serviceId": service_id_raw, "serviceName": svc_name, "reason": reason_text},
                    }
                ), 400

            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

            # -----------------------------------------------------------------
            # Profit logic (requested):
            # profit_amount = store base price - system offer base (svc.offers)
            # -----------------------------------------------------------------
            system_offer_base = _system_offer_base_amount_from_service(svc_doc, value_obj, item.get("value"))
            base_amount = round(float(_to_float(item.get("base_amount")) or 0.0), 2)
            profit_amount = 0.0
            profit_percent_used = 0.0
            if system_offer_base is not None and base_amount > 0:
                profit_amount = max(0.0, round(base_amount - float(system_offer_base), 2))
                if system_offer_base > 0:
                    profit_percent_used = round((profit_amount / float(system_offer_base)) * 100.0, 2)
            profit_amount_total += profit_amount
            store_profit_percent = _store_profit_percent_for_item(
                store_doc, svc_doc, value_obj, item.get("value"), base_amount
            )
            store_profit_amount = max(0.0, round(amt_total - base_amount, 2))
            store_profit_field = {"store_profit_amount": store_profit_amount} if paystack_verified else {}

            # network + api
            network_id = _resolve_network_id(item, value_obj, svc_doc) if svc_doc else None

            # bundle key (dedupe)
            shared_bundle_for_key = None
            if svc_doc:
                unit = _service_unit(svc_doc)
                vol_for_key = _extract_volume(
                    value_obj if isinstance(value_obj, dict) else item.get("value"), unit
                )
                if vol_for_key is not None:
                    try:
                        shared_bundle_for_key = int(vol_for_key)
                    except Exception:
                        shared_bundle_for_key = None

            is_express = False
            bundle_key = _build_bundle_key(is_express, shared_bundle_for_key, value_obj)

            if phone and (network_id is not None) and (bundle_key is not None):
                cart_key = (phone, int(network_id), int(bundle_key[1]), bundle_key[0], amount_key)
                if cart_key in seen_keys:
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": 0.0,
                            "amount": 0.0,
                            "originally_requested_amount": amt_total,
                            "profit_amount": 0.0,
                            "profit_percent_used": 0.0,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": (svc_type if svc_type else ("unknown" if not svc_doc else None)),
                            "network_id": network_id,
                            "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]},
                            "line_amount_key": amount_key,
                            "line_status": "skipped_duplicate_in_cart",
                            "api_status": "skipped",
                            "api_response": {"note": "Duplicate line in this cart (same number, network, bundle, amount)"},
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
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": (svc_type if svc_type else ("unknown" if not svc_doc else None)),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_processing",
                        "api_status": "skipped",
                        "api_response": {
                            "note": "Same number + same network + same bundle + same amount already processing; skipping."
                        },
                    }
                )
                continue

            resolved_network = _resolve_network_slug(svc_doc, item)
            svc_name_norm = (svc_name or "").strip().lower()
            svc_network_norm = (svc_doc.get("network") or "").strip().lower() if svc_doc else ""
            combo_name_net = f"{svc_name_norm} {svc_network_norm}"

            is_mtn_express = (svc_name_norm == "mtn express")
            is_mtn_normal = (svc_name_norm == "mtn normal")
            is_telecel_bundle = ("telecel" in combo_name_net)
            is_ishare_bundle = (
                "ishare" in combo_name_net
                or "i share" in combo_name_net
                or "at - ishare" in combo_name_net
            )

            svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
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
                "store_line_routing",
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

            total_processing_amount += amt_total
            results.append(
                {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **store_profit_field,
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

        skipped_count = sum(
            1
            for it in results
            if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )

        store_profit_total = 0.0
        if paystack_verified:
            store_profit_total = sum(_money(it.get("store_profit_amount")) for it in results)

        order_doc = {
            "user_id": (ObjectId(session["user_id"]) if session.get("user_id") else store_doc.get("owner_id")),
            "store_slug": slug,
            "order_id": order_id,
            "items": results,
            "total_amount": round(total_requested, 2),
            "charged_amount": round(total_processing_amount, 2),
            "profit_amount_total": round(profit_amount_total, 2),
            "status": "pending",
            "paid_from": "paystack_inline",
            "paystack_reference": ps_ref,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "debug": {
                "store_checkout": True,
                "events": debug_events[-10:],
                "paystack_paid_ghs": paid_ghs,
                "paystack_expected_ghs": expected_pay_ghs,
                "gateway_fee_overage_ghs": fee_delta_ghs,
                "skipped_count": skipped_count,
            },
        }

        for it in (order_doc.get("items") or []):
            if not it.get("line_status"):
                it["line_status"] = "pending"
            if isinstance(it.get("value"), (dict, list)):
                it["value"] = ""
            if not it.get("value"):
                vo = it.get("value_obj") or {}
                vol = vo.get("volume")
                if isinstance(vol, (int, float)) and vol > 0:
                    gb = vol / 1000
                    it["value"] = f"{gb:g}GB"
                else:
                    it["value"] = "N/A"

        if _checkout_helpers.get("order_fn"):
            try:
                _checkout_helpers["order_fn"](orders_col, order_doc)
            except Exception:
                orders_col.insert_one(order_doc)
        else:
            orders_col.insert_one(order_doc)

        if paystack_verified and store_profit_total > 0:
            try:
                store_accounts_col.update_one(
                    {"store_slug": slug},
                    {
                        "$inc": {"total_profit_balance": round(store_profit_total, 2)},
                        "$set": {
                            "last_updated_profit": round(store_profit_total, 2),
                            "updated_at": datetime.utcnow(),
                        },
                        "$setOnInsert": {
                            "store_slug": slug,
                            "created_at": datetime.utcnow(),
                        },
                    },
                    upsert=True,
                )
            except Exception:
                jlog("store_account_update_error", store_slug=slug)


        return jsonify(
            {
                "success": True,
                "message": f"✅ Order received and is processing. Order ID: {order_id}",
                "order_id": order_id,
                "status": "processing",
                "charged_amount": round(total_processing_amount, 2),
                "profit_amount_total": round(profit_amount_total, 2),
                "skipped_count": skipped_count,
                "items": results,
                "paid_ghs": paid_ghs,
                "expected_ghs": expected_pay_ghs,
            }
        ), 200

    except Exception:
        try:
            jlog("store_public_checkout_uncaught", slug=slug, error=traceback.format_exc())
        except Exception:
            pass
        return jsonify({"success": False, "message": "Server error"}), 500


# ---------------------------------------------------------------------
# Optional helper endpoint (safe): fetch order summary by order_id
# (does not expose sensitive paystack raw)
# ---------------------------------------------------------------------
@stores_bp.route("/api/store-order/<order_id>", methods=["GET"])
def api_store_order(order_id: str):
    try:
        order_id = (order_id or "").strip()
        if not order_id:
            return jsonify({"success": False, "message": "order_id required"}), 400

        doc = orders_col.find_one(
            {"order_id": order_id},
            {
                "_id": 0,
                "order_id": 1,
                "store_slug": 1,
                "status": 1,
                "total_amount": 1,
                "charged_amount": 1,
                "profit_amount_total": 1,
                "items": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        if not doc:
            return jsonify({"success": False, "message": "Order not found"}), 404

        # datetime safe
        for k in ("created_at", "updated_at"):
            if isinstance(doc.get(k), datetime):
                doc[k] = doc[k].isoformat()

        return jsonify({"success": True, "order": doc}), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500
