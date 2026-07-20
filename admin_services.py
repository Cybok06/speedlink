from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify, Request
from db import db
from datetime import datetime
from bson import ObjectId
from werkzeug.utils import secure_filename
import os
import json
import uuid
import re
from ast import literal_eval
from collections import defaultdict
from typing import Dict, List

admin_services_bp = Blueprint("admin_services", __name__)
services_col = db["services"]
users_col = db["users"]                     # customers live here
service_profits_col = db["service_profits"]  # {service_id, customer_id, offers:[{index, value_text, total}], created_at, updated_at}

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")

def _ensure_upload_folder():
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _require_admin():
    return session.get("role") == "admin"

_ALLOWED_TYPES = {"API", "OFF"}
def _norm_type(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().upper()
    return t if t in _ALLOWED_TYPES else None

_ALLOWED_PROVIDERS = {"justice", "skplug"}
def _norm_provider(p: str | None) -> str | None:
    if not p:
        return None
    p = str(p).strip().lower()
    return p if p in _ALLOWED_PROVIDERS else None

def _to_float(s):
    try:
        return float(s)
    except Exception:
        return None

def _to_int(s):
    try:
        if isinstance(s, str):
            s = s.replace(",", "").strip()
        return int(float(s))
    except Exception:
        return None

_MB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*MB\s*$", re.I)
_GB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*G(?:B|IG)?\s*$", re.I)
_INT_RE = re.compile(r"^\s*[\d,]+\s*$")

def _parse_volume_to_mb(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    txt = str(v).strip()

    m = _MB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val))

    m = _GB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val * 1000))

    if _INT_RE.match(txt):
        return int(txt.replace(",", ""))

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "volume" in as_json:
                return _to_int(as_json["volume"])
    except Exception:
        pass

    try:
        d = literal_eval(txt)
        if isinstance(d, dict) and "volume" in d:
            return _to_int(d["volume"])
    except Exception:
        pass

    return None

