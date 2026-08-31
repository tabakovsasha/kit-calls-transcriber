"""In-process isolation test for the websocket hub.

The live smoke test cannot force a real publish, because every hub.publish call
sits behind the transcription/scheduler pipeline which needs real Voximplant
credentials. Here the ASGI app is driven in the same event loop as the hub, so
the production endpoint, the production ticket signing and the production
ConnectionHub are all exercised while events are injected through the same
hub.publish that workers call. No production code is modified.

Usage:
    .venv/bin/python -m tests.ws_isolation
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.events_service import hub

BACKEND_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = BACKEND_DIR / ".dev-test-credentials"

_failures: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" ({detail})" if detail else ""))
    if not passed:
        _failures.append(name)


def load_users() -> list[dict]:
    if not CREDENTIALS_FILE.exists():
        raise SystemExit("run tests.seed_dev_users first")
    return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))


def main() -> int:
    users = load_users()
    user_a, user_b = users[0], users[1]

    with TestClient(app) as client:
        def token_for(user: dict) -> str:
            response = client.post(
                "/api/auth/login",
                json={"email": user["email"], "password": user["password"]},
            )
            response.raise_for_status()
            return response.json()["access_token"]

        def ticket_for(token: str) -> str:
            response = client.post(
                "/api/auth/ws-ticket", headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
            return response.json()["ticket"]

        token_a = token_for(user_a)
        token_b = token_for(user_b)
        ticket_a = ticket_for(token_a)
        ticket_b = ticket_for(token_b)

        id_a = uuid.UUID(ticket_a.split(".")[0])
        id_b = uuid.UUID(ticket_b.split(".")[0])
        check("two distinct owner ids", id_a != id_b)

        with client.websocket_connect(f"/ws?ticket={ticket_a}") as sock_a, \
                client.websocket_connect(f"/ws?ticket={ticket_b}") as sock_b:
            check("A ready frame", sock_a.receive_json()["type"] == "ready")
            check("B ready frame", sock_b.receive_json()["type"] == "ready")

            check("hub tracks one socket for A", hub.owner_count(id_a) == 1,
                  f"{hub.owner_count(id_a)}")
            check("hub tracks one socket for B", hub.owner_count(id_b) == 1,
                  f"{hub.owner_count(id_b)}")

            # Publish to B only, mirroring what transcription_service does.
            portal = client.portal
            portal.call(
                hub.publish,
                id_b,
                {"type": "queue_snapshot", "snapshot": {"marker": "for-b-only"}},
            )

            received_b = sock_b.receive_json()
            check(
                "B receives its own event",
                received_b.get("snapshot", {}).get("marker") == "for-b-only",
            )

            # A must see nothing. An empty queue raises instead of blocking.
            leaked = True
            try:
                sock_a._send_queue.get(timeout=1.5)  # type: ignore[attr-defined]
            except Exception:
                leaked = False
            check("A receives NO event addressed to B", not leaked)

            # Second socket for the same user.
            ticket_a2 = ticket_for(token_a)
            with client.websocket_connect(f"/ws?ticket={ticket_a2}") as sock_a2:
                sock_a2.receive_json()
                check("hub tracks two sockets for A", hub.owner_count(id_a) == 2,
                      f"{hub.owner_count(id_a)}")

                portal.call(
                    hub.publish,
                    id_a,
                    {"type": "queue_item_done", "item": {"marker": "for-a"}},
                )
                first = sock_a.receive_json()
                second = sock_a2.receive_json()
                check(
                    "both A sockets get the A event",
                    first["item"]["marker"] == "for-a"
                    and second["item"]["marker"] == "for-a",
                )

        # Everything closed: the hub must have released both owners.
        for _ in range(20):
            if hub.owner_count(id_a) == 0 and hub.owner_count(id_b) == 0:
                break
            import time

            time.sleep(0.1)
        check("hub is empty for A after disconnect", hub.owner_count(id_a) == 0,
              f"{hub.owner_count(id_a)}")
        check("hub is empty for B after disconnect", hub.owner_count(id_b) == 0,
              f"{hub.owner_count(id_b)}")

        # Publishing to a disconnected owner must not raise.
        try:
            client.portal.call(hub.publish, id_a, {"type": "queue_snapshot"})
            check("publish to owner with no sockets is a no-op", True)
        except Exception as exc:  # pragma: no cover
            check("publish to owner with no sockets is a no-op", False, repr(exc))

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} -> {', '.join(_failures)}")
        return 1
    print("All websocket isolation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
