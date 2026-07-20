from flask import Blueprint, render_template, session, redirect, url_for
from bson import ObjectId
from datetime import datetime, timedelta
from db import db

transactions_bp   = Blueprint("transactions", __name__)
transactions_col  = db["transactions"]

def _sum_amount(match):
    """Aggregate helper to sum 'amount' with a $match stage."""
    pipeline = [
        {"$match": match},
        {"$group": {"_id": None, "total": {"$sum": {"$toDouble": "$amount"}}}}
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return float(agg[0]["total"]) if agg else 0.0

@transactions_bp.route("/customer/transactions")
def view_transactions():
    if session.get("role") != "customer":
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    uid = ObjectId(user_id)

    # Time windows (UTC, aligning with how verified_at/created_at are stored)
    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)
    end_today = start_today + timedelta(days=1)

    # --- Totals ---
    # Total Topups Today: successful deposits verified today
    k_total_topups_today = _sum_amount({
        "user_id": uid,
        "type": "deposit",
        "status": "success",
        "verified_at": {"$gte": start_today, "$lt": end_today}
    })

    # Total Sales Today: successful purchases verified today
    k_total_sales_today = _sum_amount({
        "user_id": uid,
        "type": "purchase",
        "status": "success",
        "verified_at": {"$gte": start_today, "$lt": end_today}
    })

    # Lifetime Sales: successful purchases (all time)
    k_lifetime_sales = _sum_amount({
        "user_id": uid,
        "type": "purchase",
        "status": "success"
    })

    # Average Daily Sales (last 30 days, including today)
    start_30 = start_today - timedelta(days=29)  # 30-day window
    sales_last_30 = _sum_amount({
        "user_id": uid,
        "type": "purchase",
        "status": "success",
        "verified_at": {"$gte": start_30, "$lt": end_today}
    })
    k_avg_daily_sales = round(sales_last_30 / 30.0, 2)

    # Refunds Today: successful refunds verified today (if you record refunds)
    k_refunds_today = _sum_amount({
        "user_id": uid,
        "type": "refund",
        "status": "success",
        "verified_at": {"$gte": start_today, "$lt": end_today}
    })

    # Transactions list (newest first). Use verified_at, then created_at as fallback in template.
    transactions = list(
        transactions_col.find({"user_id": uid}).sort([("verified_at", -1), ("created_at", -1)])
    )

    return render_template(
        "transactions.html",
        transactions=transactions,
        k_total_topups_today=round(k_total_topups_today, 2),
        k_total_sales_today=round(k_total_sales_today, 2),
        k_lifetime_sales=round(k_lifetime_sales, 2),
        k_avg_daily_sales=round(k_avg_daily_sales, 2),
        k_refunds_today=round(k_refunds_today, 2),
    )
