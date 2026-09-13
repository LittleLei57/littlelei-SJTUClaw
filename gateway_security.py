"""Small, dependency-free security helpers for the local Gateway."""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
from urllib.parse import urlsplit


_SECRET_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|tvly-[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)(access_key|ticket|token|secret|api_key)=([^&\s]+)"),
    re.compile(r'(?i)(["\']?(?:access_key|ticket|token|secret|api_key)["\']?\s*[:=]\s*["\']?)([^"\'\s,}&]+)'),
)


def redact_sensitive(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _SECRET_PATTERNS[0].sub("[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", text)
    text = _SECRET_PATTERNS[2].sub(r"\1=[REDACTED]", text)
    text = _SECRET_PATTERNS[3].sub(r"\1[REDACTED]", text)
    return text[:1000]


def remote_access_enabled() -> bool:
    return os.getenv("SJTUCLAW_ALLOW_REMOTE", "").strip().lower() in {"1", "true", "yes", "on"}


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]")
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
        return address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def same_origin(origin: str, request_scheme: str, request_host: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    expected = f"{request_scheme.lower()}://{request_host.lower()}"
    actual = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return hmac.compare_digest(actual, expected)


def add_security_headers(response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'",
    )
