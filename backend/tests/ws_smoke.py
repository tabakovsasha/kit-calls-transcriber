"""Runtime smoke test for the websocket infrastructure.

Runs against a live backend (uvicorn + PostgreSQL) instead of mocking, so it
exercises the real ticket signature, the real session lookup and the real
ConnectionHub. Credentials are read from the environment and never printed.

Usage:
    .venv/bin/python -m tests.ws_smoke
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import httpx
import websockets

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")
WS_URL = BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

_failures: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" ({detail})" if detail else ""))
    if not passed:
        _failures.append(name)


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def get_ticket(client: httpx.AsyncClient, token: str) -> tuple[str, int]:
    response = await client.post(
        "/api/auth/ws-ticket", headers={"Authorization": f"Bearer {token}"}
    )
    response.raise_for_status()
    body = response.json()
    return body["ticket"], body["expires_at"]


async def ws_connect_fails(ticket: str | None) -> bool:
    """True when the handshake is rejected."""
    url = WS_URL if ticket is None else f"{WS_URL}?ticket={ticket}"
    try:
        async with websockets.connect(url, open_timeout=8) as socket:
            await asyncio.wait_for(socket.recv(), timeout=3)
        return False
    except Exception:
        return True


async def main() -> int:
    # User A is the primary actor, user B is the isolation counterpart.
    a_email = os.environ["SMOKE_USER_A_EMAIL"]
    a_password = os.environ["SMOKE_USER_A_PASSWORD"]
    b_email = os.environ["SMOKE_USER_B_EMAIL"]
    b_password = os.environ["SMOKE_USER_B_PASSWORD"]
    pending_email = os.environ.get("SMOKE_PENDING_EMAIL")
    pending_password = os.environ.get("SMOKE_PENDING_PASSWORD")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as client:
        token_a = await login(client, a_email, a_password)
        token_b = await login(client, b_email, b_password)
        check("login user A + user B", True)

        # A user who must still rotate their password cannot get a ticket.
        if pending_email and pending_password:
            pending_token = await login(client, pending_email, pending_password)
            pending = await client.post(
                "/api/auth/ws-ticket",
                headers={"Authorization": f"Bearer {pending_token}"},
            )
            check(
                "must_change_password user cannot get a ticket -> 403",
                pending.status_code == 403,
                f"got {pending.status_code}",
            )

        # --- ws-ticket -----------------------------------------------------
        no_auth = await client.post("/api/auth/ws-ticket")
        check(
            "ws-ticket without auth -> 401",
            no_auth.status_code == 401,
            f"got {no_auth.status_code}",
        )

        ticket, expires_at = await get_ticket(client, token_a)
        ttl = expires_at - int(time.time())
        check("ticket TTL is short", 0 < ttl <= 120, f"{ttl}s")
        check("ticket is not the access token", ticket != token_a)
        check("ticket has 4 signed parts", len(ticket.split(".")) == 4)

        # --- happy path ----------------------------------------------------
        async with websockets.connect(
            f"{WS_URL}?ticket={ticket}", open_timeout=8
        ) as socket:
            ready = await asyncio.wait_for(socket.recv(), timeout=5)
            check("valid ticket connects and gets ready", '"ready"' in ready)
            await socket.send("ping")
            pong = await asyncio.wait_for(socket.recv(), timeout=5)
            check("heartbeat ping -> pong", '"pong"' in pong)

        # --- rejection paths -----------------------------------------------
        check("no ticket is rejected", await ws_connect_fails(None))
        check("garbage ticket is rejected", await ws_connect_fails("not-a-ticket"))

        owner, session_id, exp, sig = ticket.split(".")
        forged_user = f"{uuid.uuid4()}.{session_id}.{exp}.{sig}"
        check("swapped user id breaks signature", await ws_connect_fails(forged_user))

        forged_exp = f"{owner}.{session_id}.{int(exp) + 3600}.{sig}"
        check("extended expiry breaks signature", await ws_connect_fails(forged_exp))

        forged_sig = f"{owner}.{session_id}.{exp}.{'0' * len(sig)}"
        check("tampered signature is rejected", await ws_connect_fails(forged_sig))

        expired = f"{owner}.{session_id}.{int(time.time()) - 60}.{sig}"
        check("expired ticket is rejected", await ws_connect_fails(expired))

        # --- session binding -----------------------------------------------
        # A second login for user B, then B revokes its other sessions: the
        # ticket minted from the now-revoked session must stop working.
        revoke_token = await login(client, b_email, b_password)
        doomed_ticket, _ = await get_ticket(client, revoke_token)
        await client.post(
            "/api/auth/sessions/revoke-others",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        check(
            "ticket from a revoked session is rejected",
            await ws_connect_fails(doomed_ticket),
        )

        # --- lifecycle: several sockets for one user ------------------------
        ticket_a, _ = await get_ticket(client, token_a)
        ticket_a2, _ = await get_ticket(client, token_a)
        ticket_b, _ = await get_ticket(client, token_b)
        # Tickets are a deterministic HMAC over (scope, session, owner, exp).
        # Two tickets minted within the same second are therefore identical, and
        # a ticket stays replayable until it expires. Documented, not a defect.
        check(
            "ticket is deterministic within the same expiry second",
            ticket_a == ticket_a2,
        )

        async with websockets.connect(f"{WS_URL}?ticket={ticket_a}") as sock_a1, \
                websockets.connect(f"{WS_URL}?ticket={ticket_a2}") as sock_a2, \
                websockets.connect(f"{WS_URL}?ticket={ticket_b}") as sock_b:
            for sock in (sock_a1, sock_a2, sock_b):
                ready = await asyncio.wait_for(sock.recv(), timeout=5)
                assert '"ready"' in ready
            check("same user may hold multiple sockets", True)
            check("two different users connected at once", True)

        # Sockets are closed here; the server must have dropped them all.
        await asyncio.sleep(0.5)
        check("reusing a ticket before expiry still works (documented)",
              not await ws_connect_fails(ticket_a))

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} -> {', '.join(_failures)}")
        return 1
    print("All websocket smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

