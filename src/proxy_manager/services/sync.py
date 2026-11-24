from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from proxy_manager.provider.client import ProxySellerClient
from proxy_manager.provider.errors import ProviderError
from proxy_manager.provider.schemas import ProxyItem, ProxyPage
from proxy_manager.security.credentials import StoredCredential
from proxy_manager.security.dpapi import PasswordProtectionError, protect_password
from proxy_manager.storage.database import Database
from proxy_manager.storage.models import Operation, Proxy, SyncRun, utc_now


class SyncCancelled(Exception):
    pass


@dataclass(frozen=True)
class SyncResult:
    outcome: str
    count: int | None = None
    reason: str | None = None


def _ensure_active(cancel: Event) -> None:
    if cancel.is_set():
        raise SyncCancelled


def _validate_page(page: ProxyPage, expected: int, first: ProxyPage) -> None:
    page_count = (first.total + first.per_page - 1) // first.per_page
    expected_items = min(first.per_page, max(0, first.total - (expected - 1) * first.per_page))
    if (
        page.page != expected
        or page.per_page != first.per_page
        or page.total != first.total
        or page.total_pages != first.total_pages
        or (first.total == 0 and first.total_pages not in (0, 1))
        or (first.total > 0 and first.total_pages != page_count)
        or len(page.items) != expected_items
    ):
        raise ProviderError("inconsistent_pagination")


def _read_all(key: str, per_page: int, cancel: Event) -> list[ProxyItem]:
    def wait(seconds: float) -> None:
        if cancel.wait(seconds):
            raise SyncCancelled

    with ProxySellerClient(wait=wait) as client:
        _ensure_active(cancel)
        first = client.list_isp(key, 1, per_page)
        _validate_page(first, 1, first)
        items = list(first.items)
        for number in range(2, first.total_pages + 1):
            _ensure_active(cancel)
            page = client.list_isp(key, number, per_page)
            _validate_page(page, number, first)
            items.extend(page.items)
    if len({item.provider_id for item in items}) != len(items):
        raise ProviderError("duplicate_proxy_id")
    return items


class SyncService:
    def __init__(self, database: Database, per_page: int = 100):
        self.database = database
        self.per_page = per_page
        self._lock = Lock()

    def run(self, credential: StoredCredential, cancel: Event | None = None) -> SyncResult:
        if not self._lock.acquire(blocking=False):
            return SyncResult("failed", reason="sync_in_progress")
        try:
            return self._run(credential, cancel if cancel is not None else Event())
        finally:
            self._lock.release()

    def _run(self, credential: StoredCredential, cancel: Event) -> SyncResult:
        started = utc_now()
        try:
            items = _read_all(credential.key, self.per_page, cancel)
            rows = []
            for item in items:
                _ensure_active(cancel)
                rows.append(Proxy(
                    profile_id=credential.profile_id, provider_id=item.provider_id,
                    address=item.address, port_http=item.port_http, port_socks=item.port_socks,
                    username=item.username, password_encrypted=protect_password(item.password),
                    country=item.country, status=item.status, order_id=item.order_id,
                    expires_on=item.expires_on,
                ))
            result = SyncResult("success", count=len(rows))
            _ensure_active(cancel)
            self._record(credential.profile_id, started, result, rows)
            return result
        except SyncCancelled:
            result = SyncResult("cancelled")
        except ProviderError as error:
            reason = error.kind
            if error.kind == "http_error":
                reason = f"http_error_{error.status}"
            result = SyncResult("failed", reason=reason)
        except PasswordProtectionError:
            result = SyncResult("failed", reason="password_encryption_failed")
        except SQLAlchemyError:
            result = SyncResult("failed", reason="database_write_failed")
        self._record(credential.profile_id, started, result)
        return result

    def _record(
        self, profile_id: str, started: datetime, result: SyncResult,
        rows: list[Proxy] | None = None,
    ) -> None:
        finished = utc_now()
        with self.database.sessions.begin() as session:
            if rows is not None:
                session.execute(delete(Proxy).where(Proxy.profile_id == profile_id))
                session.add_all(rows)
            session.add(SyncRun(
                profile_id=profile_id, started_at=started, finished_at=finished,
                outcome=result.outcome, count=result.count, reason=result.reason,
            ))
            session.add(Operation(happened_at=finished, action="sync", outcome=result.outcome))
