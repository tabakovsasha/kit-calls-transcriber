"""Async client for the Voximplant Kit history API.

Design constraints carried over from the original single-file app, hardened:
- credentials are passed in per call (never module globals, never persisted);
- every request target is validated by net_guard (SSRF protection);
- upstream failures are normalized into UpstreamError with machine codes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import aiofiles
import httpx

from app.core.errors import (
    UpstreamError,
    UPSTREAM_AUTH_FAILED,
    UPSTREAM_FORBIDDEN,
    UPSTREAM_MALFORMED_RESPONSE,
    UPSTREAM_RATE_LIMIT,
    UPSTREAM_TIMEOUT,
    UPSTREAM_TRANSPORT,
    UPSTREAM_UNAVAILABLE,
    UPSTREAM_VALIDATION_FAILED,
)
from app.core.net_guard import assert_host_allowed, build_api_url, is_upstream_media_url

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 10
MAX_SEARCH_PAGES_HARD_LIMIT = 500
SEARCH_RETRY_DELAY_SECONDS = 1.0
MAX_SEARCH_RETRIES = 2
SEARCH_TIMEOUT_SECONDS = 120
AUDIO_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class UpstreamCredentials:
    """Per-connection credentials, decrypted just-in-time for one operation."""

    api_host: str
    domain: str
    access_token: str

    def require_complete(self) -> None:
        if not self.api_host or not self.domain or not self.access_token:
            raise UpstreamError(
                "Некорректные учетные данные подключения", code=UPSTREAM_AUTH_FAILED
            )


def get_dynamic_page_budget(records_limit: int) -> int:
    safe_limit = max(1, int(records_limit or 1))
    estimated = max(10, (safe_limit // max(1, SEARCH_PAGE_SIZE)) * 5 + 10)
    return min(MAX_SEARCH_PAGES_HARD_LIMIT, estimated)


def _classify_status(response: httpx.Response) -> UpstreamError:
    status = response.status_code
    if status in (401, 407):
        return UpstreamError("Voximplant отклонил токен", code=UPSTREAM_AUTH_FAILED)
    if status == 403:
        return UpstreamError("Voximplant запретил доступ", code=UPSTREAM_FORBIDDEN)
    if status == 429:
        return UpstreamError("Voximplant ограничил частоту запросов", code=UPSTREAM_RATE_LIMIT)
    if 400 <= status < 500:
        return UpstreamError(
            f"Voximplant отклонил запрос ({status})", code=UPSTREAM_VALIDATION_FAILED
        )
    return UpstreamError(f"Voximplant недоступен ({status})", code=UPSTREAM_UNAVAILABLE)


async def _post_page(client: httpx.AsyncClient, api_url: str, form_data: dict[str, Any]) -> dict:
    """POST one search page with bounded retries on 429. Raises UpstreamError."""
    for attempt in range(MAX_SEARCH_RETRIES + 1):
        try:
            response = await client.post(
                api_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=form_data,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                "Истек таймаут запроса к Voximplant", code=UPSTREAM_TIMEOUT
            ) from exc
        except httpx.TransportError as exc:
            raise UpstreamError("Ошибка соединения с Voximplant", code=UPSTREAM_TRANSPORT) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            delay = (
                float(retry_after)
                if retry_after
                else SEARCH_RETRY_DELAY_SECONDS * (attempt + 1)
            )
            if attempt >= MAX_SEARCH_RETRIES:
                raise _classify_status(response)
            logger.warning(
                "[VOXIMPLANT] 429 rate limit, retry #%s через %.1fs", attempt + 1, delay
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code >= 400:
            raise _classify_status(response)

        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Voximplant вернул некорректный ответ", code=UPSTREAM_MALFORMED_RESPONSE
            ) from exc

    raise UpstreamError("Не удалось получить страницу звонков", code=UPSTREAM_UNAVAILABLE)


class VoximplantClient:
    """Thin wrapper bound to one set of credentials for one request lifecycle."""

    def __init__(self, credentials: UpstreamCredentials) -> None:
        credentials.require_complete()
        # Validate the host up-front; build_api_url re-validates per request.
        assert_host_allowed(credentials.api_host)
        self._creds = credentials

    @property
    def api_host(self) -> str:
        return self._creds.api_host

    def _search_url(self) -> str:
        return build_api_url(
            self._creds.api_host,
            f"api/v4/history/searchCalls?domain={self._creds.domain}",
        )

    async def search_calls_page(
        self,
        client: httpx.AsyncClient,
        *,
        from_value: str,
        to_value: str,
        scenario_id: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        form_data: dict[str, Any] = {
            "access_token": self._creds.access_token,
            "from": from_value,
            "to": to_value,
            "with_scenarios": "true",
            "limit": SEARCH_PAGE_SIZE,
        }
        if scenario_id:
            form_data["scenario_ids"] = f"[{scenario_id}]"
        if cursor:
            form_data["cursor"] = cursor
        return await _post_page(client, self._search_url(), form_data)

    async def verify(self) -> bool:
        """Cheap credential check used when creating or rotating a connection."""
        async with httpx.AsyncClient(timeout=30) as client:
            await self.search_calls_page(
                client,
                from_value="2000-01-01 00:00:00",
                to_value="2000-01-01 00:01:00",
            )
        return True

    async def download_audio(self, record_url: str, destination: Path) -> None:
        """Download a recording, injecting credentials the way the Kit API expects."""
        if not record_url:
            raise UpstreamError("Пустой URL записи", code=UPSTREAM_VALIDATION_FAILED)
        if not is_upstream_media_url(record_url, self._creds.api_host):
            raise UpstreamError(
                "URL записи не принадлежит хосту подключения",
                code=UPSTREAM_VALIDATION_FAILED,
            )

        destination = Path(destination)
        # Reuse a previous download so playback stays fast during transcription.
        if destination.exists() and destination.stat().st_size > 0:
            return

        call_id = parse_qs(urlparse(record_url).query).get("call_id", [None])[0]
        payload: dict[str, Any] = {
            "access_token": self._creds.access_token,
            "domain": self._creds.domain,
            "api_host": self._creds.api_host,
        }
        if call_id:
            payload["call_id"] = call_id

        form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        attempts: list[tuple[str, dict[str, Any]]] = [
            ("post", {"data": payload, "headers": form_headers}),
            ("get", {"params": payload}),
        ]

        async with httpx.AsyncClient(timeout=AUDIO_TIMEOUT_SECONDS) as client:
            last_error: Optional[str] = None
            for method, kwargs in attempts:
                try:
                    if method == "post":
                        response = await client.post(record_url, **kwargs)
                    else:
                        response = await client.get(record_url, **kwargs)

                    if response.status_code < 400 and response.content:
                        async with aiofiles.open(destination, "wb") as out_file:
                            await out_file.write(response.content)
                        return

                    last_error = f"{response.status_code} {response.reason_phrase}"
                    logger.warning("[AUDIO] Загрузка не удалась (%s): %s", method, last_error)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = str(exc)
                    logger.warning("[AUDIO] Ошибка загрузки через %s: %s", method, exc)

        raise UpstreamError(
            f"Не удалось скачать аудио: {last_error}", code=UPSTREAM_UNAVAILABLE
        )
