from threading import Event

import httpx
import pytest
from sqlalchemy import text

from proxy_manager.security.credentials import StoredCredential
from proxy_manager.security.dpapi import unprotect_password
from proxy_manager.services.sync import SyncService
from proxy_manager.storage.repositories import Repository

KEY = "fixture-key-only"
CREDENTIAL = StoredCredential("fixture-profile", KEY)
BASE = f"https://proxy-seller.com/personal/api/v2/{KEY}/proxy/list/isp"


def response(page, *, item_id=None, total=2, total_pages=2):
    items = []
    if item_id:
        items = [{
            "id": item_id, "ip": f"192.0.2.{item_id}", "port_http": 8080,
            "login": "test-user", "password": "proxy-secret",
        }]
    return httpx.Response(200, json={
        "status": "success", "errors": [],
        "data": {
            "items": items, "page": page, "per_page": 1,
            "total": total, "total_pages": total_pages,
        },
    })


@pytest.fixture
def synced_database(database, respx_mock):
    first = respx_mock.get(f"{BASE}?page=1&per_page=1").mock(return_value=response(1, item_id="1"))
    second = respx_mock.get(f"{BASE}?page=2&per_page=1").mock(return_value=response(2, item_id="2"))
    service = SyncService(database, per_page=1)
    assert service.run(CREDENTIAL).count == 2
    assert first.call_count == second.call_count == 1
    return service, first, second


def test_sync_multi_page_encrypts_password_and_records_history(database, synced_database):
    repo = Repository(database)
    rows = repo.list_proxies(CREDENTIAL.profile_id)
    assert [row.provider_id for row in rows] == ["1", "2"]
    assert all(unprotect_password(row.password_encrypted) == "proxy-secret" for row in rows)
    assert repo.sync_history(CREDENTIAL.profile_id)[0].outcome == "success"
    data = database.engine.url.database
    with open(data, "rb") as handle:
        contents = handle.read()
    assert b"proxy-secret" not in contents
    assert KEY.encode() not in contents


@pytest.mark.parametrize(("kind", "failure"), [
    ("http_error_503", lambda: httpx.Response(503)),
    ("business_error", lambda: httpx.Response(200, json={"status": "error"})),
    ("inconsistent_pagination", lambda: response(2, item_id="2", total=3, total_pages=3)),
    ("duplicate_proxy_id", lambda: response(2, item_id="3")),
])
def test_second_page_failure_preserves_snapshot(database, synced_database, kind, failure):
    service, first, second = synced_database
    first.mock(return_value=response(1, item_id="3"))
    second.mock(return_value=failure())
    assert service.run(CREDENTIAL).reason == kind
    repo = Repository(database)
    assert [row.provider_id for row in repo.list_proxies(CREDENTIAL.profile_id)] == ["1", "2"]
    assert repo.sync_history(CREDENTIAL.profile_id)[0].outcome == "failed"


def test_transaction_rolls_back_on_insert_failure(database, synced_database):
    service, first, _ = synced_database
    first.mock(return_value=response(1, item_id="3", total=1, total_pages=1))
    with database.engine.begin() as connection:
        connection.execute(text(
            "CREATE TRIGGER test_abort BEFORE INSERT ON proxies "
            "BEGIN SELECT RAISE(ABORT, 'test'); END"
        ))
    assert service.run(CREDENTIAL).reason == "database_write_failed"
    assert [row.provider_id for row in Repository(database).list_proxies(CREDENTIAL.profile_id)] == ["1", "2"]


def test_empty_snapshot_and_cancellation(database, synced_database):
    service, first, _ = synced_database
    cancel = Event()
    cancel.set()
    assert service.run(CREDENTIAL, cancel).outcome == "cancelled"
    assert len(Repository(database).list_proxies(CREDENTIAL.profile_id)) == 2
    first.mock(return_value=response(1, total=0, total_pages=0))
    assert service.run(CREDENTIAL).count == 0
    assert not Repository(database).list_proxies(CREDENTIAL.profile_id)

def test_annotations_survive_missing_snapshot_and_return(database, synced_database):
    service, first, second = synced_database
    repo = Repository(database)
    proxy = repo.get_proxy(CREDENTIAL.profile_id, "1")
    repo.save_annotation(proxy, "region", "加州")
    repo.save_annotation(proxy, "tag", "测试标签")

    first.mock(return_value=response(1, total=0, total_pages=0))
    assert service.run(CREDENTIAL).count == 0
    assert repo.get_proxy(CREDENTIAL.profile_id, "1") is None

    first.mock(return_value=response(1, item_id="1"))
    second.mock(return_value=response(2, item_id="2"))
    assert service.run(CREDENTIAL).count == 2
    restored = repo.get_proxy(CREDENTIAL.profile_id, "1")
    assert (restored.region, restored.tag) == ("加州", "测试标签")
    repo.save_annotation(restored, "tag", "")
    cleared = repo.get_proxy(CREDENTIAL.profile_id, "1")
    assert (cleared.region, cleared.tag) == ("加州", "")
