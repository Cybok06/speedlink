# login.py
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from db import db
from werkzeug.security import check_password_hash
from datetime import datetime
import re
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

login_bp = Blueprint("login", __name__)
users_col = db["users"]
login_logs_col = db["login_logs"]

# --- Configs for IP lookup (best-effort; won’t block login) ---
ENABLE_IP_LOOKUP = True
IP_LOOKUP_TIMEOUT = 4.0  # seconds
VERIFY_SSL = False       # avoids custom CA issues in your environment


# ---------------------------
# Helpers
# ---------------------------

_PRIVATE_NETS = (
    re.compile(r"^(127\.0\.0\.1)$"),
    re.compile(r"^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$"),
    re.compile(r"^192\.168\.\d{1,3}\.\d{1,3}$"),
    re.compile(r"^172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}$"),
    re.compile(r"^::1$"),
)

def _is_private_ip(ip: str) -> bool:
    ip = (ip or "").strip()
    if not ip:
        return True
    return any(p.match(ip) for p in _PRIVATE_NETS)

def get_client_ip() -> str:
    """
    Try to get the real client IP honoring proxies.
    """
    xfwd = (request.headers.get("X-Forwarded-For") or "").strip()
    if xfwd:
        # X-Forwarded-For: client, proxy1, proxy2
        first = xfwd.split(",")[0].strip()
        if first:
            return first
    xreal = (request.headers.get("X-Real-IP") or "").strip()
    if xreal:
        return xreal
    return request.remote_addr or ""

def build_device_info() -> dict:
    """
    Parse basic device info from Werkzeug's user_agent and headers.
    Kept simple (no external ua parser).
    """
    ua = request.user_agent
    ua_str = request.headers.get("User-Agent", "")
    s = ua_str.lower()

    is_tablet = ("ipad" in s) or ("tablet" in s)
    is_mobile = ("mobile" in s or "android" in s or "iphone" in s) and not is_tablet
    is_pc = not (is_mobile or is_tablet)

    return {
        "ua_string": ua_str,
        "browser": ua.browser or None,
        "version": ua.version or None,
        "platform": ua.platform or None,  # e.g., 'linux', 'macos', 'windows'
        "language": getattr(ua, "language", None),
        "accepted_languages": [l[0] for l in request.accept_languages] if request.accept_languages else [],
        "is_mobile": is_mobile,
        "is_tablet": is_tablet,
        "is_pc": is_pc,
        "device_label": "mobile" if is_mobile else ("tablet" if is_tablet else "desktop"),
    }

def lookup_ip_location(ip: str) -> dict:
    """
    Best-effort geolocation. Uses public endpoints with short timeouts.
    Skips private/local IPs. Never raises; returns a dict.
    """
    base = {
        "ip": ip,
        "is_private": _is_private_ip(ip),
        "source": None,
        "city": None,
        "region": None,
        "country": None,
        "country_name": None,
        "latitude": None,
        "longitude": None,
        "timezone": None,
    }

    if not ENABLE_IP_LOOKUP or base["is_private"] or not ip:
        return base

    # Try ipapi.co first
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=IP_LOOKUP_TIMEOUT, verify=VERIFY_SSL)
        if r.ok:
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j:
                base.update({
                    "source": "ipapi.co",
                    "city": j.get("city"),
                    "region": j.get("region"),
                    "country": j.get("country"),
                    "country_name": j.get("country_name"),
                    "latitude": j.get("latitude"),
                    "longitude": j.get("longitude"),
                    "timezone": j.get("timezone"),
                })
                return base
    except Exception:
        pass

    # Fallback ipinfo.io
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=IP_LOOKUP_TIMEOUT, verify=VERIFY_SSL)
        if r.ok:
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j:
                loc = j.get("loc", "")  # "lat,lon"
                lat, lon = (None, None)
                if isinstance(loc, str) and "," in loc:
                    try:
                        lat_s, lon_s = loc.split(",", 1)
                        lat = float(lat_s)
                        lon = float(lon_s)
                    except Exception:
                        pass
                base.update({
                    "source": "ipinfo.io",
                    "city": j.get("city"),
                    "region": j.get("region"),
                    "country": j.get("country"),
                    "country_name": j.get("country"),  # ipinfo lacks full name on free tier
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": j.get("timezone"),
                })
                return base
    except Exception:
        pass

    return base


