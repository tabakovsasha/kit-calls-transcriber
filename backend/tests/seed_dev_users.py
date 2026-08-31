"""Create two disposable development users for the websocket smoke test.

The bootstrap admin is left untouched: it still has must_change_password set,
which is exactly the state a fresh install should be in. Generated passwords go
to backend/.dev-test-credentials (mode 600, gitignored) and are never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_DIR / ".env"
CREDENTIALS_FILE = BACKEND_DIR / ".dev-test-credentials"
BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")


def read_env(key: str) -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_FILE}")


def make_password() -> str:
    """Random password that satisfies validate_password_strength."""
    return secrets.token_urlsafe(18).replace("-", "x").replace("_", "y") + "Aa1"


async def main() -> int:
    admin_email = read_env("INIT_ADMIN_EMAIL")
    admin_password = read_env("INIT_ADMIN_PASSWORD")

    users = [
        {"email": "ws-test-a@example.com", "password": make_password()},
        {"email": "ws-test-b@example.com", "password": make_password()},
    ]

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        login = await client.post(
            "/api/auth/login",
            json={"email": admin_email, "password": admin_password},
        )
        if login.status_code != 200:
            print(f"admin login failed: HTTP {login.status_code}")
            return 1
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        for user in users:
            response = await client.post(
                "/api/users",
                headers=headers,
                json={
                    "email": user["email"],
                    "password": user["password"],
                    "role": "USER",
                    "is_active": True,
                    # The smoke test needs to reach ActiveUserDep endpoints.
                    "must_change_password": False,
                },
            )
            if response.status_code == 201:
                user["id"] = response.json()["id"]
                print(f"created {user['email']}")
                continue

            # Already exists from a previous run: reset the password instead.
            listing = await client.get(
                "/api/users", headers=headers, params={"search": user["email"]}
            )
            listing.raise_for_status()
            payload = listing.json()
            items = payload["items"] if isinstance(payload, dict) else payload
            match = next(
                (item for item in items if item["email"] == user["email"]), None
            )
            if match is None:
                print(f"cannot create or find {user['email']}: HTTP {response.status_code}")
                return 1
            reset = await client.post(
                f"/api/users/{match['id']}/reset-password",
                headers=headers,
                json={
                    "new_password": user["password"],
                    "must_change_password": False,
                },
            )
            reset.raise_for_status()
            user["id"] = match["id"]
            print(f"reset password for {user['email']}")

    CREDENTIALS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    CREDENTIALS_FILE.chmod(0o600)
    print(f"credentials written to {CREDENTIALS_FILE.name} (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
