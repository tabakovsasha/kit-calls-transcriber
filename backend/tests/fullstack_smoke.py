"""Full-stack smoke test: browser origin -> Vite proxy -> FastAPI -> PostgreSQL.

Deliberately talks to the dev server origin (default http://127.0.0.1:5173), not
the backend port, so the proxy, the same-origin refresh cookie and the websocket
upgrade are all covered the way a browser would exercise them.

Idempotent: the admin password is changed to a temporary value and changed back,
so the value in .env stays authoritative and the test can be re-run.

Usage:
    .venv/bin/python -m tests.fullstack_smoke

Requires a running Vite dev server and backend. Credentials are read from
backend/.env (INIT_ADMIN_EMAIL / INIT_ADMIN_PASSWORD) and never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx
import websockets

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:5173")
WS_URL = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
BACKEND_DIR = Path(__file__).resolve().parent.parent

# Long enough to satisfy PASSWORD_MIN_LENGTH, only ever used inside this run.
TEMP_PASSWORD = "Sm0ke-Temp-Pass!2026"

_failures: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" ({detail})" if detail else ""))
    if not passed:
        _failures.append(name)


def read_env() -> dict[str, str]:
    """Minimal .env reader: no export, no interpolation, no logging of values."""
    values: dict[str, str] = {}
    for line in (BACKEND_DIR / ".env").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"')
    return values


async def ws_roundtrip(ticket: str) -> tuple[dict, dict]:
    """Open the socket through the proxy and exercise ready + ping/pong."""
    async with websockets.connect(f"{WS_URL}?ticket={ticket}", open_timeout=10) as sock:
        ready = json.loads(await asyncio.wait_for(sock.recv(), timeout=10))
        await sock.send("ping")
        pong = json.loads(await asyncio.wait_for(sock.recv(), timeout=10))
        return ready, pong


async def ws_rejected(ticket: str) -> bool:
    """True when the handshake is refused, as in tests/ws_smoke.py."""
    try:
        async with websockets.connect(f"{WS_URL}?ticket={ticket}", open_timeout=8) as sock:
            await asyncio.wait_for(sock.recv(), timeout=8)
        return False
    except Exception:  # noqa: BLE001 - any failure means "rejected"
        return True



async def connections_checks(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    """Exercise the connections CRUD surface, including the re-create case."""
    payload = {
        "label": "fullstack-smoke",
        "api_host": "kitapi-eu.voximplant.com",
        "domain": "fullstack-smoke-domain",
        "access_token": "dummy-token-not-verified",
        "verify": False,
    }

    response = await client.post("/api/connections", json=payload, headers=auth)
    check("connection create 201", response.status_code == 201, str(response.status_code))
    created = response.json()
    connection_id = created["id"]
    check("credentials never echoed back",
          "access_token" not in created and "token" not in created,
          ",".join(sorted(created.keys())))

    response = await client.get("/api/connections", headers=auth)
    listed = response.json()
    items = listed["items"] if isinstance(listed, dict) else listed
    check("connection list 200", response.status_code == 200, str(response.status_code))
    check("created connection is listed",
          any(item["id"] == connection_id for item in items))

    response = await client.put(
        f"/api/connections/{connection_id}",
        json={"label": "fullstack-smoke-renamed"}, headers=auth,
    )
    check("connection update 200", response.status_code == 200, str(response.status_code))
    check("label updated", response.json()["label"] == "fullstack-smoke-renamed")

    response = await client.post("/api/connections", json=payload, headers=auth)
    check("duplicate host+domain is 409", response.status_code == 409,
          str(response.status_code))

    response = await client.delete(f"/api/connections/{connection_id}", headers=auth)
    check("connection delete 200", response.status_code == 200, str(response.status_code))

    # Regression: the unique constraint used to ignore deleted_at, so this
    # second create returned 500 instead of 201.
    response = await client.post("/api/connections", json=payload, headers=auth)
    check("re-create after delete 201", response.status_code == 201,
          str(response.status_code))
    if response.status_code == 201:
        await client.delete(f"/api/connections/{response.json()['id']}", headers=auth)


async def main() -> int:
    env = read_env()
    email = env["INIT_ADMIN_EMAIL"]
    password = env["INIT_ADMIN_PASSWORD"]
    cookie_name = env.get("REFRESH_COOKIE_NAME", "kit_refresh")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        # --- frontend is served ------------------------------------------
        response = await client.get("/")
        check("dev server serves index.html", response.status_code == 200,
              str(response.status_code))
        check("index.html mounts the SPA", 'id="root"' in response.text)

        # --- login through the proxy -------------------------------------
        response = await client.post(
            "/api/auth/login", json={"email": email, "password": password}
        )
        check("login 200", response.status_code == 200, str(response.status_code))
        body = response.json()
        token = body["access_token"]
        user = body["user"]
        check("refresh cookie issued", cookie_name in client.cookies)
        auth = {"Authorization": f"Bearer {token}"}

        # --- /auth/me ----------------------------------------------------
        response = await client.get("/api/auth/me", headers=auth)
        check("/auth/me 200", response.status_code == 200, str(response.status_code))
        check("/auth/me returns the caller", response.json().get("email") == email)

        # --- error envelope ----------------------------------------------
        response = await client.get("/api/auth/me")
        envelope = response.json()
        check("unauthenticated request is 401", response.status_code == 401,
              str(response.status_code))
        check("AppError envelope has code+message",
              "code" in envelope and "message" in envelope,
              envelope.get("code", ""))

        # --- forced password change --------------------------------------
        # The bootstrap admin starts with must_change_password=true; after the
        # first run of this test the flag is already cleared, so both states
        # are handled to keep the test re-runnable.
        if user["must_change_password"]:
            response = await client.post("/api/auth/ws-ticket", headers=auth)
            check("ws-ticket blocked before password change",
                  response.status_code == 403, str(response.status_code))

            response = await client.post(
                "/api/auth/change-password", headers=auth,
                json={"current_password": password, "new_password": TEMP_PASSWORD},
            )
            check("forced password change 200", response.status_code == 200,
                  str(response.status_code))

            response = await client.get("/api/auth/me", headers=auth)
            check("must_change_password cleared",
                  response.status_code == 200
                  and response.json().get("must_change_password") is False)

            # Restore .env as the single source of truth for the password.
            response = await client.post(
                "/api/auth/change-password", headers=auth,
                json={"current_password": TEMP_PASSWORD, "new_password": password},
            )
            check("password restored to the .env value", response.status_code == 200,
                  str(response.status_code))
        else:
            response = await client.post(
                "/api/auth/change-password", headers=auth,
                json={"current_password": password, "new_password": "short"},
            )
            check("weak password rejected", response.status_code in (400, 422),
                  str(response.status_code))
            print("[SKIP] forced-change flow (must_change_password already cleared)")

        # --- websocket ---------------------------------------------------
        response = await client.post("/api/auth/ws-ticket", headers=auth)
        check("ws-ticket 200", response.status_code == 200, str(response.status_code))
        ticket = response.json()["ticket"]
        check("ticket has 4 signed parts", len(ticket.split(".")) == 4)

        try:
            ready, pong = await ws_roundtrip(ticket)
            check("ws upgrade through the proxy", ready.get("type") == "ready",
                  json.dumps(ready))
            check("ws answers ping with pong", pong.get("type") == "pong")
        except Exception as exc:  # noqa: BLE001
            check("ws upgrade through the proxy", False, repr(exc))

        response = await client.post("/api/auth/ws-ticket")
        check("ws-ticket requires auth", response.status_code == 401,
              str(response.status_code))
        check("forged ticket is rejected",
              await ws_rejected("bogus.bogus.9999999999.deadbeef"))

        # --- refresh: the page-reload path -------------------------------
        response = await client.post("/api/auth/refresh")
        check("refresh works with cookie alone", response.status_code == 200,
              str(response.status_code))
        token = response.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        check("refresh returns the user", response.json()["user"]["email"] == email)
        response = await client.get("/api/auth/me", headers=auth)
        check("rotated access token is usable", response.status_code == 200,
              str(response.status_code))

        await connections_checks(client, auth)

        # --- logout ------------------------------------------------------
        response = await client.post("/api/auth/logout", headers=auth)
        check("logout 200", response.status_code == 200, str(response.status_code))
        response = await client.post("/api/auth/refresh")
        check("refresh dead after logout", response.status_code == 401,
              str(response.status_code))
        response = await client.get("/api/auth/me", headers=auth)
        check("access token dead after logout", response.status_code == 401,
              str(response.status_code))

    print(f"\n{'FAILED' if _failures else 'OK'}: {len(_failures)} failure(s)")
    if _failures:
        print("failed checks: " + ", ".join(_failures))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
