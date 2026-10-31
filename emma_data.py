"""Emma Data purchase integration. Network mapping is shared with admin services."""

import os
import re
from decimal import Decimal, InvalidOperation

import requests


def network_for_service(service):
    """Match the five supported services, allowing spaces, hyphens and underscores."""
    name = re.sub(r"[^a-z0-9]", "", str((service or {}).get("name") or "").lower())
    return {
        "mtn": "YELLO", "mtnnormal": "YELLO", "mtnexpress": "EXPRESS_YELLO",
        "telecel": "TELECEL", "atishare": "at", "airteltigoishare": "at",
        "atbigtime": "AT_PREMIUM", "airteltigobigtime": "AT_PREMIUM",
    }.get(name)


def selected_provider(service):
    # Never take the routing selection from a customer-supplied cart.
    return str((service or {}).get("fulfillment_provider") or "default").strip().lower()


def capacity_in_gb(value_obj, raw_value):
    """Stored offer volumes are decimal MB; Emma expects a numeric GB string."""
    try:
        volume = (value_obj or {}).get("volume")
        if volume is not None:
            capacity = Decimal(str(volume)) / 1000
        else:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(GB|GIG|G|MB)\s*", str(raw_value or ""), re.I)
            if not match:
                return None
            capacity = Decimal(match[1]) / (1000 if match[2].upper() == "MB" else 1)
        if not capacity.is_finite() or capacity <= 0:
            return None
        return format(capacity.normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return None


def initial_order_status(items):
    """Preserve existing pending behavior unless every purchased line is delivered."""
    active = [item for item in items if not str(item.get("line_status") or "").startswith("skipped_")]
    if active and all(item.get("line_status") == "delivered" for item in active):
        return "delivered"
    return "pending"


def purchase(*, service, value_obj, raw_value, phone, reference):
    network = network_for_service(service)
    capacity = capacity_in_gb(value_obj, raw_value)
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("233") and len(digits) == 12:
        digits = "0" + digits[3:]
    elif len(digits) == 9:
        digits = "0" + digits
    payload = {"network": network, "capacity": capacity, "phoneNumber": digits, "ref": reference}
    result = {
        "provider": "emma_data", "provider_network": network,
        "provider_request_order_id": reference, "provider_reference": None,
        "provider_order_id": None, "line_status": "pending", "api_status": "submit_failed",
        "api_response": {"ok": False, "request": payload},
    }

    def fail(message, http_status=400, uncertain=False):
        result["api_response"].update(http_status=http_status, response={"message": message})
        if uncertain:
            result["api_status"] = "submission_unknown"
            result["provider_follow_up_required"] = True
        return result

    if not network:
        return fail("This service is not supported by Emma Data.")
    if not capacity:
        return fail("Could not derive a valid Emma Data capacity from the offer.")
    if not re.fullmatch(r"0\d{9}", digits):
        return fail("Emma Data requires a valid Ghana phone number.")
    api_key = (os.getenv("EMMA_DATA_API_KEY") or "").strip()
    if not api_key:
        return fail("EMMA_DATA_API_KEY is not configured.", 500)
    try:
        timeout = float(os.getenv("EMMA_DATA_TIMEOUT") or "45")
        if not 0 < timeout <= 300:
            raise ValueError
    except ValueError:
        return fail("EMMA_DATA_TIMEOUT must be a number between 0 and 300 seconds.", 500)

    # Never retry or fall back to another provider: a timeout can hide a paid purchase.
    try:
        response = requests.post(
            "https://emmadataking.com/api/v1/purchase", json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json", "X-API-Key": api_key},
            timeout=(10, timeout), allow_redirects=False,
        )
    except requests.RequestException:
        return fail("Emma Data could not confirm the purchase. Check the provider using the saved reference before resubmitting.", 599, uncertain=True)
    try:
        body = response.json()
    except ValueError:
        return fail("Emma Data returned an unreadable response. Check the provider before resubmitting.", response.status_code, uncertain=True)
    if not isinstance(body, dict):
        return fail("Emma Data returned an unexpected response. Check the provider before resubmitting.", response.status_code, uncertain=True)
    result["api_response"].update(http_status=response.status_code, response=body)
    data = body.get("data")
    ok = (200 <= response.status_code < 300 and body.get("status") == "success"
          and isinstance(data, dict) and bool(data.get("id")))
    if not ok:
        if response.status_code >= 500 or (200 <= response.status_code < 300 and body.get("status") == "success"):
            result.update(api_status="submission_unknown", provider_follow_up_required=True)
        return result

    status = str(data.get("status") or "").strip().upper()
    if status in {"COMPLETED", "DELIVERED"}:
        line_status, api_status = "delivered", "success"
    elif status in {"FAILED", "CANCELLED", "CANCELED", "REFUNDED", "REVERSED"}:
        line_status, api_status = "failed", "failed"
    else:
        line_status, api_status = "processing", "submitted"
    result.update(
        provider_order_id=str(data["id"]), provider_reference=str(data["id"]),
        provider_transaction_id=data.get("transaction_code"), provider_amount=data.get("amount"),
        provider_status=status, line_status=line_status, api_status=api_status,
        provider_follow_up_required=line_status != "delivered",
    )
    result["api_response"]["ok"] = True
    return result
