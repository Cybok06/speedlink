"""Monthly subscription receipts and current agent access for administrators."""
from datetime import datetime
import re

from dateutil.relativedelta import relativedelta
from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from subscriptions import get_user, payments, subscription_state, users

admin_subscriptions_bp = Blueprint("admin_subscriptions", __name__)


@admin_subscriptions_bp.route("/admin/subscriptions")
def page():
    admin = get_user(session.get("user_id"))
    if not admin:
        return redirect(url_for("login.login"))
    if admin.get("role") != "admin":
        abort(403)
    now = datetime.utcnow()
    month = request.args.get("month", now.strftime("%Y-%m"))
    try:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError()
        start = datetime.strptime(month, "%Y-%m")
        end = start + relativedelta(months=1)
    except (ValueError, OverflowError):
        abort(400, "Choose a valid month in YYYY-MM format.")
    status = request.args.get("status", "all")
    if status not in {"all", "paid", "unpaid", "grace"}:
        abort(400, "Invalid subscription filter.")
    search = request.args.get("q", "").strip()[:100]
    # Receipt dates remain immutable on webhook/callback retries.
    totals = list(payments.aggregate([
        {"$match": {"status": "success", "verified_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$user_id", "amount": {"$sum": "$amount"},
                     "count": {"$sum": 1}, "last_paid": {"$max": "$verified_at"}}}]))
    receipts = {item["_id"]: item for item in totals}
    rows = []
    # Historical month excludes accounts created later; old accounts without a creation date are included.
    agents = users.find({"role": {"$in": ["customer", "agent"]}, "status": {"$ne": "deleted"},
                        "$or": [{"created_at": {"$lt": end}}, {"created_at": {"$exists": False}}, {"created_at": None}]},
                       {"username": 1, "first_name": 1, "last_name": 1, "email": 1, "phone": 1,
                        "subscription_expires_at": 1, "subscription_initial_due_at": 1})
    for agent in agents:
        receipt = receipts.get(agent["_id"], {})
        name = (" ".join([agent.get("first_name") or "", agent.get("last_name") or ""]).strip()
                or agent.get("username") or agent.get("email") or "Agent")
        rows.append({"user": agent, "name": name, "paid": bool(receipt),
                     "amount": receipt.get("amount", 0) / 100, "last_paid": receipt.get("last_paid"),
                     "state": subscription_state(agent, now)})
    summary = {"agents": len(rows), "paid": sum(row["paid"] for row in rows),
               "unpaid": sum(not row["paid"] for row in rows),
               "grace": sum(row["state"]["in_grace"] for row in rows),
               "revenue": sum(item["amount"] for item in totals) / 100,
               "payments": sum(item["count"] for item in totals)}
    filtered = [row for row in rows if
                (status == "all" or status == "paid" and row["paid"] or status == "unpaid" and not row["paid"]
                 or status == "grace" and row["state"]["in_grace"])
                and (not search or search.casefold() in " ".join([row["name"], row["user"].get("email") or "",
                                                                 row["user"].get("phone") or ""]).casefold())]
    filtered.sort(key=lambda row: (row["name"].casefold(), str(row["user"]["_id"])))
    page_count = max(1, (len(filtered) + 24) // 25)
    page_number = min(page_count, max(1, request.args.get("page", 1, type=int) or 1))
    return render_template("admin_subscriptions.html", rows=filtered[(page_number-1)*25:page_number*25],
                           summary=summary, month=month, month_label=start.strftime("%B %Y"), status=status,
                           search=search, page=page_number, page_count=page_count, total=len(filtered)), 200, {"Cache-Control": "no-store"}
