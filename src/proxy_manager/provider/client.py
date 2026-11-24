from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import sleep
from urllib.parse import quote

import httpx

from proxy_manager.logging_config import suppress_http_diagnostics
from proxy_manager.provider.errors import ProviderError
from proxy_manager.provider.schemas import ProxyPage, parse_page

ORIGIN = "https://proxy-seller.com"
MAX_RETRY_WAIT = 60


def validate_key(key: str) -> None:
    if not key or key in {".", ".."} or any(
        not ch.isprintable() or ch.isspace() for ch in key
    ):
        raise ProviderError("invalid_key")


def _retry_delay(value: str, attempt: int) -> float:
    value = value.strip()
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0")
        return float(digits or 0) if len(digits) <= 2 else MAX_RETRY_WAIT + 1
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is not None:
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        pass
    return float(2 ** attempt)


class ProxySellerClient:
    def __init__(self, wait: Callable[[float], None] = sleep):
        suppress_http_diagnostics()
        self._http = httpx.Client(
            follow_redirects=False, trust_env=False, timeout=httpx.Timeout(15, connect=8),
        )
        self._wait = wait

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._http.close()

    def list_isp(self, key: str, page: int, per_page: int = 100) -> ProxyPage:
        validate_key(key)
        if page < 1 or per_page < 1:
            raise ValueError("invalid pagination")
        url = f"{ORIGIN}/personal/api/v2/{quote(key, safe='')}/proxy/list/isp"
        for attempt in range(4):
            response = self._request(url, page, per_page)
            if response.status_code != 429:
                break
            delay = _retry_delay(response.headers.get("Retry-After", ""), attempt)
            if attempt == 3 or delay > MAX_RETRY_WAIT:
                raise ProviderError("rate_limited", 429)
            self._wait(delay)
        if response.status_code != 200:
            raise ProviderError("http_error", response.status_code)
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            payload = None
        return parse_page(payload)

    def _request(self, url: str, page: int, per_page: int) -> httpx.Response:
        try:
            return self._http.get(url, params={"page": page, "per_page": per_page})
        except httpx.TimeoutException:
            kind = "timeout"
        except (httpx.HTTPError, httpx.InvalidURL):
            kind = "transport_error"
        raise ProviderError(kind)
