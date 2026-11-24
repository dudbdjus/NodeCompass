from contextlib import closing
from hashlib import file_digest
import sqlite3

from alembic import command
from alembic.operations import Operations
import pytest
from sqlalchemy import URL, create_engine

from proxy_manager.security.dpapi import protect_password, unprotect_password
from proxy_manager.storage import database as storage
from proxy_manager.storage import upgrade_backup


_LEGACY = {"alembic_version", "operations", "proxies", "proxy_annotations", "sync_runs"}
_VPS = {"vps_profiles", "kiwivm_service_snapshots", "kiwivm_usage_points",
        "vps_network_samples", "vps_network_rollups", "vps_system_samples", "monitoring_sync_runs"}
_KEYS = {"proxies": "profile_id, provider_id", "proxy_annotations": "profile_id, provider_id",
         "operations": "id", "sync_runs": "id"}


def read(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, autocommit=True))


def capture(path):
    with read(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        rows = {table: conn.execute(f"SELECT * FROM {table} ORDER BY {_KEYS[table]}").fetchall() for table in _KEYS}
        columns = {table: conn.execute(f"PRAGMA table_info({table})").fetchall() for table in _KEYS}
        objects = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        passwords = [unprotect_password(blob) for _, blob in conn.execute(
            "SELECT provider_id, password_encrypted FROM proxies ORDER BY profile_id, provider_id"
        )]
        assert len(rows["proxies"]) == 21
        return revision, columns, rows, objects, passwords


@pytest.fixture
def old_copy(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    engine = create_engine(URL.create("sqlite", database=str(path)), connect_args={"autocommit": False})
    with engine.begin() as transaction:
        config = storage.migration_config()
        config.attributes["connection"] = transaction
        command.upgrade(config, "0004")
        cipher = protect_password("test-only")
        transaction.exec_driver_sql(
            "INSERT INTO proxies (profile_id,provider_id,address,port_http,port_socks,username,password_encrypted,"
            "country,status,order_id,expires_on) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [("profile", str(n), "192.0.2.1", 8080, 1080, "tester", cipher, "USA", "Active",
              f"order-{n}", "2026-12-31") for n in range(21)]
        )
        transaction.exec_driver_sql("INSERT INTO proxy_annotations VALUES ('profile','1','地区','标签')")
        transaction.exec_driver_sql("INSERT INTO sync_runs VALUES (1,'profile','2026-11-21','2026-11-21','success',21,NULL)")
        transaction.exec_driver_sql("INSERT INTO operations VALUES (1,'2026-11-21','sync','success')")
    engine.dispose()
    return path


def verify_copy_matches(after, before):
    assert after[1] == before[1]
    # Keep the assertion messages free of any field contents or plaintext credentials.
    if any(after[2][table] != before[2][table] for table in _KEYS):
        pytest.fail("a retained row differs", pytrace=False)
    if any(new != old for new, old in zip(after[4], before[4], strict=True)):
        pytest.fail("decrypted password differs", pytrace=False)


def test_upgrade_0004_backup_and_all_preserved_fields(old_copy):
    before = capture(old_copy)
    assert before[0] == [("0004",)]
    storage.Database(old_copy).close()
    after = capture(old_copy)
    assert after[0] == [("0005",)]
    verify_copy_matches(after, before)
    old_objects = {row[1]: row for row in before[3]}
    assert {row[1]: row for row in after[3] if row[1] in old_objects} == old_objects
    assert {row[1] for row in after[3] if row[0] == "table"} == _VPS | _LEGACY
    [backup] = (old_copy.parent / "backups").glob("*.sqlite3")
    assert "-before-0.3.0-" in backup.name
    with backup.open("rb") as handle:
        assert file_digest(handle, "sha256").hexdigest() == backup.with_suffix(".sqlite3.sha256").read_text().split()[0]
    saved = capture(backup)
    assert saved[0] == before[0]
    verify_copy_matches(saved, before)
    storage.Database(old_copy).close()
    assert len(list((old_copy.parent / "backups").glob("*.sqlite3"))) == 1


def test_mid_ddl_failure_restores_real_sqlite_schema_version_and_rows(old_copy, monkeypatch):
    before = capture(old_copy)
    create = Operations.create_table
    completed = []

    def inject(ops, name, *args, **kwargs):
        created = create(ops, name, *args, **kwargs)
        completed.append(name)
        if len(completed) == 3:
            assert set(completed) <= _VPS
            conn = ops.get_bind()
            assert conn.connection.driver_connection.in_transaction
            names = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
            assert set(completed) <= names
            raise RuntimeError("simulated_mid_ddl_failure")
        return created

    monkeypatch.setattr(Operations, "create_table", inject)
    with pytest.raises(RuntimeError, match="simulated_mid_ddl_failure"):
        storage.Database(old_copy)
    assert len(completed) == 3
    after = capture(old_copy)
    assert after[0] == [("0004",)]
    assert after[3] == before[3]
    verify_copy_matches(after, before)
    assert len(list((old_copy.parent / "backups").glob("*.sqlite3"))) == 1


def test_failure_after_alembic_version_update_is_rolled_back(old_copy, monkeypatch):
    before = capture(old_copy)
    upgrade = command.upgrade

    def change_revision(config, revision):
        upgrade(config, revision)
        connection = config.attributes["connection"]
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0005"
        connection.exec_driver_sql("UPDATE alembic_version SET version_num='bogus'")

    monkeypatch.setattr(command, "upgrade", change_revision)
    with pytest.raises(upgrade_backup.MigrationVerificationError, match="sqlite_revision_mismatch"):
        storage.Database(old_copy)
    after = capture(old_copy)
    assert after[0] == before[0]
    assert after[3] == before[3]
    verify_copy_matches(after, before)


@pytest.mark.parametrize("error", ["integrity", "checksum"])
def test_failed_backup_blocks_0005(old_copy, monkeypatch, error):
    before = capture(old_copy)
    if error == "integrity":
        def integrity(_):
            raise upgrade_backup.MigrationVerificationError("sqlite_integrity_check_failed")
        monkeypatch.setattr(upgrade_backup, "_integrity", integrity)
    else:
        backup = storage.create_upgrade_backup

        def corrupt_checksum(path):
            file = backup(path)
            file.with_suffix(".sqlite3.sha256").write_text("bad", encoding="ascii")
            return file

        monkeypatch.setattr(storage, "create_upgrade_backup", corrupt_checksum)
    with pytest.raises(upgrade_backup.MigrationVerificationError):
        storage.Database(old_copy)
    after = capture(old_copy)
    assert after[0] == before[0]
    assert after[3] == before[3]
    verify_copy_matches(after, before)
