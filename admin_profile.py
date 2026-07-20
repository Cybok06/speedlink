from datetime import datetime
import re

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import db
from paystack_config import get_paystack_keys, is_public_key, is_secret_key, save_paystack_keys


admin_profile_bp = Blueprint("admin_profile", __name__)
users_col = db["users"]

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,}$")


@admin_profile_bp.route("/admin/profile", methods=["GET", "POST"])
def admin_profile():
    if not session.get("admin_logged_in") or session.get("role") != "admin":
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    try:
        admin_object_id = ObjectId(user_id)
    except Exception:
        flash("Invalid admin session. Please log in again.", "danger")
        return redirect(url_for("login.logout"))

    admin = users_col.find_one({"_id": admin_object_id, "role": "admin"})
    if not admin:
        flash("Admin account not found.", "danger")
        return redirect(url_for("login.login"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "update_profile":
            new_username = (request.form.get("new_username") or "").strip()
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            current_password = request.form.get("current_password") or ""

            if not new_username:
                flash("Username is required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not _USERNAME_RE.fullmatch(new_username):
                flash("Invalid username format.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not name:
                flash("Name is required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not email:
                flash("Email is required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not check_password_hash(admin.get("password", ""), current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if users_col.find_one({"username": new_username, "_id": {"$ne": admin_object_id}}):
                flash("Username already exists.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if users_col.find_one({"email": email, "_id": {"$ne": admin_object_id}}):
                flash("Email already exists.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))

            users_col.update_one(
                {"_id": admin_object_id},
                {
                    "$set": {
                        "username": new_username,
                        "name": name,
                        "email": email,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            session["username"] = new_username
            flash("Profile updated successfully.", "success")
            return redirect(url_for("admin_profile.admin_profile"))

        if action == "change_password":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""

            if not check_password_hash(admin.get("password", ""), current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not new_password or not confirm_password:
                flash("New password and confirmation are required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if new_password != confirm_password:
                flash("New passwords do not match.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))

            users_col.update_one(
                {"_id": admin_object_id},
                {"$set": {"password": generate_password_hash(new_password), "updated_at": datetime.utcnow()}}
            )
            flash("Password updated successfully.", "success")
            return redirect(url_for("admin_profile.admin_profile"))

        if action == "update_paystack_keys":
            current_password = request.form.get("current_password") or ""
            public_key = (request.form.get("paystack_public_key") or "").strip()
            secret_key = (request.form.get("paystack_secret_key") or "").strip()

            if not check_password_hash(admin.get("password", ""), current_password):
                flash("Admin password is required to change Paystack keys.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not public_key or not secret_key:
                flash("Both Paystack public and secret keys are required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not is_public_key(public_key):
                flash("Paystack public key must start with 'pk_'.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not is_secret_key(secret_key):
                flash("Paystack secret key must start with 'sk_'.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))

            save_paystack_keys(public_key, secret_key, updated_by=str(admin_object_id))
            flash("Paystack keys updated successfully.", "success")
            return redirect(url_for("admin_profile.admin_profile"))

        flash("Unknown action.", "danger")
        return redirect(url_for("admin_profile.admin_profile"))

    return render_template("admin_profile.html", admin=admin, paystack_keys=get_paystack_keys())
