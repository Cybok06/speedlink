from flask import Blueprint, render_template, session, redirect, url_for, request
from bson import ObjectId
from pymongo import DESCENDING

from db import db

admin_paystack_topups_bp = Blueprint("admin_paystack_topups", __name__)

transactions_col = db["transactions"]
users_col = db["users"]

try:
    transactions_col.create_index(
        [("type", 1), ("gateway", 1), ("status", 1), ("verified_at", DESCENDING)],
        name="admin_paystack_topups_lookup_idx",
    )
except Exception:
    pass


def _display_customer_name(user: dict) -> str:
    first = (user or {}).get("first_name", "") or ""
    last = (user or {}).get("last_name", "") or ""
    full_name = f"{first} {last}".strip()
    if full_name:
        return full_name
    return (user or {}).get("username") or (user or {}).get("email") or "Unknown Customer"


@admin_paystack_topups_bp.route("/admin/paystack-topups")
def admin_paystack_topups():
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)

    per_page = 10
    query = {
        "type": "deposit",
        "gateway": "Paystack",
        "status": "success",
    }

    total_topups = transactions_col.count_documents(query)
    total_pages = max((total_topups + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    topups = list(
        transactions_col.find(query)
        .sort([("verified_at", -1), ("created_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(per_page)
    )

    user_ids = []
    for topup in topups:
        user_id = topup.get("user_id")
        if isinstance(user_id, ObjectId):
            user_ids.append(user_id)

    users_map = {}
    if user_ids:
        for user in users_col.find({"_id": {"$in": list(set(user_ids))}}):
            users_map[user["_id"]] = user

    for topup in topups:
        user = users_map.get(topup.get("user_id"), {}) or {}
        topup["user"] = user
        topup["customer_name"] = _display_customer_name(user)
        topup["customer_phone"] = (
            user.get("phone")
            or user.get("phone_normalized")
            or user.get("mobile")
            or "N/A"
        )
        topup["display_amount"] = float(topup.get("amount") or 0)
        topup["display_datetime"] = topup.get("verified_at") or topup.get("created_at")

    return render_template(
        "admin_paystack_topups.html",
        topups=topups,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_topups=total_topups,
    )
