import pytest

from proxy_manager.storage.database import Database


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "fixture.sqlite3")
    try:
        yield db
    finally:
        db.close()