def _format_volume(vol_mb):
    if vol_mb is None:
        return "-"
    try:
        vol_mb = float(vol_mb)
    except Exception:
        return "-"
    if vol_mb >= 1000:
        gb = vol_mb / 1000.0
        return f"{int(gb)}GB" if abs(gb - round(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(vol_mb)}MB"

def _extract_pkg_id(value_raw):
    if value_raw is None:
        return None
    if isinstance(value_raw, (int, float)):
        return _to_int(value_raw)

    txt = str(value_raw).strip()
    if _INT_RE.match(txt):
        return _to_int(txt)

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "id" in as_json:
                return _to_int(as_json["id"])
    except Exception:
        pass

    try:
        d = literal_eval(txt)
        if isinstance(d, dict) and "id" in d:
            return _to_int(d["id"])
    except Exception:
        pass

    return None

def _to_mtn_value_string(pkg_id: int | None, volume_mb: int | None, fallback_value_raw: str | None):
    if volume_mb is None:
        volume_mb = _parse_volume_to_mb(fallback_value_raw)
    volume_mb = _to_int(volume_mb) if volume_mb is not None else None
    pkg_id = _to_int(pkg_id) if pkg_id is not None else None
    if pkg_id is None or volume_mb is None:
        return None
    return f"{{'id': {pkg_id}, 'volume': {volume_mb}}}"

def _compute_value_text_from_mtn_string(value_str: str):
    if not isinstance(value_str, str):
        vol_mb = _parse_volume_to_mb(value_str)
        return _format_volume(vol_mb) if vol_mb is not None else "-"
    try:
        d = literal_eval(value_str)
        if not isinstance(d, dict):
            return value_str
        vol_mb = _to_int(d.get("volume"))
        return _format_volume(vol_mb)
    except Exception:
        vol_mb = _parse_volume_to_mb(value_str)
        if vol_mb is not None:
            return _format_volume(vol_mb)
        return value_str or "-"

# ===========================
# OFFERS PARSER (WITH PREFIX)
# ===========================
def _parse_offers(req: Request, prefix: str = "offers"):
    """
    prefix='offers'         -> uses offers_amount[], offers_value[]
    prefix='store_offers'   -> uses store_offers_amount[], store_offers_value[]
    """
    amount_key = f"{prefix}_amount[]"
    value_key  = f"{prefix}_value[]"

    amounts = req.form.getlist(amount_key)
    values_freetext = req.form.getlist(value_key)

    n = max(len(amounts), len(values_freetext))
    offers = []
    auto_id_seed = 1

    for i in range(n):
        amount = (amounts[i] if i < len(amounts) else "").strip()
        value_txt = (values_freetext[i] if i < len(values_freetext) else "").strip()

        if not amount and not value_txt:
            continue

        base_amount = _to_float(amount)

        pkg_id = _extract_pkg_id(value_txt)
        vol_mb = _parse_volume_to_mb(value_txt)

        if pkg_id is None:
            pkg_id = auto_id_seed
            auto_id_seed += 1

        value_str = _to_mtn_value_string(pkg_id, vol_mb, value_txt)
        if value_str is None and (pkg_id is not None and vol_mb is not None):
            value_str = f"{{'id': {int(pkg_id)}, 'volume': {int(vol_mb)}}}"

        offers.append({
            "amount": base_amount,
            "value": value_str,
            "profit": None
        })

    return offers

# =======================
#  CUSTOMER PRICE HELPERS
# =======================
def _service_offers(service_doc):
    offers = service_doc.get("offers")
    return offers if isinstance(offers, list) else []

def _service_value_text(service_doc, idx: int) -> str:
    offers = _service_offers(service_doc)
    if idx < 0 or idx >= len(offers):
        return "-"
    value = offers[idx].get("value")
    return _compute_value_text_from_mtn_string(value)

def _parse_customer_offer_prices(req: Request, service_doc) -> List[Dict]:
    indices = req.form.getlist("offer_index[]")
    totals = req.form.getlist("offer_total[]")
    offers = _service_offers(service_doc)
    picked = []

    for raw_idx, raw_total in zip(indices, totals):
        try:
            idx = int(raw_idx)
        except Exception:
            continue
        if idx < 0 or idx >= len(offers):
            continue
        total = _to_float(raw_total)
        if total is None or total < 0:
            continue
        picked.append(
            {
                "index": idx,
                "value_text": _service_value_text(service_doc, idx),
                "total": round(float(total), 2),
            }
        )

    picked.sort(key=lambda x: x["index"])
    return picked

def _offers_summary(offers: List[Dict]) -> str:
    if not offers:
        return "Fallback to system prices"
    head = [f"{x.get('value_text')}: GHS {float(x.get('total') or 0):.2f}" for x in offers[:3]]
    if len(offers) > 3:
        head.append(f"+{len(offers) - 3} more")
    return " | ".join(head)

def _pricing_scope_label(ov: Dict, names_map: Dict[ObjectId, str]) -> str:
    customer_id = ov.get("customer_id")
    if customer_id is None:
        return "All Agents"
    return names_map.get(customer_id, str(customer_id))

def _display_name(user_doc):
    nm = (user_doc.get("business_name") or "").strip()
    if nm:
        return nm
    fn = (user_doc.get("first_name") or "").strip()
    ln = (user_doc.get("last_name") or "").strip()
    full = (" ".join([fn, ln])).strip()
    return full or (user_doc.get("username") or user_doc.get("phone") or str(user_doc.get("_id")))

# =======================
#      PAGE ROUTES
# =======================
@admin_services_bp.route("/admin/services", methods=["GET"])
def manage_services():
    if not _require_admin():
        return redirect(url_for("login.login"))

    services = list(services_col.find({}, {
        "name": 1,
        "image_url": 1,
        "offers": 1,
        "store_offers": 1,   # NEW
        "public_offers": 1,
        "created_at": 1,
        "type": 1,
        "provider": 1,
        "status": 1,
        "availability": 1
    }).sort([("_id", -1)]))

    for s in services:
        s["_id_str"] = str(s["_id"])

        # compute value_text for default + store
        for key in ("offers", "store_offers", "public_offers"):
            if isinstance(s.get(key), list):
                for of in s[key]:
                    v = of.get("value")
                    of["value_text"] = _compute_value_text_from_mtn_string(v)

    service_ids = [s["_id"] for s in services]
    overrides_by_service = defaultdict(list)
    customer_id_set = set()

    if service_ids:
        for ov in service_profits_col.find({"service_id": {"$in": service_ids}}):
            overrides_by_service[ov["service_id"]].append(ov)
            if ov.get("customer_id") is not None:
                customer_id_set.add(ov["customer_id"])

    names_map = {}
    if customer_id_set:
        for u in users_col.find({"_id": {"$in": list(customer_id_set)}},
                                {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "business_name": 1}):
            names_map[u["_id"]] = _display_name(u)

    for s in services:
        ov_list = overrides_by_service.get(s["_id"], [])
        default_pricing = next((ov for ov in ov_list if ov.get("customer_id") is None), None)
        s["default_agent_pricing"] = {
            "offers": (default_pricing or {}).get("offers") or [],
            "offers_count": len((default_pricing or {}).get("offers") or []),
            "offers_summary": _offers_summary((default_pricing or {}).get("offers") or []),
            "updated_at": (default_pricing or {}).get("updated_at"),
        } if default_pricing else None
        s["customer_profits"] = [
            {
                "customer_id": str(ov["customer_id"]),
                "customer_name": _pricing_scope_label(ov, names_map),
                "offers": ov.get("offers") or [],
                "offers_count": len(ov.get("offers") or []),
                "offers_summary": _offers_summary(ov.get("offers") or []),
                "updated_at": ov.get("updated_at"),
            }
            for ov in sorted(
                [x for x in ov_list if x.get("customer_id") is not None],
                key=lambda x: (names_map.get(x.get("customer_id"), ""), str(x.get("customer_id"))),
            )
        ]

    users_cursor = users_col.find(
        {"role": "customer"},
        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "business_name": 1}
    ).sort([("first_name", 1), ("last_name", 1)])

    customers = [{"_id": str(u["_id"]), "name": _display_name(u)} for u in users_cursor]

    return render_template("admin_services.html", services=services, customers=customers)

