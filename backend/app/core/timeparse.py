"""Datetime helpers shared by call search and schedule execution.

Ported from the legacy single-file app. Kept dependency-free so both the
request path and the background scheduler can use them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

API_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def normalize_datetime_for_api(value: str) -> str:
    """Accept ``YYYY-MM-DDTHH:MM`` from the UI and emit what the Kit API wants."""
    if "T" in value:
        value = value.replace("T", " ")
    if len(value) == 16:
        value += ":00"
    return value


def parse_input_datetime(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(normalize_datetime_for_api(value))
    except ValueError:
        return None


def clamp_datetime_range_to_now(from_date: str, to_date: str) -> tuple[str, str, bool]:
    """Clamp a user-selected range so the upstream never sees future stamps.

    Returns (from_value, to_value, to_was_clamped).
    """
    from_value = normalize_datetime_for_api(from_date)
    to_value = normalize_datetime_for_api(to_date)

    now_value = datetime.now().replace(microsecond=0)
    from_dt = parse_input_datetime(from_value)
    to_dt = parse_input_datetime(to_value)
    to_was_clamped = False

    if to_dt and to_dt > now_value:
        to_dt = now_value
        to_value = to_dt.strftime(API_DATETIME_FORMAT)
        to_was_clamped = True

    if from_dt and from_dt > now_value:
        from_dt = now_value
        from_value = from_dt.strftime(API_DATETIME_FORMAT)

    if from_dt and to_dt and from_dt > to_dt:
        from_value = to_dt.strftime(API_DATETIME_FORMAT)

    return from_value, to_value, to_was_clamped


def parse_call_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an upstream call timestamp, tolerating ISO and space-separated forms."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("T", " ").replace("Z", ""))
    except ValueError:
        return None


def extract_call_timezone(payload: dict) -> Optional[str]:
    """Pull whichever timezone field the upstream happened to populate."""
    for key in ("timezone", "time_zone", "tz", "timezone_name", "datetime_start_timezone"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def is_hour_in_window(hour: int, from_hour: int, to_hour: int) -> bool:
    """Inclusive hour window that also supports ranges crossing midnight."""
    if from_hour <= to_hour:
        return from_hour <= hour <= to_hour
    return hour >= from_hour or hour <= to_hour


def schedule_slot_key(moment: datetime) -> str:
    """Identity of an hourly slot, used to make schedule runs idempotent."""
    return moment.strftime("%Y-%m-%dT%H")
