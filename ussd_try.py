import requests
import json

url = "https://povills.onrender.com/ussd/callback"

payload = {
    "sessionId": "test-session-002",
    "new": True,
    "msisdn": "233530457300",
    "network": 3,
    "message": "",
    "extension": "109",
    "data": "11005"
}

headers = {
    "Content-Type": "application/json"
}

try:
    response = requests.post(
        url,
        headers=headers,
        data=json.dumps(payload),
        timeout=30
    )

    print("STATUS CODE:")
    print(response.status_code)

    print("\nRESPONSE:")
    try:
        print(json.dumps(response.json(), indent=2))
    except Exception:
        print(response.text)

except Exception as e:
    print("ERROR:")
    print(str(e))