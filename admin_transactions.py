from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from db import db
from datetime import datetime, timedelta

admin_transactions_bp = Blueprint("admin_transactions", __name__)

transactions_col = db["transactions"]
users_col = db["users"]


@admin_transactions_bp.route("/admin/transactions")
def admin_view_transactions():
    # Auth
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    customer_id = (request.args.get("customer") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()

    # pagination
    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)

    per_page = 10

    query = {}

    # Filter by customer
    if customer_id:
        try:
            query["user_id"] = ObjectId(customer_id)
        except Exception:
            flash("Invalid customer selected.", "warning")

    # Date range filter (verified_at)
    # - start_date: >= start_dt 00:00
    # - end_date: <= end_dt 23:59:59 by using end_dt + 1 day (exclusive upper bound)
    verified_filter = {}
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            verified_filter["$gte"] = start_dt
        except Exception:
            flash("Invalid start date.", "warning")

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            verified_filter["$lt"] = end_dt + timedelta(days=1)
        except Exception:
            flash("Invalid end date.", "warning")

    if verified_filter:
        query["verified_at"] = verified_filter

    # Count
    total_txns = transactions_col.count_documents(query)
    total_pages = max((total_txns + per_page - 1) // per_page, 1)

    # Clamp page to range (prevents dead pages when filters reduce results)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    # Fetch transactions
    transactions = list(
        transactions_col.find(query)
        .sort("verified_at", -1)
        .skip(skip)
        .limit(per_page)
    )

    # Load customers for dropdown
    customers = list(users_col.find({"role": "customer"}).sort("first_name", 1))

    # Attach user info efficiently
    user_ids = [t.get("user_id") for t in transactions if t.get("user_id")]
    users_map = {}
    if user_ids:
        for u in users_col.find({"_id": {"$in": list(set(user_ids))}}):
            users_map[u["_id"]] = u

    for txn in transactions:
        txn["user"] = users_map.get(txn.get("user_id"), {}) or {}
        txn_type = (txn.get("type") or "").lower()
        if txn_type == "deposit":
            txn["source_label"] = "Account Deposit"
        elif txn_type == "purchase":
            txn["source_label"] = "Order"
        else:
            txn["source_label"] = "Other"

    return render_template(
        "admin_transactions.html",
        transactions=transactions,
        customers=customers,
        selected_customer=customer_id,
        start_date=start_date,
        end_date=end_date,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
