from datetime import datetime

from werkzeug.security import generate_password_hash

from db import db


users_col = db["users"]
DEFAULT_PASSWORD = "1234"


def main():
    print("Admin Password Reset")
    print("--------------------")

    username = (input("Username [admin]: ") or "admin").strip()
    if not username:
        print("Username is required.")
        return

    user = users_col.find_one({"username": username})
    if not user:
        print(f"User '{username}' was not found.")
        return

    print(f"Found user: {user.get('username')} | role={user.get('role')} | status={user.get('status')}")

    hashed = generate_password_hash(DEFAULT_PASSWORD)

    users_col.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password": hashed,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    print("")
    print("Password updated successfully.")
    print(f"Username: {user.get('username')}")
    print(f"Password: {DEFAULT_PASSWORD}")
    print(f"Hash prefix: {hashed.split('$', 1)[0]}")


if __name__ == "__main__":
    main()
