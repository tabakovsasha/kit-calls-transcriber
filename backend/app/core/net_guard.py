"""SSRF protection for user-supplied Voximplant Kit hosts.

A user controls api_host, so every outbound call must be validated:
- hostname-only (no scheme, credentials, path or port tricks);
- HTTPS enforced by construction;
- DNS resolved and checked against private/loopback/link-local ranges;
- optional strict allowlist.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.errors import UpstreamError, UPSTREAM_BLOCKED_HOST

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_host(raw_host: str) -> str:
    """Reduce arbitrary user input to a bare, validated hostname."""
    host = (raw_host or "").strip().lower()
    if not host:
        raise UpstreamError("Не указан хост API (host)", code=UPSTREAM_BLOCKED_HOST)

    # Accept values pasted with a scheme or trailing path and strip them.
    if "//" in host:
        host = urlparse(host if "://" in host else f"https://{host}").netloc or host
    host = host.split("/")[0]

    if "@" in host:
        raise UpstreamError("Хост не должен содержать credentials", code=UPSTREAM_BLOCKED_HOST)
    if ":" in host:
        raise UpstreamError("Хост не должен содержать порт", code=UPSTREAM_BLOCKED_HOST)
    if not _HOSTNAME_RE.match(host):
        raise UpstreamError(f"Неверный формат хоста: {raw_host}", code=UPSTREAM_BLOCKED_HOST)

    return host


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_host_allowed(host: str) -> str:
    """Validate a normalized host against the allowlist and IP policy.

    Called immediately before each outbound request, which also limits the
    window for DNS rebinding.
    """
    settings = get_settings()
    normalized = normalize_host(host)

    allowlist = settings.upstream_host_allowlist
    if allowlist and normalized not in allowlist:
        raise UpstreamError(
            f"Хост {normalized} не входит в разрешенный список",
            code=UPSTREAM_BLOCKED_HOST,
        )

    if settings.allow_private_upstream_hosts:
        return normalized

    try:
        resolved = socket.getaddrinfo(normalized, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UpstreamError(
            f"Не удалось разрешить хост {normalized}", code=UPSTREAM_BLOCKED_HOST
        ) from exc

    for entry in resolved:
        address = entry[4][0]
        if _is_blocked_ip(address):
            raise UpstreamError(
                f"Хост {normalized} указывает на закрытый адрес и заблокирован",
                code=UPSTREAM_BLOCKED_HOST,
            )

    return normalized


def build_api_url(host: str, path: str) -> str:
    """Build an HTTPS URL for a validated host."""
    normalized = assert_host_allowed(host)
    return f"https://{normalized}/{path.lstrip('/')}"


def is_upstream_media_url(url: str, host: str) -> bool:
    """Check that a record URL belongs to the connection's upstream host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    netloc = (parsed.hostname or "").lower()
    normalized_host = normalize_host(host)
    root = ".".join(normalized_host.split(".")[-2:])
    return netloc == normalized_host or netloc.endswith(f".{root}")
