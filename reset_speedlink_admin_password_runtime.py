from datetime import datetime

from werkzeug.security import generate_password_hash

from db import db


users_col = db["users"]
TARGET_USERNAME = "speedlink"
NEW_PASSWORD = "admin123"


def main():
    print("SpeedLink Admin Password Reset")
    print("------------------------------")

    user = users_col.find_one({"username": TARGET_USERNAME})
    if not user:
        print(f"User '{TARGET_USERNAME}' was not found.")
        return

    hashed = generate_password_hash(NEW_PASSWORD)

    result = users_col.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password": hashed,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    if result.modified_count or result.matched_count:
        print("Password updated successfully.")
        print(f"Username: {TARGET_USERNAME}")
        print(f"New password: {NEW_PASSWORD}")
        print(f"Hash prefix: {hashed.split('$', 1)[0]}")
    else:
        print("No changes were applied.")


if __name__ == "__main__":
    main()
