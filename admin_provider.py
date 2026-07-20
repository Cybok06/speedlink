from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Tuple

import requests
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for


admin_provider_bp = Blueprint("admin_provider", __name__)

PROVIDER_NAME = (os.getenv("PROVIDER_DISPLAY_NAME") or "Campus Data").strip()


def _provider_api_key() -> str:
    return (os.getenv("CAMPUS_DATA_API_KEY") or "").strip()


def _provider_base_url() -> str:
    return (os.getenv("CAMPUS_DATA_BASE_URL") or "https://campus-data-2i8o.onrender.com").strip().rstrip("/")


def _provider_timeout() -> int:
    return int((os.getenv("CAMPUS_DATA_TIMEOUT") or "30").strip() or "30")


def _is_public_key(value: str) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("pk_")


def _is_secret_key(value: str) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("sk_")


def _provider_paystack_keys() -> Tuple[str, str]:
    public_key = (
        os.getenv("provider_publick_key")
        or os.getenv("provider_public_key")
        or os.getenv("PROVIDER_PUBLICK_KEY")
        or os.getenv("PROVIDER_PUBLIC_KEY")
        or ""
    ).strip()
    secret_key = (
        os.getenv("provider_secret_key")
        or os.getenv("PROVIDER_SECRET_KEY")
        or ""
    ).strip()

    if _is_secret_key(public_key) and _is_public_key(secret_key):
        public_key, secret_key = secret_key, public_key

    return public_key, secret_key


def _provider_headers() -> Dict[str, str]:
    token = _provider_api_key()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _require_admin():
    return session.get("role") == "admin"


def _provider_paystack_ready() -> bool:
    public_key, secret_key = _provider_paystack_keys()
    return _is_public_key(public_key) and _is_secret_key(secret_key)


def _parse_json_response(resp: requests.Response) -> Dict[str, Any]:
    text = resp.text or ""
    try:
        body = resp.json() if text.strip() else {}
    except Exception:
        body = {"raw": text} if text else {}
    return body if isinstance(body, dict) else {"data": body}


def _fetch_provider_balance() -> Dict[str, Any]:
    if not _provider_api_key():
        return {"ok": False, "http_status": 500, "message": "CAMPUS_DATA_API_KEY is not configured", "body": {}}

    url = f"{_provider_base_url()}/api/external/balance"
    try:
        resp = requests.get(url, headers=_provider_headers(), timeout=_provider_timeout())
        body = _parse_json_response(resp)
        ok = 200 <= resp.status_code < 300
        return {
            "ok": ok,
            "http_status": resp.status_code,
            "message": body.get("message") or body.get("error") or "",
            "body": body,
        }
    except requests.RequestException as exc:
        return {"ok": False, "http_status": 599, "message": str(exc), "body": {}}


