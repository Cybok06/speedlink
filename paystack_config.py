from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

from db import db

app_settings_col = db["app_settings"]

PAYSTACK_SETTINGS_ID = "PAYSTACK_KEYS"

ENV_PAYSTACK_PUBLIC_KEY = (os.getenv("PAYSTACK_PUBLIC_KEY", "") or os.getenv("PAYSTACK_PK", "")).strip()
ENV_PAYSTACK_SECRET_KEY = (os.getenv("PAYSTACK_SECRET_KEY", "") or os.getenv("PAYSTACK_SK", "")).strip()


def _clean_key(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def is_public_key(value: str) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("pk_")


def is_secret_key(value: str) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("sk_")


def normalize_paystack_keys(public_key: str, secret_key: str) -> Dict[str, str]:
    public_key = _clean_key(public_key)
    secret_key = _clean_key(secret_key)

    if is_secret_key(public_key) and is_public_key(secret_key):
        public_key, secret_key = secret_key, public_key

    if not is_public_key(public_key) and is_public_key(secret_key):
        public_key = secret_key
    if not is_secret_key(secret_key) and is_secret_key(public_key):
        secret_key = public_key

    return {
        "public_key": public_key,
        "secret_key": secret_key,
    }


def mask_key(value: str) -> str:
    value = _clean_key(value)
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:7]}...{value[-5:]}"


def get_paystack_doc() -> Dict[str, Any] | None:
    return app_settings_col.find_one({"_id": PAYSTACK_SETTINGS_ID})


def get_paystack_keys() -> Dict[str, Any]:
    doc = get_paystack_doc() or {}
    raw_public = _clean_key(doc.get("public_key"))
    raw_secret = _clean_key(doc.get("secret_key"))

    source = "database"
    if not raw_public and not raw_secret:
        raw_public = ENV_PAYSTACK_PUBLIC_KEY
        raw_secret = ENV_PAYSTACK_SECRET_KEY
        source = "environment"

    normalized = normalize_paystack_keys(raw_public, raw_secret)
    public_key = normalized["public_key"]
    secret_key = normalized["secret_key"]

    return {
        "public_key": public_key,
        "secret_key": secret_key,
        "configured": bool(public_key and secret_key and is_public_key(public_key) and is_secret_key(secret_key)),
        "source": source,
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
        "public_key_masked": mask_key(public_key),
        "secret_key_masked": mask_key(secret_key),
    }


def save_paystack_keys(public_key: str, secret_key: str, updated_by: str | None = None) -> Dict[str, Any]:
    normalized = normalize_paystack_keys(public_key, secret_key)
    now = datetime.utcnow()
    doc = {
        "_id": PAYSTACK_SETTINGS_ID,
        "public_key": normalized["public_key"],
        "secret_key": normalized["secret_key"],
        "updated_at": now,
        "updated_by": updated_by,
    }
    app_settings_col.update_one({"_id": PAYSTACK_SETTINGS_ID}, {"$set": doc, "$setOnInsert": {"created_at": now}}, upsert=True)
    return doc
