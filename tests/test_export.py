import csv

import pytest

from proxy_manager.security.dpapi import protect_password
from proxy_manager.services.export import ExportService
from proxy_manager.storage.models import Proxy
from proxy_manager.storage.repositories import Repository


def test_txt_ipv6_and_both_protocols(database, tmp_path):
    service = ExportService(Repository(database))
    row = Proxy(address="2001:db8::1", port_http=8080, port_socks=1080, username="fixture-user")
    path = tmp_path / "out.txt"
    assert service.run(path, [row]) == 1
    assert path.read_text(encoding="utf-8").splitlines() == [
        "http://[2001:db8::1]:8080", "socks5://[2001:db8::1]:1080",
    ]


def test_csv_unicode_escaping_and_no_password(database, tmp_path):
    repo = Repository(database)
    path = tmp_path / "out.csv"
    row = Proxy(
        provider_id="1", address="192.0.2.1", username=" =TEST()",
        country="中国", status='Active, "ISP"', password_encrypted=protect_password("plain-secret"),
    )
    assert ExportService(repo).run(path, [row]) == 1
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        record = next(reader)
    assert reader.fieldnames == [
        "provider_id", "address", "port_http", "port_socks", "username", "country",
        "status", "order_id", "expires_on",
    ]
    assert record["username"] == "' =TEST()"
    assert record["country"] == "中国"
    assert record["status"] == row.status
    assert b"plain-secret" not in path.read_bytes()
    assert "password" not in record
    assert repo.operations()[0].action == "export"


def test_failed_export_preserves_existing_file(database, tmp_path):
    repo = Repository(database)
    path = tmp_path / "out.txt"
    path.write_text("existing", encoding="ascii")
    with pytest.raises(ValueError):
        ExportService(repo).run(path, [Proxy(address="invalid", port_http=8080)])
    assert path.read_text(encoding="ascii") == "existing"
    assert not list(tmp_path.glob(".proxy-export-*"))
    assert repo.operations()[0].outcome == "failed"
