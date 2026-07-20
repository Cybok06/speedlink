from __future__ import annotations

import os
from datetime import datetime, timedelta
from flask import Flask, send_from_directory, session, request, redirect

# Load .env for non-secret things (e.g., Paystack keys)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from db import db  # required

from customer_dashboard import customer_dashboard_bp
from admin_dashboard import admin_dashboard_bp
from login import login_bp
from signup import signup_bp
from admin_customers import admin_customers_bp
from admin_services import admin_services_bp
from deposit import deposit_bp
from checkout import checkout_bp
from orders import orders_bp
from transactions import transactions_bp
from customer_profile import customer_profile_bp
from complaints import complaints_bp
from referral import referral_bp
from admin_orders import admin_orders_bp
from admin_transactions import admin_transactions_bp
from admin_paystack_topups import admin_paystack_topups_bp
from admin_complaints import admin_complaints_bp
from admin_referrals import admin_referrals_bp
from admin_balance import admin_balance_bp
from admin_wassce_checker import admin_wassce_checker_bp
from purchases import purchases_bp
from purchase_checker import purchase_checker_bp
from admin_purchases import admin_purchases_bp
from settings import settings_bp
from admin_sidebar import admin_sidebar_bp
from admin_profile import admin_profile_bp
from admin_provider import admin_provider_bp
from login_logs import login_logs_bp
from reset import reset_bp
from afa_routes import afa_bp
from admin_afa import admin_afa_bp
from cart_api import cart_api_bp
from check_status import check_status_bp
from shares import shares_bp
from admin_phone_numbers import admin_phone_numbers_bp
from backend.callbacks.callback_response import callback_response_bp
from routes.ussd import ussd_bp

# ✅ UPDATED: store blueprint now lives in routes/store_page.py
from routes.store_page import stores_bp
# ✅ IMPORTANT: importing this file attaches the create/api/media routes to stores_bp
import routes.store_create  # noqa: F401

from routes.customer_store import customer_store_bp
from routes.admin_store import admin_store_bp
from order_status import order_status_bp   # 👈 add this

# ✅ Use ABSOLUTE import (place index.py next to this file)
from index import index_bp
from paystack_config import get_paystack_keys

# === Collections ===
visits_col = db["visits"]

# === Hard-coded config ===
SECRET_KEY = "m2k4vTq3Jp9Qf7A1R6xZ0Hc8Uy4Nd5LbX3gE2sW7iK0tP9qL5rV8wC6Bn1Dz0Ya"  # 64+ chars; keep private
SESSION_DAYS = 90
SESSION_COOKIE_NAME = "nanogu_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"   # change to "Strict" if you don't embed cross-site
SESSION_COOKIE_SECURE = False     # set True if your site is HTTPS-only in production

# Read upload folder from env (fallback to ./uploads)
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.join(os.getcwd(), "uploads"))
BRAND_IMAGES_FOLDER = os.path.join(os.getcwd(), "images")
BRAND_LOGO_URL = os.getenv(
    "BRAND_LOGO_URL",
    "https://imagedelivery.net/h9fmMoa1o2c2P55TcWJGOg/2a190482-90cd-4b63-1cc0-fc0899168600/public",
)