@admin_services_bp.route("/admin/services/create", methods=["POST"])
def create_service():
    if not _require_admin():
        return redirect(url_for("login.login"))

    service_name = (request.form.get("service_name") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    service_type = _norm_type(request.form.get("service_type")) or "API"
    provider = _norm_provider(request.form.get("provider")) or "skplug"

    if not service_name:
        flash("Service name is required.", "danger")
        return redirect(url_for("admin_services.manage_services"))
    if not image_url:
        flash("Please upload/select an image for the service.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    offers = _parse_offers(request, "offers")

    # NEW: optionally copy default to store/public on create
    copy_default_to_store = (request.form.get("copy_default_to_store") or "").strip()
    copy_default_to_public = (request.form.get("copy_default_to_public") or "").strip()
    store_offers = offers if copy_default_to_store else []
    public_offers = offers if copy_default_to_public else []

    doc = {
        "name": service_name,
        "image_url": image_url,
        "offers": offers,
        "store_offers": store_offers,  # NEW
        "public_offers": public_offers,
        "type": service_type,
        "provider": provider,
        "status": "OPEN",
        "availability": "AVAILABLE",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    services_col.insert_one(doc)
    flash("Service added successfully.", "success")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/update", methods=["POST"])
def update_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        _id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    service = services_col.find_one({"_id": _id})
    if not service:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    service_name = (request.form.get("service_name") or service.get("name") or "").strip()
    image_url = (request.form.get("image_url") or service.get("image_url") or "").strip()
    service_type = _norm_type(request.form.get("service_type"))
    provider = _norm_provider(request.form.get("provider")) or service.get("provider") or "skplug"

    if not service_name:
        flash("Service name is required.", "danger")
        return redirect(url_for("admin_services.manage_services"))
    if not image_url:
        flash("Please upload/select an image for the service.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    # Preserve default offers when the pricing-only edit form does not submit them.
    offers = _parse_offers(request, "offers") if "offers_amount[]" in request.form else (service.get("offers") or [])
    store_offers = _parse_offers(request, "store_offers")
    public_offers = _parse_offers(request, "public_offers")

    update_doc = {
        "name": service_name,
        "image_url": image_url,
        "offers": offers,
        "store_offers": store_offers,  # NEW
        "public_offers": public_offers,
        "updated_at": datetime.utcnow()
    }
    if service_type:
        update_doc["type"] = service_type
    if provider:
        update_doc["provider"] = provider

    services_col.update_one({"_id": _id}, {"$set": update_doc})
    flash("Service updated successfully.", "success")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/delete", methods=["POST"])
def delete_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        _id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    svc = services_col.find_one({"_id": _id})
    res = services_col.delete_one({"_id": _id})

    if res.deleted_count:
        try:
            if svc and isinstance(svc.get("image_url"), str) and svc["image_url"].startswith("/uploads/"):
                _ensure_upload_folder()
                fname = svc["image_url"].replace("/uploads/", "")
                fpath = os.path.join(UPLOAD_FOLDER, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
        except Exception:
            pass
        service_profits_col.delete_many({"service_id": _id})
        flash("Service deleted.", "info")
    else:
        flash("Service not found or already deleted.", "warning")

    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/upload_service_image", methods=["POST"])
def upload_service_image():
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No file part 'image'"}), 400

    file = request.files["image"]
    if not file or file.filename.strip() == "":
        return jsonify({"success": False, "error": "No selected file"}), 400

    if not _allowed_file(file.filename):
        return jsonify({"success": False, "error": "Invalid file type"}), 400

    _ensure_upload_folder()

    base, ext = os.path.splitext(secure_filename(file.filename))
    filename = f"{base}_{uuid.uuid4().hex[:8]}{ext.lower()}"
    target_path = os.path.join(UPLOAD_FOLDER, filename)

    file.save(target_path)
    file_url = f"/uploads/{filename}"
    return jsonify({"success": True, "url": file_url}), 200

# =======================
#   CUSTOMER PRICING
# =======================
@admin_services_bp.route("/admin/services/<service_id>/profit/customer", methods=["POST"])
def set_customer_profit_for_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    pricing_scope = (request.form.get("pricing_scope") or "customer").strip().lower()
    customer_id_raw = (request.form.get("customer_id") or "").strip()
    c_id = None
    if pricing_scope == "customer":
        try:
            c_id = ObjectId(customer_id_raw)
        except Exception:
            flash("Choose a customer before saving customer-specific prices.", "danger")
            return redirect(url_for("admin_services.manage_services"))

        customer = users_col.find_one({"_id": c_id, "role": "customer"})
        if not customer:
            flash("Customer not found.", "warning")
            return redirect(url_for("admin_services.manage_services"))

    service = services_col.find_one({"_id": s_id}, {"offers": 1})
    if not service:
        flash("Service not found.", "warning")
        return redirect(url_for("admin_services.manage_services"))

    offers = _parse_customer_offer_prices(request, service)
    if not offers:
        flash("Enter at least one manual agent price before saving.", "warning")
        return redirect(url_for("admin_services.manage_services"))

    now = datetime.utcnow()
    service_profits_col.update_one(
        {"service_id": s_id, "customer_id": c_id},
        {
            "$set": {"offers": offers, "updated_at": now},
            "$unset": {"profit_percent": ""},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True
    )
    flash("Default pricing for all agents updated." if c_id is None else "Customer pricing for service updated.", "success")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/profit/customer/<customer_id>/delete", methods=["POST"])
def delete_customer_profit_for_service(service_id, customer_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        s_id = ObjectId(service_id)
        c_id = None if customer_id == "__all__" else ObjectId(customer_id)
    except Exception:
        flash("Invalid id(s).", "danger")
        return redirect(url_for("admin_services.manage_services"))

    res = service_profits_col.delete_one({"service_id": s_id, "customer_id": c_id})
    if res.deleted_count:
        flash("Customer pricing override removed.", "info")
    else:
        flash("Override not found.", "warning")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/api/services/<service_id>/profit", methods=["GET"])
def get_effective_profit(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        s_id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    service = services_col.find_one({"_id": s_id}, {"offers": 1})
    if not service:
        return jsonify({"success": False, "error": "Service not found"}), 404

    customer_id = request.args.get("customer_id")
    c_id = None
    if customer_id:
        try:
            c_id = ObjectId(customer_id)
        except Exception:
            return jsonify({"success": False, "error": "Invalid customer id"}), 400

    override = service_profits_col.find_one({"service_id": s_id, "customer_id": c_id}) if c_id else None
    return jsonify(
        {
            "success": True,
            "service_id": str(s_id),
            "customer_id": str(c_id) if c_id else None,
            "offers": (override or {}).get("offers") or [],
        }
    )

@admin_services_bp.route("/api/pricing/quote", methods=["GET"])
def quote_price():
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    service_id = request.args.get("service_id")
    amount = _to_float(request.args.get("amount"))
    customer_id = request.args.get("customer_id")

    if not service_id or amount is None:
        return jsonify({"success": False, "error": "service_id and amount are required"}), 400

    try:
        s_id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    service = services_col.find_one({"_id": s_id})
    if not service:
        return jsonify({"success": False, "error": "Service not found"}), 404

    c_id = None
    if customer_id:
        try:
            c_id = ObjectId(customer_id)
        except Exception:
            return jsonify({"success": False, "error": "Invalid customer id"}), 400

    override = service_profits_col.find_one({"service_id": s_id, "customer_id": c_id}) if c_id else None
    chosen_total = None
    if override and isinstance(override.get("offers"), list):
        for offer in override.get("offers") or []:
            total = _to_float(offer.get("total"))
            if total is not None and abs(float(total) - float(amount)) < 0.0001:
                chosen_total = round(float(total), 2)
                break
    if chosen_total is None:
        chosen_total = round(float(amount), 2)
    return jsonify({"success": True, "data": {"amount": round(float(amount), 2), "profit": 0.0, "total": chosen_total}})

@admin_services_bp.route("/admin/services/<service_id>/type", methods=["POST"])
def set_service_type(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    desired_raw = request.form.get("type")
    if desired_raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        desired_raw = payload.get("type")
    desired = _norm_type(desired_raw)

    if not desired:
        return jsonify({"success": False, "error": "type must be 'API' or 'OFF'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"type": desired, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "type": desired})

def _norm_status_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"open", "1", "true", "on", "yes"}:
        return "OPEN"
    if s in {"closed", "0", "false", "off", "no"}:
        return "CLOSED"
    return None

def _norm_availability_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"available", "in_stock", "instock", "1", "true", "on", "yes"}:
        return "AVAILABLE"
    if s in {"out_of_stock", "outofstock", "oos", "unavailable", "0", "false", "off", "no"}:
        return "OUT_OF_STOCK"
    return None

@admin_services_bp.route("/admin/services/<service_id>/status", methods=["POST"])
def set_service_status(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("status")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("status")

    status_val = _norm_status_flag(raw)
    if not status_val:
        return jsonify({"success": False, "error": "status must be 'OPEN' or 'CLOSED'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"status": status_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "status": status_val})

@admin_services_bp.route("/admin/services/<service_id>/availability", methods=["POST"])
def set_service_availability(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("availability")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("availability")

    avail_val = _norm_availability_flag(raw)
    if not avail_val:
        return jsonify({"success": False, "error": "availability must be 'AVAILABLE' or 'OUT_OF_STOCK'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"availability": avail_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "availability": avail_val})

@admin_services_bp.route("/admin/services/<service_id>/provider", methods=["POST"])
def set_service_provider(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("provider")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("provider")

    provider_val = _norm_provider(raw)
    if not provider_val:
        return jsonify({"success": False, "error": "provider must be 'justice' or 'skplug'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"provider": provider_val, "updated_at": datetime.utcnow()}},
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "provider": provider_val})
