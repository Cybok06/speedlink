from __future__ import annotations

import json
import os
import re
import sys

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


JUSTICE_BASE_URL = os.getenv("JUSTICE_BASE_URL", "https://backend.justicedatashop.com")
JUSTICE_API_KEY = "naj_live_1776845399048_s6z97h9x6n44nt7h6yaa6_0z3gnzvun0trw1bv8yeu51"
JUSTICE_TIMEOUT = int(os.getenv("JUSTICE_TIMEOUT", "45"))

# Prefilled test order requested by you.
TEST_PHONE = "0530393625"
TEST_SIZE = 1
TEST_NETWORK = "MTN"
TEST_CALLBACK = os.getenv("JUSTICE_CALLBACK_URL", "").strip()

ALLOWED_NETWORKS = {"TELECEL", "MTN", "AIRTELTIGO_ISHARE", "AIRTELTIGO_BIGTIME"}


def _normalize_phone(phone: str) -> str:
    """
    Justice expects a local Ghana number without +233/233 prefix.
    Example:
      233530393625 -> 0530393625
      +233530393625 -> 0530393625
    """
    digits = re.sub(r"\D+", "", str(phone or ""))
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 7:
        return "***"
    return f"{phone[:4]}***{phone[-3:]}"


def place_justice_order(
    phone: str,
    size: int,
    network: str,
    callback: str | None = None,
) -> tuple[bool, int | None, dict]:
    if not JUSTICE_API_KEY:
        return False, None, {
            "error": "JUSTICE_API_KEY is not set. Add it to your environment or .env file first."
        }

    normalized_phone = _normalize_phone(phone)
    network = str(network or "").strip().upper()

    if network not in ALLOWED_NETWORKS:
        return False, None, {
            "error": f"Invalid network '{network}'. Allowed: {', '.join(sorted(ALLOWED_NETWORKS))}"
        }

    payload = {
        "phone": normalized_phone,
        "size": int(size),
        "network": network,
    }
    if callback:
        payload["callback"] = callback

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-Key": JUSTICE_API_KEY,
    }

    url = f"{JUSTICE_BASE_URL.rstrip('/')}/api/order"

    print("Justice test order")
    print("------------------")
    print(f"URL: {url}")
    print("Payload:")
    print(
        json.dumps(
            {
                **payload,
                "phone": _mask_phone(normalized_phone),
            },
            indent=2,
        )
    )
    print("")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=JUSTICE_TIMEOUT)
        status_code = response.status_code
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        ok = bool(
            200 <= status_code < 300
            and isinstance(body, dict)
            and body.get("status") is True
        )
        return ok, status_code, body
    except requests.RequestException as exc:
        return False, None, {"error": str(exc), "type": "NETWORK_ERROR"}


def main() -> int:
    ok, status_code, response_body = place_justice_order(
        phone=TEST_PHONE,
        size=TEST_SIZE,
        network=TEST_NETWORK,
        callback=TEST_CALLBACK or None,
    )

    print("Response:")
    print(json.dumps(response_body, indent=2, default=str))
    print("")

    if status_code is not None:
        print(f"HTTP Status: {status_code}")

    print(f"Success: {'YES' if ok else 'NO'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