def _lookup_user(login_id: str):
    """
    Accept username or email, with a case-insensitive exact fallback.
    """
    login_id = (login_id or "").strip()
    if not login_id:
        return None

    user = users_col.find_one({"username": login_id})
    if user:
        return user

    lowered = login_id.lower()
    user = users_col.find_one({"email": lowered})
    if user:
        return user

    escaped_login = re.escape(login_id)
    escaped_email = re.escape(lowered)
    return users_col.find_one(
        {
            "$or": [
                {"username": {"$regex": f"^{escaped_login}$", "$options": "i"}},
                {"email": {"$regex": f"^{escaped_email}$", "$options": "i"}},
            ]
        }
    )


def _verify_password(stored_password: str, provided_password: str) -> bool:
    """
    Support Werkzeug hashes plus compatibility fallbacks for copied whitespace
    and any older/plain stored credentials.
    """
    stored = (stored_password or "").strip()
    if not stored:
        return False

    raw = provided_password or ""
    candidates = []
    for candidate in (raw, raw.strip(), re.sub(r"\s+", "", raw)):
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            if check_password_hash(stored, candidate):
                return True
        except Exception:
            pass
        if stored == candidate:
            return True

    return False


def log_login_event(user: dict, success: bool, reason: str = "") -> None:
    """
    Inserts a login log document. Never raises to caller.
    """
    try:
        ip = get_client_ip()
        log_doc = {
            "user_id": user.get("_id") if user else None,
            "username": user.get("username") if user else None,
            "role": (user or {}).get("role", "customer"),
            "success": bool(success),
            "reason": reason or None,
            "ip": ip,
            "forwarded_for": request.headers.get("X-Forwarded-For"),
            "x_real_ip": request.headers.get("X-Real-IP"),
            "user_agent": request.headers.get("User-Agent"),
            "device": build_device_info(),
            "location": lookup_ip_location(ip),
            "created_at": datetime.utcnow(),
        }
        login_logs_col.insert_one(log_doc)
    except Exception:
        # Swallow all errors; logging must not break login flow.
        pass


# ---------------------------
# Keep sessions permanent while logged in
# ---------------------------

@login_bp.before_app_request
def _keep_permanent_session():
    """
    Runs before every request (any blueprint).
    If a user is logged in, ensure the session remains 'permanent'
    so the cookie keeps its expiration (set by PERMANENT_SESSION_LIFETIME).
    """
    if session.get("user_id"):
        session.permanent = True


# ---------------------------
# Routes
# ---------------------------

@login_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        # Find by username/email with a case-insensitive fallback
        user = _lookup_user(username)

        # Invalid credentials (user missing or password mismatch)
        if (not user) or (not _verify_password(user.get("password", ""), password)):
            log_login_event(user or {"username": username, "role": "unknown"}, success=False, reason="invalid_credentials")
            flash("❌ Invalid username or password", "danger")
            return redirect(url_for("login.login"))

        # Blocked status check AFTER password is correct
        status = (user.get("status") or "active").lower()
        if status == "blocked":
            # Log a blocked login attempt and refuse to create a session
            log_login_event(user, success=False, reason="blocked")
            flash("🚫 Your account is blocked. Please contact support.", "danger")
            return redirect(url_for("login.login"))

        # Require admin approval for customers before login
        if user.get("role") != "admin" and status != "active":
            reason = "pending_approval" if status in ("pending", "inactive") else "not_active"
            log_login_event(user, success=False, reason=reason)
            flash("Your account is pending approval. Please wait for admin approval.", "warning")
            return redirect(url_for("login.login"))

        # Successful auth (active or missing status treated as active)
        session.clear()
        session["user_id"] = str(user["_id"])
        session["username"] = user["username"]
        session["role"] = user.get("role", "customer")
        session.permanent = True  # <- critical: sets cookie expiration
        session.modified = True

        # Log success before redirect
        log_login_event(user, success=True)

        # Role-based redirect
        if session["role"] == "admin":
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard.admin_dashboard"))
        else:
            session["customer_logged_in"] = True
            return redirect(url_for("customer_dashboard.customer_dashboard"))

    return render_template("login.html")


@login_bp.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully", "info")
    return redirect(url_for("login.login"))
