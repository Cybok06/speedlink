"""Monthly agent billing. Entitlements and deduplication update atomically."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from bson import ObjectId
from dateutil.relativedelta import relativedelta
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

from db import db
from paystack_config import get_paystack_keys

subscription_bp = Blueprint("subscription", __name__)
users = db["users"]
payments = db["subscription_payments"]
AMOUNT_PESEWAS = 1500
REFERENCE_PREFIX = "SL-SUB-"
GRACE_PERIOD = timedelta(hours=24)


def ensure_initial_due(user):
    """Start introductory grace once, on authenticated use, never on store visits."""
    if user and (user.get("subscription_expires_at") or user.get("subscription_initial_due_at")):
        return user
    if user and user.get("role", "customer") in {"customer", "agent"}:
        users.update_one({"_id": user["_id"], "subscription_initial_due_at": {"$exists": False},
                          "subscription_expires_at": None},
                         {"$set": {"subscription_initial_due_at": datetime.utcnow()}})
        return users.find_one({"_id": user["_id"]})
    return user


def subscription_state(user, now=None):
    now = now or datetime.utcnow()
    user = user or {}
    due = user.get("subscription_expires_at") or user.get("subscription_initial_due_at")
    grace_end = due + GRACE_PERIOD if isinstance(due, datetime) else None
    paid = is_active(user, now)
    in_grace = bool(not paid and grace_end and now < grace_end)
    return {"paid": paid, "in_grace": in_grace, "allowed": paid or in_grace,
            "due_at": due, "grace_end": grace_end,
            "label": "Active" if paid else ("Grace period" if in_grace else "Payment due")}


def has_access(user, now=None):
    return subscription_state(user, now)["allowed"]


def subscription_notice_context():
    if not session.get("user_id") or session.get("role") not in {"customer", "agent"}:
        return {}
    user = get_user(session["user_id"])
    return {"subscription_notice": subscription_state(user),
            "show_subscription_announcement": session.pop("subscription_announcement", False)}


def get_user(user_id):
    try:
        return users.find_one({"_id": ObjectId(str(user_id))})
    except (ValueError, TypeError):
        return None


def is_active(user, now=None):
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    expiry = user.get("subscription_expires_at")
    return isinstance(expiry, datetime) and expiry > (now or datetime.utcnow())


def store_is_active(store):
    return has_access(get_user(store.get("owner_id")))


def apply_payment(payment, data):
    """Accept only the exact server-created payment; safely retry after crashes."""
    reference = payment["_id"]
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Invalid payment metadata")
    if (data.get("status") != "success" or data.get("reference") != reference
            or data.get("amount") != AMOUNT_PESEWAS or data.get("currency") != "GHS"
            or metadata.get("purpose") != "agent_subscription"
            or metadata.get("user_id") != str(payment["user_id"])
            or (data.get("customer") or {}).get("email", "").lower() != payment["email"].lower()):
        raise ValueError("Payment has not succeeded or does not match this subscription")
    for _ in range(20):
        user = users.find_one({"_id": payment["user_id"]})
        if not user:
            raise ValueError("Account not found")
        if reference in user.get("subscription_payment_refs", []):
            break
        now = datetime.utcnow()
        old_expiry = user.get("subscription_expires_at")
        start = max(now, old_expiry) if isinstance(old_expiry, datetime) else now
        expiry = start + relativedelta(months=1)
        result = users.update_one(
            {"_id": user["_id"], "subscription_expires_at": old_expiry,
             "subscription_payment_refs": {"$ne": reference}},
            {"$set": {"subscription_expires_at": expiry, "subscription_updated_at": now},
             "$addToSet": {"subscription_payment_refs": reference}})
        if result.modified_count:
            break
    else:
        raise RuntimeError("Subscription update busy; retry verification")
    payments.update_one({"_id": reference, "status": {"$ne": "success"}}, {"$set": {
        "status": "success", "verified_at": datetime.utcnow(), "channel": data.get("channel")}})


def verify_payment(payment):
    secret = get_paystack_keys()["secret_key"]
    if not secret:
        raise ValueError("Payments are unavailable. Please contact support.")
    response = requests.get(
        "https://api.paystack.co/transaction/verify/" + payment["_id"],
        headers={"Authorization": "Bearer " + secret}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("status"):
        raise ValueError("Payment is not confirmed yet. Please check again shortly.")
    data = payload.get("data") or {}
    if data.get("status") in {"failed", "abandoned", "reversed"}:
        payments.update_one({"_id": payment["_id"], "status": {"$ne": "success"}},
                            {"$set": {"status": data["status"]}})
        raise ValueError("This payment was not completed. You can start a new payment.")
    apply_payment(payment, data)


@subscription_bp.before_request
def require_account():
    if request.endpoint == "subscription.webhook":
        return None
    user = get_user(session.get("user_id"))
    if not user:
        return redirect(url_for("login.login"))
    if user.get("role", "customer") not in ("customer", "agent"):
        abort(403)
    if request.method == "POST":
        token = session.get("subscription_csrf", "")
        if not token or not hmac.compare_digest(token, request.form.get("csrf_token", "")):
            abort(400, "Please reload the Subscription page and try again.")


@subscription_bp.route("/subscription")
def page():
    user = ensure_initial_due(get_user(session["user_id"]))
    session.setdefault("subscription_csrf", secrets.token_urlsafe(32))
    history = list(payments.find({"user_id": user["_id"]}).sort("created_at", -1).limit(20))
    response = render_template("subscription.html", user=user, active=is_active(user),
                               billing_state=subscription_state(user), history=history)
    return response, 200, {"Cache-Control": "no-store"}


@subscription_bp.route("/subscription/pay", methods=["POST"])
def pay():
    user = ensure_initial_due(get_user(session["user_id"]))
    email = (user.get("email") or "").strip()
    secret = get_paystack_keys()["secret_key"]
    if not email or not secret:
        flash("A valid account email and configured Paystack payments are required. Please contact support.", "danger")
        return redirect(url_for("subscription.page"))
    # Reuse an unfinished checkout to avoid accidental double payment on retries.
    pending = payments.find_one({"user_id": user["_id"], "status": "pending"}, sort=[("created_at", -1)])
    if pending:
        try:
            verify_payment(pending)
            flash("Payment confirmed. Your subscription is active.", "success")
            return redirect(url_for("subscription.page"))
        except ValueError:
            latest = payments.find_one({"_id": pending["_id"]}) or {}
            if latest.get("status") == "pending" and pending.get("authorization_url"):
                return redirect(pending["authorization_url"], code=303)
        except requests.RequestException:
            flash("Unable to check your previous payment. Please retry shortly.", "warning")
            return redirect(url_for("subscription.page"))
    reference = REFERENCE_PREFIX + secrets.token_hex(16)
    payments.insert_one({"_id": reference, "user_id": user["_id"], "email": email,
                         "amount": AMOUNT_PESEWAS, "status": "pending", "created_at": datetime.utcnow()})
    try:
        response = requests.post("https://api.paystack.co/transaction/initialize", headers={"Authorization": "Bearer " + secret},
            json={"email": email, "amount": AMOUNT_PESEWAS, "currency": "GHS", "reference": reference,
                  "callback_url": url_for("subscription.callback", _external=True),
                  "metadata": {"purpose": "agent_subscription", "user_id": str(user["_id"])}}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        checkout_url = (payload.get("data") or {}).get("authorization_url", "")
        parsed = urlparse(checkout_url)
        if not payload.get("status") or parsed.scheme != "https" or parsed.hostname != "checkout.paystack.com":
            raise ValueError("Unable to start payment")
        payments.update_one({"_id": reference}, {"$set": {"authorization_url": checkout_url}})
        return redirect(checkout_url, code=303)
    except (requests.RequestException, ValueError):
        flash("Paystack could not start checkout. Please try again shortly.", "danger")
        return redirect(url_for("subscription.page"))


def confirm(reference):
    payment = payments.find_one({"_id": reference, "user_id": ObjectId(session["user_id"])})
    if not payment:
        abort(404)
    try:
        verify_payment(payment)
        flash("Payment confirmed. Your dashboard and store are now available.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    except requests.RequestException:
        flash("Could not confirm payment yet. Use Check payment to retry without paying again.", "warning")
    return redirect(url_for("subscription.page"))


@subscription_bp.route("/subscription/callback")
def callback():
    return confirm(request.args.get("reference", ""))


@subscription_bp.route("/subscription/check", methods=["POST"])
def check():
    return confirm(request.form.get("reference", ""))


@subscription_bp.route("/subscription/webhook", methods=["POST"])
def webhook():
    secret = get_paystack_keys()["secret_key"]
    signature = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(secret.encode(), request.get_data(), hashlib.sha512).hexdigest()
    if not secret or not hmac.compare_digest(expected, signature):
        abort(401)
    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.success":
        data = event.get("data") or {}
        payment = payments.find_one({"_id": data.get("reference")})
        if payment:
            try:
                apply_payment(payment, data)
            except ValueError:
                abort(400)
    return "", 200


def enforce_subscription():
    """Gate authenticated agent operations, while preserving public/callback routes."""
    if not session.get("user_id") or not request.endpoint:
        return None
    if request.blueprint in {"subscription", "login", "signup", "reset", "index", "callback_response", "ussd", "ist"}:
        return None
    # The public index purchase flow checks phone eligibility before payment.
    # Exempt only this public endpoint, not the administrative phone controls.
    if request.endpoint in {"static", "brand_images", "uploaded_file", "healthz", "deposit.verify_transaction",
                            "admin_phone_numbers.phone_number_eligibility"}:
        return None
    if request.blueprint == "stores" and (
        request.path.startswith(("/s/", "/store-checkout/", "/api/store-availability/", "/api/store-email/", "/api/store-products/", "/api/store-order/", "/media/"))
        and (request.method == "GET" or request.path.startswith("/store-checkout/"))
        and not request.path.endswith("/mine")
    ):
        return None  # Public store availability is checked against its owner separately.
    user = ensure_initial_due(get_user(session["user_id"]))
    if not user:
        session.clear()
        return redirect(url_for("login.login"))
    if user.get("role", "customer") in {"customer", "agent"} and not has_access(user):
        if request.is_json or request.path.startswith("/api/") or request.method != "GET":
            return jsonify(success=False, code="subscription_required", message="Your monthly subscription is due.",
                           redirect_url=url_for("subscription.page")), 402
        return redirect(url_for("subscription.page"))
