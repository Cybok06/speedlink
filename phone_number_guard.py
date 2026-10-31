from __future__ import annotations

from datetime import datetime
import re
import time

from flask import jsonify, session

from db import db

orders_col = db["orders"]
settings_col = db["settings"]

_SETTING_KEY = "block_new_numbers"
_CACHE_TTL_SECONDS = 60
_settings_cache = {"value": False, "expires_at": 0.0}


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def is_block_new_numbers_enabled(force_refresh: bool = False) -> bool:
    now = time.time()
    if (not force_refresh) and _settings_cache["expires_at"] > now:
        return bool(_settings_cache["value"])

    doc = settings_col.find_one({"key": _SETTING_KEY}, {"value": 1})
    value = bool((doc or {}).get("value"))
    _settings_cache["value"] = value
    _settings_cache["expires_at"] = now + _CACHE_TTL_SECONDS
    return value


def set_block_new_numbers_enabled(enabled: bool) -> None:
    settings_col.update_one(
        {"key": _SETTING_KEY},
        {
            "$set": {
                "key": _SETTING_KEY,
                "value": bool(enabled),
                "updated_at": datetime.utcnow(),
                "updated_by": session.get("admin_id") or session.get("user_id"),
            },
            "$setOnInsert": {"created_at": datetime.utcnow()},
        },
        upsert=True,
    )
    _settings_cache["value"] = bool(enabled)
    _settings_cache["expires_at"] = time.time() + _CACHE_TTL_SECONDS


def has_prior_order(phone: str) -> bool:
    normalized = normalize_phone(phone)
    if not normalized:
        return False

    variants = {normalized}
    if normalized.startswith("0") and len(normalized) == 10:
        variants.add(f"233{normalized[1:]}")

    return orders_col.count_documents({"items.phone": {"$in": list(variants)}}, limit=1) > 0


def evaluate_phone_order_eligibility(phone: str) -> dict:
    normalized = normalize_phone(phone)
    if not normalized:
        return {
            "allowed": False,
            "normalized_phone": "",
            "has_prior_order": False,
            "block_new_numbers_enabled": is_block_new_numbers_enabled(),
            "message": "Invalid phone number.",
        }

    enabled = is_block_new_numbers_enabled()
    prior = has_prior_order(normalized)
    if enabled and not prior:
        return {
            "allowed": False,
            "normalized_phone": normalized,
            "has_prior_order": False,
            "block_new_numbers_enabled": True,
            "message": "Can't Place Order now, Please Contact Admin",
        }

    return {
        "allowed": True,
        "normalized_phone": normalized,
        "has_prior_order": prior,
        "block_new_numbers_enabled": enabled,
        "message": "",
    }


def eligibility_json_response(phone: str):
    result = evaluate_phone_order_eligibility(phone)
    return jsonify({"success": True, **result})
