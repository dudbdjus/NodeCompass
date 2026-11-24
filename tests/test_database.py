from proxy_manager.storage.database import Database
from proxy_manager.storage.models import Proxy
from proxy_manager.storage.repositories import Repository


def test_repository_scopes_proxies_to_profile(tmp_path):
    database = Database(tmp_path / "proxies.sqlite3")
    try:
        with database.sessions.begin() as session:
            session.add(Proxy(profile_id="first", provider_id="1", address="192.0.2.1"))
            session.add(Proxy(profile_id="second", provider_id="1", address="192.0.2.2"))
        repository = Repository(database)
        assert [row.address for row in repository.list_proxies("first")] == ["192.0.2.1"]
        assert repository.get_proxy("second", "1").address == "192.0.2.2"
        repository.log_operation("test", "ok")
        assert repository.operations()[0].action == "test"
    finally:
        database.close()
