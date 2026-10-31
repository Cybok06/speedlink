from flask import Blueprint, flash, redirect, session, url_for

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/admin/settings", methods=["GET", "POST"])
def manage_api():
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    flash("Paystack keys are now managed from Profile & Security for stronger protection.", "info")
    return redirect(url_for("admin_profile.admin_profile"))
