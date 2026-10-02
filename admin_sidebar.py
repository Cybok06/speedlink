# admin_sidebar.py
from flask import Blueprint, session
from db import db

admin_sidebar_bp = Blueprint("admin_sidebar", __name__)

orders_col = db["orders"]
complaints_col = db["complaints"]   # change if your collection name differs
users_col = db["users"]
blocked_phone_numbers_col = db["blocked_phone_numbers"]


def _is_admin() -> bool:
    return session.get("role") == "admin"


@admin_sidebar_bp.app_context_processor
def inject_admin_counts():
    """
    Inject counts into all templates. If not an admin, expose zeros so templates stay safe.
    """
    if not _is_admin():
        return {
            "pending_orders_count": 0,
            "undelivered_orders_count": 0,
            "pending_complaints_count": 0,
            "pending_customers_count": 0,
            "blocked_phone_numbers_count": 0,
        }

    # Pending orders (if you use it anywhere else)
    try:
        pending_orders = orders_col.count_documents({"status": "pending"})
    except Exception:
        pending_orders = 0

    # Only orders that are currently PROCESSING
    try:
        undelivered_orders = orders_col.count_documents({"status": "processing"})
    except Exception:
        undelivered_orders = 0

    # Complaints that are pending
    try:
        pending_complaints = complaints_col.count_documents({"status": "pending"})
    except Exception:
        pending_complaints = 0

    # Customers pending approval
    try:
        pending_customers = users_col.count_documents({"role": "customer", "status": "pending"})
    except Exception:
        pending_customers = 0

    try:
        blocked_phone_numbers = blocked_phone_numbers_col.count_documents({"is_active": True})
    except Exception:
        blocked_phone_numbers = 0

    return {
        "pending_orders_count": pending_orders,
        "undelivered_orders_count": undelivered_orders,
        "pending_complaints_count": pending_complaints,
        "pending_customers_count": pending_customers,
        "blocked_phone_numbers_count": blocked_phone_numbers,
    }