def create_app():
    app = Flask(__name__)

    # --- Session / cookies (all hard-coded) ---
    app.secret_key = SECRET_KEY
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_DAYS)
    app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    app.config["SESSION_COOKIE_HTTPONLY"] = SESSION_COOKIE_HTTPONLY
    app.config["SESSION_COOKIE_SAMESITE"] = SESSION_COOKIE_SAMESITE
    app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE

    # Keep sessions permanent whenever user is logged in
    @app.before_request
    def _keep_permanent_sessions():
        if session.get("user_id"):
            session.permanent = True

    # Count visits to "/" without adding a second route
    @app.before_request
    def _count_home_visits():
        if request.path == "/":
            try:
                visits_col.update_one(
                    {"_id": "global"},
                    {
                        "$inc": {"total": 1},
                        "$set": {"updated_at": datetime.utcnow()},
                        "$setOnInsert": {"created_at": datetime.utcnow()},
                    },
                    upsert=True,
                )
            except Exception as e:
                print(f"[visits] increment failed: {e}")

    # --- File uploads ---
    app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
    app.config["BRAND_IMAGES_FOLDER"] = BRAND_IMAGES_FOLDER
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(BRAND_IMAGES_FOLDER, exist_ok=True)

    @app.route("/images/<path:filename>")
    def brand_images(filename):
        if filename in {
            "logo_for_white_bg.png",
            "logo_for_blue_bg.png",
            "logo_text_for_white_bg.png",
            "logo_text_for_blue_bg.png",
        }:
            return redirect(BRAND_LOGO_URL, code=302)
        return send_from_directory(app.config["BRAND_IMAGES_FOLDER"], filename)

    # --- Blueprints ---
    app.register_blueprint(customer_dashboard_bp)
    app.register_blueprint(admin_dashboard_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(signup_bp)
    app.register_blueprint(admin_customers_bp)
    app.register_blueprint(admin_services_bp)
    app.register_blueprint(deposit_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(customer_profile_bp)
    app.register_blueprint(complaints_bp)
    app.register_blueprint(referral_bp)
    app.register_blueprint(admin_orders_bp)
    app.register_blueprint(admin_transactions_bp)
    app.register_blueprint(admin_paystack_topups_bp)
    app.register_blueprint(admin_complaints_bp)
    app.register_blueprint(admin_referrals_bp)
    app.register_blueprint(admin_balance_bp)
    app.register_blueprint(admin_wassce_checker_bp)
    app.register_blueprint(purchase_checker_bp)
    app.register_blueprint(purchases_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(admin_purchases_bp)
    app.register_blueprint(admin_sidebar_bp)
    app.register_blueprint(admin_profile_bp)
    app.register_blueprint(admin_provider_bp)
    app.register_blueprint(login_logs_bp)
    app.register_blueprint(reset_bp)
    app.register_blueprint(afa_bp)
    app.register_blueprint(admin_afa_bp)
    app.register_blueprint(cart_api_bp)  # no prefix; routes already start with /api/cart
    app.register_blueprint(index_bp)     # serves "/" dynamically with offers & public buy
    app.register_blueprint(check_status_bp)
    app.register_blueprint(shares_bp)
    app.register_blueprint(admin_phone_numbers_bp)
    app.register_blueprint(callback_response_bp)
    app.register_blueprint(ussd_bp)

    # ✅ Store module (public store page + create + CRUD + media) now comes from store_page/store_create split
    app.register_blueprint(stores_bp)

    app.register_blueprint(customer_store_bp)
    app.register_blueprint(admin_store_bp)
    app.register_blueprint(order_status_bp)   # 👈 add this

    # --- Jinja env injection ---
    @app.context_processor
    def inject_env():
        paystack_keys = get_paystack_keys()
        return {
            "PAYSTACK_PUBLIC_KEY": paystack_keys.get("public_key", ""),
            "COMPANY_NAME": os.getenv("COMPANY_NAME", "SpeedLink"),
            "BRAND_LOGO_URL": BRAND_LOGO_URL,
            "SUPPORT_PHONE": os.getenv("SUPPORT_PHONE", "0549869925"),
            "SUPPORT_EMAIL": os.getenv("SUPPORT_EMAIL", "deikoj17@gmail.com"),
            "SUPPORT_WHATSAPP": os.getenv("SUPPORT_WHATSAPP", "https://wa.me/233549869925"),
            "COMMUNITY_WHATSAPP": os.getenv(
                "COMMUNITY_WHATSAPP",
                "https://chat.whatsapp.com/GMzawMS1BpFEC3jW9pzkPJ?mode=gi_t",
            ),
        }

    # --- Utility routes ---
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    return app


# Gunicorn entrypoint: `gunicorn app:app`
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
