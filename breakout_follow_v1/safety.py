"""Paper-only safety: block private order API paths."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from . import config


class LiveTradingForbidden(RuntimeError):
    """Raised when any call path attempts a private/order endpoint."""


def assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    path = parsed.path or ""
    lower = path.lower()
    for bad in config.FORBIDDEN_ORDER_PATH_SUBSTRINGS:
        if bad.lower() in lower:
            raise LiveTradingForbidden(f"blocked order/private path: {path}")
    # allowlist check for fapi paths we use
    if path.startswith("/fapi/") and path not in config.ALLOWED_PUBLIC_PATHS:
        # premiumIndex etc already allowed; reject unknown fapi writes
        if any(x in lower for x in ("order", "position", "leverage", "listenkey", "userdata")):
            raise LiveTradingForbidden(f"blocked unknown trading path: {path}")


def guard_session_request(method: str, url: str, **kwargs: Any) -> None:
    m = (method or "GET").upper()
    if m not in ("GET", "HEAD"):
        raise LiveTradingForbidden(f"only GET/HEAD allowed in paper mode, got {m} {url}")
    assert_public_url(url)


def scan_source_for_order_calls(source: str) -> list[str]:
    """Static-ish scan of source text for forbidden substrings (test helper)."""
    hits = []
    for bad in config.FORBIDDEN_ORDER_PATH_SUBSTRINGS:
        if bad in source:
            hits.append(bad)
    for needle in ("create_order", "place_order", "newOrder", "signed_request", "API_SECRET", "api_secret"):
        if needle in source:
            hits.append(needle)
    return hits