def _fetch_provider_transactions(page: int, limit: int, tx_type: str, status: str) -> Dict[str, Any]:
    if not _provider_api_key():
        return {
            "ok": False,
            "http_status": 500,
            "message": "CAMPUS_DATA_API_KEY is not configured",
            "items": [],
            "pagination": {"page": page, "limit": limit, "has_next": False, "total_pages": 1, "total": 0},
            "body": {},
        }

    params: Dict[str, Any] = {"page": page, "limit": limit}
    if tx_type:
        params["type"] = tx_type
    if status:
        params["status"] = status

    url = f"{_provider_base_url()}/api/external/transactions"
    try:
        resp = requests.get(url, headers=_provider_headers(), params=params, timeout=_provider_timeout())
        body = _parse_json_response(resp)
        ok = 200 <= resp.status_code < 300

        items = []
        for key in ("items", "transactions", "data", "results"):
            val = body.get(key)
            if isinstance(val, list):
                items = val
                break

        total = body.get("total")
        if total is None:
            total = body.get("count")
        try:
            total = int(total)
        except Exception:
            total = None

        current_page = body.get("page", page)
        try:
            current_page = max(int(current_page), 1)
        except Exception:
            current_page = page

        current_limit = body.get("limit", limit)
        try:
            current_limit = max(int(current_limit), 1)
        except Exception:
            current_limit = limit

        total_pages = body.get("total_pages")
        if total_pages is None and total is not None:
            total_pages = max((total + current_limit - 1) // current_limit, 1)
        try:
            total_pages = max(int(total_pages), 1)
        except Exception:
            total_pages = current_page if current_page > 0 else 1

        has_next = body.get("has_next")
        if has_next is None:
            has_next = current_page < total_pages

        return {
            "ok": ok,
            "http_status": resp.status_code,
            "message": body.get("message") or body.get("error") or "",
            "items": items,
            "pagination": {
                "page": current_page,
                "limit": current_limit,
                "has_next": bool(has_next),
                "total_pages": total_pages,
                "total": total,
            },
            "body": body,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "http_status": 599,
            "message": str(exc),
            "items": [],
            "pagination": {"page": page, "limit": limit, "has_next": False, "total_pages": 1, "total": 0},
            "body": {},
        }


def _post_provider_topup(amount: float, date_value: str, paystack_reference: str) -> Dict[str, Any]:
    if not _provider_api_key():
        return {"ok": False, "http_status": 500, "message": "CAMPUS_DATA_API_KEY is not configured", "body": {}}

    payload = {
        "amount": round(float(amount), 2),
        "date": date_value,
        "paystack_reference": paystack_reference.strip(),
    }
    url = f"{_provider_base_url()}/api/external/balance/topup"
    try:
        resp = requests.post(url, headers=_provider_headers(), json=payload, timeout=_provider_timeout())
        body = _parse_json_response(resp)
        ok = 200 <= resp.status_code < 300
        if isinstance(body, dict):
            if "success" in body:
                ok = ok and body.get("success") is True
            elif "status" in body:
                status_val = body.get("status")
                ok = ok and (status_val is True or str(status_val).strip().lower() in {"success", "ok", "completed"})
        return {
            "ok": ok,
            "http_status": resp.status_code,
            "message": body.get("message") or body.get("error") or "",
            "body": body,
        }
    except requests.RequestException as exc:
        return {"ok": False, "http_status": 599, "message": str(exc), "body": {}}


def _provider_balance_value(balance_payload: Dict[str, Any]) -> Any:
    body = balance_payload.get("body") or {}
    for key in ("balance", "available_balance", "balance_remaining", "amount"):
        if key in body:
            return body.get(key)
    for block_key in ("data", "payload", "result"):
        block = body.get(block_key)
        if isinstance(block, dict):
            for key in ("balance", "available_balance", "balance_remaining", "amount"):
                if key in block:
                    return block.get(key)
    return None


def _verify_provider_paystack(reference: str) -> Dict[str, Any]:
    if not _provider_paystack_ready():
        return {"ok": False, "http_status": 500, "message": "Provider Paystack keys are not configured", "body": {}}

    url = f"https://api.paystack.co/transaction/verify/{reference.strip()}"
    _, secret_key = _provider_paystack_keys()
    headers = {"Authorization": f"Bearer {secret_key}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=_provider_timeout())
        body = _parse_json_response(resp)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        ok = bool(body.get("status")) and data.get("status") == "success"
        return {
            "ok": ok,
            "http_status": resp.status_code,
            "message": body.get("message") or data.get("gateway_response") or "",
            "body": body,
            "data": data,
        }
    except requests.RequestException as exc:
        return {"ok": False, "http_status": 599, "message": str(exc), "body": {}, "data": {}}


@admin_provider_bp.route("/admin/provider", methods=["GET"])
def provider_dashboard():
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except Exception:
        page = 1
    try:
        limit = max(min(int(request.args.get("limit", 20)), 100), 1)
    except Exception:
        limit = 20

    tx_type = (request.args.get("type") or "").strip()
    status = (request.args.get("status") or "").strip()

    balance_res = _fetch_provider_balance()
    tx_res = _fetch_provider_transactions(page, limit, tx_type, status)

    filters_query = {
        "type": tx_type,
        "status": status,
        "limit": limit,
    }

    return render_template(
        "admin_provider.html",
        provider_name=PROVIDER_NAME,
        provider_base_url=_provider_base_url(),
        balance_result=balance_res,
        balance_value=_provider_balance_value(balance_res),
        transactions=tx_res.get("items") or [],
        tx_result=tx_res,
        pagination=tx_res.get("pagination") or {},
        selected_type=tx_type,
        selected_status=status,
        selected_limit=limit,
        provider_paystack_pk=_provider_paystack_keys()[0],
        provider_paystack_ready=_provider_paystack_ready(),
        filters_query=filters_query,
    )


@admin_provider_bp.route("/admin/provider/topup/verify", methods=["POST"])
def provider_topup_verify():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    reference = (payload.get("reference") or "").strip()
    date_value = (payload.get("date") or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")

    try:
        amount = float(payload.get("amount") or 0)
    except Exception:
        amount = 0.0

    if amount <= 0:
        return jsonify({"success": False, "message": "Top up amount must be greater than zero."}), 400
    if not reference:
        return jsonify({"success": False, "message": "Paystack reference is required."}), 400

    verify_res = _verify_provider_paystack(reference)
    if not verify_res.get("ok"):
        return jsonify({
            "success": False,
            "message": verify_res.get("message") or "Paystack verification failed.",
            "verify": verify_res,
        }), 400

    data = verify_res.get("data") or {}
    paid_amount = round(float((data.get("amount") or 0) / 100.0), 2)
    if paid_amount + 0.0001 < round(float(amount), 2):
        return jsonify({
            "success": False,
            "message": f"Verified Paystack amount GHS {paid_amount:.2f} is less than requested top up GHS {amount:.2f}.",
            "verify": verify_res,
        }), 400

    topup_res = _post_provider_topup(amount, date_value, reference)
    if not topup_res.get("ok"):
        return jsonify({
            "success": False,
            "message": topup_res.get("message") or "Provider balance top up failed.",
            "verify": verify_res,
            "topup": topup_res,
        }), 502

    return jsonify({
        "success": True,
        "message": topup_res.get("message") or "Provider balance top up recorded successfully.",
        "verify": verify_res,
        "topup": topup_res,
    }), 200
