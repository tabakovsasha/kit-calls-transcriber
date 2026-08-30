"""Background workers: queue drain and hourly schedule execution.

Two long-lived loops started at app startup:
- ``queue_runner_loop`` claims queued items (per owner, FOR UPDATE SKIP LOCKED)
  and processes them with bounded concurrency;
- ``scheduler_loop`` finds due schedules, searches the previous full hour and
  enqueues matching calls.

Both loops own their sessions: a background task must never borrow a request
session. Every iteration is wrapped so one bad owner cannot kill the loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.runtime import runtime
from app.db.session import SessionFactory
from app.services import (
    call_cache,
    call_search_service,
    connection_service,
    queue_service,
    schedule_service,
    transcription_service,
)
from app.services.events_service import hub

logger = logging.getLogger(__name__)

QUEUE_POLL_SECONDS = 3.0

_tasks: list[asyncio.Task] = []
_shutdown = asyncio.Event()
_active_owners: set[uuid.UUID] = set()


async def _drain_owner(owner_user_id: uuid.UUID) -> None:
    """Process every queued item for one owner, honoring the concurrency limit."""
    while not _shutdown.is_set():
        async with SessionFactory() as db:
            item = await queue_service.claim_next_item(db, owner_user_id)
            if item is None:
                await db.commit()
                return
            # Commit the PROCESSING claim before the long-running work so other
            # workers immediately see the item as taken.
            await db.commit()
            await transcription_service.process_queue_item(db, item)


async def queue_runner_loop() -> None:
    """Poll for owners with pending work and drain their queues in parallel."""
    logger.info("[QUEUE] Runner started")
    while not _shutdown.is_set():
        try:
            async with SessionFactory() as db:
                owners = await queue_service.owners_with_pending_work(db)

            pending = [owner for owner in owners if owner not in _active_owners]
            if pending:
                limit = max(1, runtime.get("queue_workers"))
                batch = pending[:limit]
                _active_owners.update(batch)

                async def _run(owner_id: uuid.UUID) -> None:
                    try:
                        await _drain_owner(owner_id)
                    except Exception as exc:
                        logger.error(
                            "[QUEUE] Не удалось обработать очередь owner=%s: %s",
                            owner_id,
                            exc,
                            exc_info=True,
                        )
                    finally:
                        _active_owners.discard(owner_id)

                await asyncio.gather(*(_run(owner) for owner in batch))
        except Exception as exc:
            logger.error("[QUEUE] Ошибка цикла обработки: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=QUEUE_POLL_SECONDS)
        except asyncio.TimeoutError:
            continue

    logger.info("[QUEUE] Runner stopped")


async def run_schedule_now(schedule_id: uuid.UUID, owner_user_id: uuid.UUID) -> dict[str, Any]:
    """Execute one schedule for the previous full hour and enqueue its calls."""
    now = datetime.now()
    from_value, to_value, slot_key = schedule_service.window_for(now)

    async with SessionFactory() as db:
        schedule = await schedule_service.get_owned_schedule(db, schedule_id, owner_user_id)
        try:
            _, credentials = await connection_service.resolve_credentials(
                db, schedule.connection_id, owner_user_id
            )
            calls = await call_search_service.fetch_raw_calls(
                credentials=credentials,
                from_value=from_value,
                to_value=to_value,
                scenario_id=schedule.scenario_id,
                min_duration=schedule.min_duration,
                has_recording=schedule.has_recording,
                records_limit=schedule.records_limit,
            )
        except AppError as exc:
            result = {"status": "error", "error": exc.message, "window": [from_value, to_value]}
            schedule_service.record_run(schedule, slot_key, result)
            await db.commit()
            return result

        metadata = {call["id"]: call for call in calls}
        call_cache.remember_many(owner_user_id, schedule.connection_id, metadata)

        created, skipped = await queue_service.add_items(
            db,
            owner_user_id=owner_user_id,
            connection_id=schedule.connection_id,
            call_ids=list(metadata.keys()),
            whisper_model=schedule.whisper_model,
            metadata_by_call=metadata,
        )
        result = {
            "status": "ok",
            "found": len(calls),
            "queued": len(created),
            "skipped": len(skipped),
            "window": [from_value, to_value],
        }
        schedule_service.record_run(schedule, slot_key, result)
        await db.commit()

        snapshot = await queue_service.build_snapshot(db, owner_user_id)

    await hub.publish(
        owner_user_id,
        {"type": "queue_snapshot", "snapshot": snapshot.model_dump(mode="json")},
    )
    return result


async def scheduler_loop() -> None:
    """Check every poll interval whether any schedule is due for its slot."""
    poll_seconds = get_settings().scheduler_poll_seconds
    logger.info("[SCHEDULER] Loop started (poll=%ss)", poll_seconds)

    while not _shutdown.is_set():
        try:
            now = datetime.now()
            async with SessionFactory() as db:
                due = await schedule_service.due_schedules(db, now)
                pending = [(item.id, item.owner_user_id) for item in due]

            for schedule_id, owner_user_id in pending:
                if _shutdown.is_set():
                    break
                try:
                    await run_schedule_now(schedule_id, owner_user_id)
                except Exception as exc:
                    logger.error(
                        "[SCHEDULER] Расписание %s завершилось ошибкой: %s",
                        schedule_id,
                        exc,
                        exc_info=True,
                    )
        except Exception as exc:
            logger.error("[SCHEDULER] Ошибка цикла: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            continue

    logger.info("[SCHEDULER] Loop stopped")


def start_workers() -> None:
    """Start the background loops. Idempotent."""
    if _tasks:
        return
    _shutdown.clear()
    _tasks.append(asyncio.create_task(queue_runner_loop(), name="queue-runner"))
    _tasks.append(asyncio.create_task(scheduler_loop(), name="scheduler"))


async def stop_workers() -> None:
    """Signal both loops and wait for them to unwind."""
    _shutdown.set()
    for task in _tasks:
        if not task.done():
            task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    _active_owners.clear()

