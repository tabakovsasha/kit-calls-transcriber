"""Call history search, scoped to one connection.

The client never supplies credentials: the caller picks a connection they own
and the service decrypts that connection's token just-in-time. Record URLs stay
server-side; the browser only receives signed playback URLs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

import httpx

from app.core.timeparse import (
    clamp_datetime_range_to_now,
    extract_call_timezone,
    parse_call_datetime,
)
from app.db.models import VoximplantConnection
from app.schemas.transcription import CallRecord, CallSearchResponse
from app.services import media_service
from app.services.voximplant_client import (
    SEARCH_RETRY_DELAY_SECONDS,
    SEARCH_TIMEOUT_SECONDS,
    UpstreamCredentials,
    VoximplantClient,
    get_dynamic_page_budget,
)

logger = logging.getLogger(__name__)


def _scenario_name(item: dict[str, Any]) -> Optional[str]:
    scenario = item.get("scenario")
    if isinstance(scenario, dict):
        name = scenario.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(scenario, str) and scenario.strip():
        return scenario.strip()
    return None


def _passes_filters(item: dict[str, Any], *, has_recording: bool, min_duration: int) -> bool:
    if has_recording and not item.get("record_url"):
        return False
    if min_duration and int(item.get("duration") or 0) < min_duration:
        return False
    return True


async def search_calls(
    *,
    connection: VoximplantConnection,
    credentials: UpstreamCredentials,
    owner_user_id: uuid.UUID,
    from_date: str,
    to_date: str,
    scenario_id: Optional[int],
    min_duration: int,
    has_recording: bool,
    records_limit: int,
    cursor: Optional[str] = None,
    transcripts_by_call: Optional[dict[str, str]] = None,
    metadata_sink: Optional[dict[str, dict[str, Any]]] = None,
) -> CallSearchResponse:
    """Page through the upstream history API until the limit or budget is hit.

    ``metadata_sink``, when provided, is filled with the raw per-call metadata
    (including ``record_url``) so the caller can cache it server-side. That URL
    embeds an access token and must never reach the browser.
    """
    client_wrapper = VoximplantClient(credentials)
    from_value, to_value, clamped = clamp_datetime_range_to_now(from_date, to_date)
    from_dt = parse_call_datetime(from_value)
    to_dt = parse_call_datetime(to_value)

    max_pages = get_dynamic_page_budget(records_limit)
    transcripts = transcripts_by_call or {}
    items: list[CallRecord] = []
    current_cursor = cursor
    pages = 0

    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as http_client:
        while pages < max_pages and len(items) < records_limit:
            pages += 1
            payload = await client_wrapper.search_calls_page(
                http_client,
                from_value=from_value,
                to_value=to_value,
                scenario_id=scenario_id,
                cursor=current_cursor,
            )

            results = payload.get("result")
            if not isinstance(results, list):
                results = []

            for item in results:
                if len(items) >= records_limit:
                    break
                if not isinstance(item, dict):
                    continue
                if not _passes_filters(
                    item, has_recording=has_recording, min_duration=min_duration
                ):
                    continue

                # Second safety filter: the upstream window is coarse, so drop
                # anything that falls outside the requested range.
                call_dt = parse_call_datetime(item.get("datetime_start"))
                if call_dt and from_dt and to_dt and not (from_dt <= call_dt <= to_dt):
                    continue

                call_id = str(item.get("id") or "").strip()
                if not call_id:
                    continue

                if metadata_sink is not None:
                    metadata_sink[call_id] = {
                        "record_url": item.get("record_url"),
                        "datetime_start": item.get("datetime_start"),
                        "timezone": extract_call_timezone(item),
                        "phone_a": item.get("phone_a"),
                        "phone_b": item.get("phone_b"),
                        "duration": int(item.get("duration") or 0),
                        "scenario_name": _scenario_name(item),
                    }

                audio_url: Optional[str] = None
                if item.get("record_url"):
                    audio_url, _ = media_service.build_audio_url(
                        owner_user_id, connection.id, call_id
                    )

                items.append(
                    CallRecord(
                        id=call_id,
                        datetime_start=item.get("datetime_start"),
                        timezone=extract_call_timezone(item),
                        phone_a=item.get("phone_a"),
                        phone_b=item.get("phone_b"),
                        duration=int(item.get("duration") or 0),
                        scenario_name=_scenario_name(item),
                        has_recording=bool(item.get("record_url")),
                        transcript=transcripts.get(call_id),
                        audio_url=audio_url,
                    )
                )

            meta = payload.get("_meta") or {}
            current_cursor = meta.get("cursor") if isinstance(meta, dict) else None
            if not current_cursor:
                break
            if len(items) < records_limit:
                await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS)

    return CallSearchResponse(
        items=items,
        total_loaded=len(items),
        can_load_more=bool(current_cursor) and len(items) >= records_limit,
        cursor=current_cursor,
        clamped_to_now=clamped,
    )


async def fetch_raw_calls(
    *,
    credentials: UpstreamCredentials,
    from_value: str,
    to_value: str,
    scenario_id: Optional[int],
    min_duration: int,
    has_recording: bool,
    records_limit: int,
) -> list[dict[str, Any]]:
    """Fetch raw upstream call dicts, used by schedule execution.

    Keeps ``record_url`` because the queue needs it to download audio; that URL
    never reaches a client response.
    """
    client_wrapper = VoximplantClient(credentials)
    from_dt = parse_call_datetime(from_value)
    to_dt = parse_call_datetime(to_value)
    max_pages = get_dynamic_page_budget(records_limit)

    collected: list[dict[str, Any]] = []
    current_cursor: Optional[str] = None
    pages = 0

    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as http_client:
        while pages < max_pages and len(collected) < records_limit:
            pages += 1
            payload = await client_wrapper.search_calls_page(
                http_client,
                from_value=from_value,
                to_value=to_value,
                scenario_id=scenario_id,
                cursor=current_cursor,
            )
            results = payload.get("result")
            if not isinstance(results, list):
                results = []

            for item in results:
                if len(collected) >= records_limit:
                    break
                if not isinstance(item, dict):
                    continue
                if not _passes_filters(
                    item, has_recording=has_recording, min_duration=min_duration
                ):
                    continue

                call_dt = parse_call_datetime(item.get("datetime_start"))
                if call_dt and from_dt and to_dt and not (from_dt <= call_dt <= to_dt):
                    continue

                call_id = str(item.get("id") or "").strip()
                if not call_id:
                    continue

                collected.append(
                    {
                        "id": call_id,
                        "record_url": item.get("record_url"),
                        "datetime_start": item.get("datetime_start"),
                        "timezone": extract_call_timezone(item),
                        "phone_a": item.get("phone_a"),
                        "phone_b": item.get("phone_b"),
                        "duration": int(item.get("duration") or 0),
                        "scenario_name": _scenario_name(item),
                    }
                )

            meta = payload.get("_meta") or {}
            current_cursor = meta.get("cursor") if isinstance(meta, dict) else None
            if not current_cursor:
                break
            if len(collected) < records_limit:
                await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS)

    return collected

