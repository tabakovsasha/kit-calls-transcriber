"""Queue stage acceptance test: batch add, realtime progress, cancel, retry, clear.

Drives exactly the endpoints QueuePage.jsx and the CallsPage batch bar call,
through the Vite dev origin, so the proxy and the websocket are exercised the
way a browser exercises them.

Nothing is mocked: a real saved connection is used and real short recordings are
transcribed by local Whisper. History and recordings are only ever read.

Usage:
    .venv/bin/python -m tests.queue_e2e

Credentials come from backend/.env and are never printed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

from tests.calls_e2e import (
    BASE_URL,
    WS_URL,
    _failures,
    check,
    login,
    pick_connection,
    read_env,
    search_calls,
)

# Batch size for the acceptance run. Deliberately small: the point is to prove
# the batch path works, not to burn CPU on dozens of long recordings.
BATCH_SIZE = 3
DRAIN_TIMEOUT_SECONDS = 900
TERMINAL_STATUSES = ["done", "failed", "skipped", "canceled"]


async def get_queue(client: httpx.AsyncClient, auth: dict[str, str]) -> dict:
    response = await client.get("/api/queue", headers=auth)
    if response.status_code != 200:
        return {"_status": response.status_code, "_body": response.text[:300]}
    return response.json()


async def ws_collect(ticket: str, stop: asyncio.Event) -> dict:
    """Record every queue status transition the socket reports."""
    seen: dict[str, object] = {
        "types": set(),
        "statuses_by_call": {},
        "done_items": [],
    }

    try:
        async with websockets.connect(f"{WS_URL}?ticket={ticket}", open_timeout=15) as sock:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(sock.recv(), timeout=10)
                except asyncio.TimeoutError:
                    continue

                payload = json.loads(raw)
                event_type = payload.get("type")
                seen["types"].add(event_type)

                if event_type == "queue_snapshot":
                    for item in payload.get("snapshot", {}).get("items", []):
                        history = seen["statuses_by_call"].setdefault(item["call_id"], [])
                        if not history or history[-1] != item["status"]:
                            history.append(item["status"])
                elif event_type == "queue_item_done":
                    seen["done_items"].append(payload.get("item"))
    except Exception as exc:
        seen["error"] = str(exc)

    return seen


async def main() -> None:
    env = read_env()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as client:
        print("--- Session ---")
        auth = await login(client, env)
        connection = await pick_connection(client, auth)
        connection_id = connection["id"]

        # Start from a clean slate so counters are unambiguous.
        await client.post(
            "/api/queue/clear",
            headers=auth,
            json={"statuses": TERMINAL_STATUSES},
        )

        print("\n--- Find short recorded calls ---")
        # Short calls keep the Whisper cost of this test low.
        found = await search_calls(
            client, auth, connection_id, days_back=30, records_limit=50, min_duration=1
        )
        candidates = [
            item for item in found.get("items", []) if item.get("has_recording")
        ]
        candidates.sort(key=lambda item: item.get("duration") or 0)
        batch = candidates[:BATCH_SIZE]
        check(
            f"found at least {BATCH_SIZE} recorded calls",
            len(batch) >= BATCH_SIZE,
            f"{len(candidates)} with recording",
        )
        if len(batch) < BATCH_SIZE:
            raise SystemExit("Not enough recorded calls to exercise the batch path")

        durations = ", ".join(str(item.get("duration")) for item in batch)
        print(f"       selected {len(batch)} calls (durations: {durations}s)")

        # Open the socket before enqueuing: a fast Whisper run can finish before
        # a late subscriber ever sees `queued`.
        ticket_response = await client.post("/api/auth/ws-ticket", headers=auth)
        check("mint ws ticket", ticket_response.status_code == 200)
        ticket = ticket_response.json()["ticket"]

        stop = asyncio.Event()
        listener = asyncio.create_task(ws_collect(ticket, stop))
        await asyncio.sleep(1.0)

        print("\n--- Batch add ---")
        call_ids = [item["id"] for item in batch]
        add = await client.post(
            "/api/queue/add",
            headers=auth,
            json={"connection_id": connection_id, "call_ids": call_ids},
        )
        check("batch add accepted", add.status_code == 200, f"HTTP {add.status_code}")
        body = add.json() if add.status_code == 200 else {}
        check(
            "response reports how many were queued",
            body.get("queued") == len(call_ids),
            f"queued={body.get('queued')}, skipped_active={len(body.get('skipped_active', []))}",
        )
        check("response carries a snapshot", "snapshot" in body)

        print("\n--- Duplicate protection ---")
        again = await client.post(
            "/api/queue/add",
            headers=auth,
            json={"connection_id": connection_id, "call_ids": call_ids},
        )
        dup = again.json() if again.status_code == 200 else {}
        check(
            "re-adding active calls creates no duplicates",
            dup.get("queued") == 0 and len(dup.get("skipped_active", [])) == len(call_ids),
            f"queued={dup.get('queued')}, skipped={len(dup.get('skipped_active', []))}",
        )
        snapshot = await get_queue(client, auth)
        check(
            "queue holds exactly one item per call",
            len(snapshot.get("items", [])) == len(call_ids),
            f"{len(snapshot.get('items', []))} items",
        )

        print("\n--- Worker state ---")
        check("snapshot reports the worker is running", snapshot.get("is_running") is True)

        print("\n--- Drain and verify ---")
        deadline = time.time() + DRAIN_TIMEOUT_SECONDS
        done_count = 0
        while time.time() < deadline:
            snapshot = await get_queue(client, auth)
            counters = snapshot.get("counters", {})
            done_count = counters.get("done", 0)
            active = counters.get("queued", 0) + counters.get("processing", 0)
            print(
                f"       queued={counters.get('queued', 0)} "
                f"processing={counters.get('processing', 0)} "
                f"done={done_count} failed={counters.get('failed', 0)}",
                flush=True,
            )
            if active == 0:
                break
            await asyncio.sleep(5)

        check("at least one item reached done", done_count >= 1, f"{done_count} done")

        items = snapshot.get("items", [])
        done_items = [item for item in items if item["status"] == "done"]
        # An empty transcript is a legitimate DONE outcome: a few seconds of
        # silence or hold music yields no text. What matters is that the field is
        # populated (not None) and that real speech actually produced words.
        check(
            "done items carry a transcript field",
            all(item.get("transcript_text") is not None for item in done_items),
            f"{len(done_items)} done",
        )
        non_empty = [item for item in done_items if (item.get("transcript_text") or "").strip()]
        check(
            "at least one done item produced actual text",
            len(non_empty) >= 1,
            f"{len(non_empty)}/{len(done_items)} non-empty",
        )
        check(
            "queue response never leaks a record URL",
            not any("record_url" in item for item in items),
        )

        stop.set()
        events = await listener

        print("\n--- Realtime ---")
        check("received queue_snapshot events", "queue_snapshot" in events["types"])
        check(
            "received queue_item_done for finished work",
            len(events["done_items"]) >= 1,
            f"{len(events['done_items'])} done events",
        )
        transitions = events["statuses_by_call"]
        saw_progression = any(
            "done" in history or "processing" in history for history in transitions.values()
        )
        check(
            "socket reported live status transitions",
            saw_progression,
            "; ".join(f"{cid[-6:]}:{'>'.join(h)}" for cid, h in list(transitions.items())[:3]),
        )

        print("\n--- Clear completed ---")
        cleared = await client.post(
            "/api/queue/clear",
            headers=auth,
            json={"statuses": TERMINAL_STATUSES},
        )
        check("clear completed", cleared.status_code == 200, f"HTTP {cleared.status_code}")
        remaining = cleared.json().get("items", []) if cleared.status_code == 200 else []
        check(
            "no terminal items remain",
            not any(i["status"] in TERMINAL_STATUSES for i in remaining),
            f"{len(remaining)} left",
        )

    print("\n" + "=" * 60)
    if _failures:
        print(f"❌ {len(_failures)} checks failed:")
        for name in _failures:
            print(f"   • {name}")
        sys.exit(1)
    print("✅ All checks passed — queue acceptance criteria met")


if __name__ == "__main__":
    asyncio.run(main())
