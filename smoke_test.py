"""
Quick local smoke tests for PolyEdge security behavior.

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
    os.environ["ACCESS_CODE"] = "smoke-test-code"

    import server  # pylint: disable=import-outside-toplevel

    # Keep test state isolated from any prior interactive usage.
    server.ACCESS_CODE = "smoke-test-code"
    server.AUTH_ATTEMPTS.clear()

    client = server.app.test_client()

    # 1) API should be locked before auth.
    unauth = client.get("/api/saved-traders")
    require(unauth.status_code == 401, "Expected /api/saved-traders to require auth")

    # 2) Login throttling should trigger after repeated failures.
    for _ in range(server.AUTH_MAX_FAILS + 1):
        client.post("/api/auth", json={"code": "wrong-code"})
    throttled = client.post("/api/auth", json={"code": "wrong-code"})
    require(throttled.status_code == 429, "Expected auth throttling after repeated failures")
    require("Too many attempts" in (throttled.json or {}).get("error", ""), "Expected throttle error message")

    # 3) Clear throttle state, authenticate, and verify access.
    server.AUTH_ATTEMPTS.clear()
    login = client.post("/api/auth", json={"code": "smoke-test-code"})
    require(login.status_code == 200 and (login.json or {}).get("ok") is True, "Expected successful login")

    saved = client.get("/api/saved-traders")
    require(saved.status_code == 200, "Expected authorized /api/saved-traders response")
    telegram = (saved.json or {}).get("telegram", {})
    require("token" not in telegram, "Telegram token must never be returned to frontend")
    require("connected" in telegram, "Expected telegram status payload")

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
