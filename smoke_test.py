"""
Quick local smoke tests for PolyEdge desktop-local behavior.

Run:
  py smoke_test.py
"""
import os
import sys


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, repo_dir)

    import server  # pylint: disable=import-outside-toplevel

    client = server.app.test_client()

    # 1) API should be locked before a session is established.
    unauth = client.get("/api/saved-traders")
    require(unauth.status_code == 401, "Expected /api/saved-traders to require auth")

    # 2) Visiting the app root should create an authenticated local session.
    root = client.get("/")
    require(root.status_code == 200, "Expected app index page")

    saved = client.get("/api/saved-traders")
    require(saved.status_code == 200, "Expected authorized /api/saved-traders after index session")
    telegram = (saved.json or {}).get("telegram", {})
    require("token" not in telegram, "Telegram token must never be returned to frontend")
    require("connected" in telegram, "Expected telegram status payload")

    # 3) /api/auth should still return success in desktop-local mode.
    login = client.post("/api/auth", json={"code": "anything"})
    require(login.status_code == 200 and (login.json or {}).get("ok") is True, "Expected desktop-local auth success")

    # 4) Address validation should reject malformed addresses.
    bad_address = client.post("/api/add-trader", json={"address": "not-a-wallet"})
    require((bad_address.json or {}).get("error") == "Enter a valid wallet address", "Expected address validation error")

    # 5) Logout should clear access.
    logout = client.post("/api/logout")
    require(logout.status_code == 200 and (logout.json or {}).get("ok") is True, "Expected successful logout")
    post_logout = client.get("/api/saved-traders")
    require(post_logout.status_code == 401, "Expected API lock after logout")

    print("Smoke tests passed.")


if __name__ == "__main__":
    main()
