import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from enum import Enum
import socket
import ssl
from threading import Event
from time import sleep
from typing import Generic, TypeVar

import httpx

from proxy_manager.logging_config import suppress_http_diagnostics
from proxy_manager.provider.kiwivm_parser import (
    KiwiVMError, LiveServiceInfo, ServiceInfo, UsageHistory,
    parse_live_service_info, parse_service_info, parse_usage_history,
)
from proxy_manager.security.vps_credentials import KiwiVMCredential

_ORIGIN = "https://api.64clouds.com/v1/"
_MAX_ATTEMPTS = 3
_MAX_RETRY_WAIT = 60
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class KiwiVMEndpoint(Enum):
    SERVICE_INFO = "getServiceInfo"
    RAW_USAGE_STATS = "getRawUsageStats"
    LIVE_SERVICE_INFO = "getLiveServiceInfo"


T = TypeVar("T", ServiceInfo, UsageHistory, LiveServiceInfo)


@dataclass(frozen=True)
class Snapshot(Generic[T]):
    fetched_at_utc: datetime
    data: T


def _retry_wait(header: str, attempt: int) -> float:
    value = header.strip()
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0") or "0"
        return float(digits) if len(digits) <= 2 else _MAX_RETRY_WAIT + 1
    try:
        instant = parsedate_to_datetime(value)
        if instant.tzinfo is not None:
            return max(0.0, (instant - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        pass
    return float(2**attempt)


def _transport_kind(error: httpx.HTTPError) -> str:
    pending: list[BaseException] = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return "tls_error"
        if isinstance(current, socket.gaierror):
            return "dns_error"
        # HTTPCore retains the OS exception as an argument even when its cause is suppressed.
        pending.extend(arg for arg in current.args if isinstance(arg, BaseException))
        cause = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if cause is not None:
            pending.append(cause)
    return "timeout" if isinstance(error, httpx.TimeoutException) else "transport_error"


def _decode(response: httpx.Response, parser: Callable[..., T], secret: str) -> tuple[T | None, str]:
    try:
        payload = response.json(parse_float=Decimal)
    except (ValueError, RecursionError):
        return None, "invalid_response"
    try:
        return parser(payload, secret=secret), ""
    except KiwiVMError as error:
        # Do not propagate parser frames that contain the provider's response body.
        return None, error.kind


class KiwiVMClient:
    """Read-only KiwiVM operations using fixed HTTPS POST form requests."""

    def __init__(self, credential: KiwiVMCredential, *, wait: Callable[[float], None] = sleep,
                 cancel: Event | None = None, single_attempt: bool = False):
        if type(credential) is not KiwiVMCredential:
            raise KiwiVMError("invalid_credentials")
        suppress_http_diagnostics()
        self._credential = credential
        self._wait = wait
        self._cancel = cancel
        self._attempts = 1 if single_attempt else _MAX_ATTEMPTS
        self._closed = False
        self._runner = asyncio.Runner()
        self._client = httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(20, connect=8, write=8, pool=8),
        )
        self._snapshots: dict[KiwiVMEndpoint, Snapshot] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._runner.run(self._client.aclose())
        finally:
            self._runner.close()

    def last_success(self, endpoint: KiwiVMEndpoint) -> Snapshot | None:
        if type(endpoint) is not KiwiVMEndpoint:
            raise KiwiVMError("endpoint_not_allowed")
        return self._snapshots.get(endpoint)

    def get_service_info(self) -> Snapshot[ServiceInfo]:
        return self._fetch(KiwiVMEndpoint.SERVICE_INFO, parse_service_info)

    def get_raw_usage_stats(self) -> Snapshot[UsageHistory]:
        return self._fetch(KiwiVMEndpoint.RAW_USAGE_STATS, parse_usage_history)

    def get_live_service_info(self) -> Snapshot[LiveServiceInfo]:
        return self._fetch(KiwiVMEndpoint.LIVE_SERVICE_INFO, parse_live_service_info)

    def _check_cancelled(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise KiwiVMError("cancelled")

    def _pause(self, seconds: float) -> None:
        if self._cancel is None:
            self._wait(seconds)
        else:
            self._cancel.wait(seconds)
        self._check_cancelled()

    def _fetch(self, endpoint: KiwiVMEndpoint, parser: Callable[..., T]) -> Snapshot[T]:
        if type(endpoint) is not KiwiVMEndpoint:
            raise KiwiVMError("endpoint_not_allowed")
        if self._closed:
            raise KiwiVMError("client_closed")
        for attempt in range(self._attempts):
            self._check_cancelled()
            response, kind = self._post(endpoint)
            self._check_cancelled()
            if response is None:
                if kind in {"timeout", "transport_error", "dns_error"} and attempt + 1 < self._attempts:
                    self._pause(float(2**attempt))
                    continue
                raise KiwiVMError(kind)
            status = response.status_code
            if status == 200:
                break
            if status in _RETRYABLE_HTTP and attempt + 1 < self._attempts:
                delay = _retry_wait(response.headers.get("Retry-After", ""), attempt)
                if delay <= _MAX_RETRY_WAIT:
                    self._pause(delay)
                    continue
            del response
            if status in (401, 403):
                raise KiwiVMError("authentication_failed", status)
            raise KiwiVMError("rate_limited" if status == 429 else "http_error", status)

        data, kind = _decode(response, parser, self._credential.api_key)
        del response
        if data is None:
            raise KiwiVMError(kind)
        snapshot = Snapshot(datetime.now(UTC), data)
        if not isinstance(data, UsageHistory) or data.format_recognized:
            self._snapshots[endpoint] = snapshot
        return snapshot

    def _post(self, endpoint: KiwiVMEndpoint) -> tuple[httpx.Response | None, str]:
        if type(endpoint) is not KiwiVMEndpoint:
            raise KiwiVMError("endpoint_not_allowed")
        try:
            return self._runner.run(self._bounded_request(endpoint))
        except TimeoutError:
            return None, "timeout"
        except httpx.HTTPError as error:
            # HTTP exceptions may retain the form and response body.
            return None, _transport_kind(error)

    async def _bounded_request(self, endpoint):
        async with asyncio.timeout(35):
            task = asyncio.create_task(self._request(endpoint))
            try:
                if self._cancel is not None:
                    while not task.done():
                        self._check_cancelled()
                        await asyncio.wait((task,), timeout=0.05)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def _request(self, endpoint):
        async with self._client.stream(
            "POST", _ORIGIN + endpoint.value,
            data={"veid": self._credential.veid, "api_key": self._credential.api_key},
        ) as response:
            content = bytearray()
            if response.status_code == 200:
                async for chunk in response.aiter_bytes():
                    self._check_cancelled()
                    if len(content) + len(chunk) > 16 * 1024 * 1024:
                        return None, "invalid_response"
                    content.extend(chunk)
            headers = {"Retry-After": response.headers["Retry-After"]} if "Retry-After" in response.headers else {}
            return httpx.Response(response.status_code, headers=headers, content=bytes(content)), ""
