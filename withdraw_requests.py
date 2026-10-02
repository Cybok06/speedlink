from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Tuple

from bson import ObjectId

from db import db

balances_col = db["balances"]
transactions_col = db["transactions"]
store_withdraw_requests_col = db["store_withdraw_requests"]


def _fmt_money(x: Any) -> float:
    try:
        return round(float(x or 0), 2)
    except Exception:
        return 0.0


def _make_reference(prefix: str, oid: ObjectId) -> str:
    d = datetime.utcnow().strftime("%Y%m%d")
    tail = str(oid)[-6:].upper()
    return f"{prefix}-{d}-{tail}"


def _coerce_actor_id(actor_id: Any):
    if actor_id is None:
        return None
    if isinstance(actor_id, ObjectId):
        return actor_id
    try:
        return ObjectId(str(actor_id))
    except Exception:
        return str(actor_id)


def update_withdraw_request_status(
    req_id: str,
    new_status: str,
    actor_id: Any,
    note: str = "",
) -> Tuple[bool, Dict[str, Any], int]:
    if not req_id:
        return False, {"message": "Missing request id"}, 400

    try:
        rid = ObjectId(req_id)
    except Exception:
        return False, {"message": "Invalid request id"}, 400

    raw_status = (new_status or "").strip().lower()
    alias = {
        "processing": "pending",
        "canceled": "rejected",
        "cancelled": "rejected",
        "canceld": "rejected",
    }
    status = alias.get(raw_status, raw_status)
    if status not in ("pending", "paid", "rejected"):
        return False, {"message": "Invalid status"}, 400

    req_doc = store_withdraw_requests_col.find_one({"_id": rid})
    if not req_doc:
        return False, {"message": "Request not found"}, 404

    old_status = str(req_doc.get("status") or "pending").lower()
    now = datetime.utcnow()
    actor = _coerce_actor_id(actor_id)
    note = (note or "").strip()

    if status == "paid":
        if old_status == "paid":
            store_withdraw_requests_col.update_one(
                {"_id": rid},
                {"$set": {"note": note, "updated_at": now}},
            )
            return True, {"message": "Already paid", "status": "paid"}, 200

        store_withdraw_requests_col.update_one(
            {"_id": rid},
            {"$set": {
                "status": "paid",
                "note": note,
                "paid_at": now,
                "paid_by": actor,
                "updated_at": now,
            }},
        )

        existing_tx = transactions_col.find_one({
            "type": "store_withdrawal",
            "meta.request_id": str(rid),
            "status": "success",
        })
        if existing_tx:
            return True, {"message": "Paid (transaction already exists)", "status": "paid"}, 200

        amount = _fmt_money(req_doc.get("amount"))
        method = str(req_doc.get("method") or "").lower()
        store_slug = req_doc.get("store_slug")
        owner_id = req_doc.get("owner_id")

        if method == "wallet":
            balances_col.update_one(
                {"user_id": owner_id},
                {"$inc": {"amount": amount}, "$setOnInsert": {"created_at": now}, "$set": {"updated_at": now}},
                upsert=True,
            )

        transactions_col.insert_one({
            "type": "store_withdrawal",
            "status": "success",
            "amount": amount,
            "reference": req_doc.get("reference") or _make_reference("WD", rid),
            "created_at": now,
            "verified_at": now,
            "meta": {
                "store_slug": store_slug,
                "owner_id": str(owner_id),
                "request_id": str(rid),
                "method": method,
                "note": note or ("Credited to wallet" if method == "wallet" else "Paid to MoMo"),
                "payout_snapshot": req_doc.get("payout_snapshot") if method == "momo" else None,
            },
        })

        return True, {"message": "Marked paid", "status": "paid"}, 200

    store_withdraw_requests_col.update_one(
        {"_id": rid},
        {"$set": {"status": status, "note": note, "updated_at": now, "updated_by": actor}},
    )
    return True, {"message": f"Marked {status}", "status": status}, 200
