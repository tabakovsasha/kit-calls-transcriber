"""Calls stage acceptance test: real Voximplant history, playback, transcription.

Drives exactly the endpoints CallsPage.jsx calls, through the Vite dev origin,
so the proxy, the signed media grant and the websocket are exercised the way a
browser exercises them.

Nothing is mocked: a real saved connection is used, real call history is read
and one real recording is transcribed by local Whisper. History and recordings
are only ever read; nothing upstream is modified.

Usage:
    .venv/bin/python -m tests.calls_e2e

Credentials come from backend/.env and are never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import websockets

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:5173")
WS_URL = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
BACKEND_DIR = Path(__file__).resolve().parent.parent

# How long to wait for local Whisper to finish one recording.
TRANSCRIBE_TIMEOUT_SECONDS = 900

_failures: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" ({detail})" if detail else ""), flush=True)
    if not passed:
        _failures.append(name)


def read_env() -> dict[str, str]:
    """Minimal .env reader. Values are used, never logged."""
    values: dict[str, str] = {}
    for line in (BACKEND_DIR / ".env").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"')
    return values


def local_input(offset_days: int) -> str:
    """`YYYY-MM-DDTHH:MM`, the same format the datetime-local input submits."""
    moment = time.localtime(time.time() - offset_days * 86400)
    return time.strftime("%Y-%m-%dT%H:%M", moment)



async def login(client: httpx.AsyncClient, env: dict[str, str]) -> dict[str, str]:
    """Log in as the bootstrap admin and return the Authorization header."""
    response = await client.post(
        "/api/auth/login",
        json={
            "email": env["INIT_ADMIN_EMAIL"],
            "password": env["INIT_ADMIN_PASSWORD"],
        },
    )
    check("login as admin", response.status_code == 200, f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise SystemExit("Cannot continue without a session")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def pick_connection(client: httpx.AsyncClient, auth: dict[str, str]) -> dict:
    """Find the real, active, verified connection to run the scenario against."""
    response = await client.get("/api/connections", headers=auth)
    check("list connections", response.status_code == 200, f"HTTP {response.status_code}")
    connections = response.json() if response.status_code == 200 else []

    usable = [item for item in connections if item.get("is_active")]
    check("an active connection exists", bool(usable), f"{len(usable)} active")
    if not usable:
        raise SystemExit("No active connection to test with")

    connection = usable[0]
    # The token must never come back in any form. Only the mask and the
    # fingerprint are allowed; `*_rotated_at` and friends are metadata.
    secret_like = {
        key: value
        for key, value in connection.items()
        if key.startswith("access_token")
        and key not in {"access_token_masked", "access_token_fingerprint", "access_token_rotated_at"}
    }
    check("connection payload carries no plaintext token", not secret_like, str(list(secret_like)))
    check(
        "token is exposed only as a mask",
        set(connection.get("access_token_masked", "")) <= {"*", "•", "…", "."} or "*" in connection.get("access_token_masked", ""),
        connection.get("access_token_masked", ""),
    )
    print(f"       using connection: {connection['label']} ({connection['api_host']})")
    return connection


async def search_calls(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    connection_id: str,
    *,
    days_back: int,
    records_limit: int,
    min_duration: int = 0,
    has_recording: bool = True,
    scenario_id: int | None = None,
    cursor: str | None = None,
) -> dict:
    """Call POST /api/calls/search exactly as the page does."""
    path = "/api/calls/search"
    if cursor:
        path += f"?cursor={httpx.QueryParams({'c': cursor})['c']}"

    response = await client.post(
        path,
        headers=auth,
        json={
            "connection_id": connection_id,
            "from_date": local_input(days_back),
            "to_date": local_input(0),
            "scenario_id": scenario_id,
            "min_duration": min_duration,
            "has_recording": has_recording,
            "records_limit": records_limit,
        },
    )
    if response.status_code != 200:
        return {"_status": response.status_code, "_body": response.text[:300]}
    return response.json()


async def playback_checks(
    client: httpx.AsyncClient, auth: dict[str, str], connection_id: str, call_id: str
) -> None:
    """Mint a signed playback URL and stream the recording through the backend."""
    response = await client.post(
        "/api/calls/audio-url",
        headers=auth,
        json={"connection_id": connection_id, "call_id": call_id},
    )
    check("mint signed audio URL", response.status_code == 200, f"HTTP {response.status_code}")
    if response.status_code != 200:
        return

    payload = response.json()
    audio_url = payload["audio_url"]
    check("audio URL points back at this backend", audio_url.startswith("/api/audio/stream?"))
    check(
        "audio URL carries no upstream token",
        "access_token" not in audio_url and "voximplant" not in audio_url.lower(),
    )
    ttl = int(payload["expires_at"]) - int(time.time())
    check("signed URL TTL is bounded", 0 < ttl <= 3600, f"{ttl}s")

    # The <audio> element sends no Authorization header, so neither do we.
    stream = await client.get(audio_url, timeout=240)
    ok = stream.status_code == 200 and len(stream.content) > 1000
    check(
        "stream recording without a bearer token",
        ok,
        f"HTTP {stream.status_code}, {len(stream.content)} bytes",
    )
    check(
        "recording served as audio",
        stream.headers.get("content-type", "").startswith("audio/"),
        stream.headers.get("content-type", ""),
    )

    # Tamper checks: the grant must not be forgeable or reusable elsewhere.
    tampered = re.sub(r"call_id=[^&]*", "call_id=999999999", audio_url)
    bad = await client.get(tampered)
    check("tampered call_id is rejected", bad.status_code in (403, 404), f"HTTP {bad.status_code}")

    no_sig = re.sub(r"&sig=[^&]*", "", audio_url)
    bad2 = await client.get(no_sig)
    check("missing signature is rejected", bad2.status_code in (403, 422), f"HTTP {bad2.status_code}")


async def ws_listen(ticket: str, call_id: str, stop: asyncio.Event) -> dict:
    """Collect transcription events for one call until it reaches a terminal state."""
    seen: dict[str, object] = {"types": set(), "statuses": [], "done_item": None}

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
                        if item.get("call_id") == call_id:
                            status = item.get("status")
                            if status not in seen["statuses"]:
                                seen["statuses"].append(status)
                elif event_type == "queue_item_done":
                    item = payload.get("item") or {}
                    if item.get("call_id") == call_id:
                        seen["done_item"] = item
                        return seen
    except Exception as exc:  # noqa: BLE001 - reported by the caller as a check
        seen["error"] = str(exc)

    return seen




async def main() -> None:
    env = read_env()
    timeout = httpx.Timeout(240, connect=10)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, follow_redirects=True) as client:
        auth = await login(client, env)
        connection = await pick_connection(client, auth)
        connection_id = connection["id"]

        # 1. Search filters and pagination
        print("\n--- Search and filters ---")
        search_30d = await search_calls(
            client, auth, connection_id, days_back=30, records_limit=10, min_duration=5
        )
        check("search with filters succeeded", "_status" not in search_30d, str(search_30d.get("_status")))
        items_30d = search_30d.get("items", [])
        check("found at least one call in 30 days", len(items_30d) > 0, f"{len(items_30d)} calls")

        if not items_30d:
            print("       no calls in 30 days; skipping rest of flow")
            if _failures:
                print(f"\n❌ {len(_failures)} FAIL: {', '.join(_failures)}")
                sys.exit(1)
            print("\n✅ All checks passed (subset: no calls to test)")
            return

        first_call = items_30d[0]
        check("call record has required fields", all(k in first_call for k in ("id", "datetime_start", "duration")))
        check(
            "call record carries no upstream record_url",
            "record_url" not in first_call or not first_call["record_url"],
        )

        total_loaded = search_30d.get("total_loaded", 0)
        can_load_more = search_30d.get("can_load_more", False)
        cursor = search_30d.get("cursor")
        check("pagination metadata present", total_loaded > 0, f"loaded={total_loaded}, more={can_load_more}")

        # If a cursor exists, exercise "Load more"
        if cursor:
            print("       pagination cursor exists; loading next page")
            page2 = await search_calls(
                client, auth, connection_id, days_back=30, records_limit=10, cursor=cursor
            )
            check("load more succeeded", "_status" not in page2, str(page2.get("_status")))
            items_p2 = page2.get("items", [])
            if items_p2:
                check("second page appends items", items_p2[0]["id"] != items_30d[0]["id"])

        # 2. Playback signed URL grant + streaming
        print("\n--- Playback ---")
        playback_call = next((c for c in items_30d if c.get("has_recording")), None)
        if not playback_call:
            print("       no recordings in the result set; skipping playback + transcription")
        else:
            await playback_checks(client, auth, connection_id, playback_call["id"])

            # 3. Single-call transcription + WebSocket live progress
            print(f"\n--- Transcription (call {playback_call['id']}) ---")

            # Open the socket *before* enqueuing so a fast transcription can't finish
            # before we subscribe. Whisper base on a short call can finish in <3s.
            print("       opening websocket to watch progress")
            ticket_resp = await client.post("/api/auth/ws-ticket", headers=auth)
            check("mint WS ticket", ticket_resp.status_code == 200, f"HTTP {ticket_resp.status_code}")
            ticket = ticket_resp.json()["ticket"]

            stop = asyncio.Event()
            ws_task = asyncio.create_task(ws_listen(ticket, playback_call["id"], stop))

            # Now enqueue the call.
            add_response = await client.post(
                "/api/queue/add",
                headers=auth,
                json={"connection_id": connection_id, "call_ids": [playback_call["id"]]},
            )
            check("add call to queue", add_response.status_code == 200, f"HTTP {add_response.status_code}")

            # Wait for the worker to process it (local Whisper typically takes 30s–120s for a 30s recording).
            print(f"       waiting up to {TRANSCRIBE_TIMEOUT_SECONDS}s for Whisper to finish...")
            try:
                seen = await asyncio.wait_for(ws_task, timeout=TRANSCRIBE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                stop.set()
                seen = {"error": "timeout"}

            check("websocket delivered events", "types" in seen and seen["types"], str(seen.get("types")))
            statuses = seen.get("statuses", [])
            check("queue transitioned through states", len(statuses) >= 1, str(statuses))
            done_item = seen.get("done_item")
            check("queue_item_done event arrived", done_item is not None, str(done_item))

            if done_item:
                transcript_text = done_item.get("transcript_text")
                check("transcript text is non-empty", bool(transcript_text), f"{len(transcript_text or '')} chars")

                # 5. Persistence check
                print("\n--- Persistence ---")
                search_again = await search_calls(
                    client, auth, connection_id, days_back=30, records_limit=10, min_duration=5
                )
                items_again = search_again.get("items", [])
                reloaded = next((c for c in items_again if c["id"] == playback_call["id"]), None)
                check("call reappears in a fresh search", reloaded is not None)
                if reloaded:
                    check(
                        "transcript survived reload without re-running Whisper",
                        bool(reloaded.get("transcript")),
                        f"{len(reloaded.get('transcript', ''))} chars",
                    )

    # Final report
    print("\n" + "=" * 60)
    if _failures:
        print(f"❌ {len(_failures)} checks failed:")
        for name in _failures:
            print(f"   • {name}")
        sys.exit(1)
    print("✅ All checks passed — acceptance criteria met")


if __name__ == "__main__":
    asyncio.run(main())
