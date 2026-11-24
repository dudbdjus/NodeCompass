from contextlib import closing
from hashlib import file_digest
import sqlite3

from alembic import command
import pytest
from sqlalchemy import URL, create_engine, inspect, text

from proxy_manager.security.dpapi import PasswordProtectionError, protect_password, unprotect_password
from proxy_manager.storage import database as storage
from proxy_manager.storage.database import Database, migrate, migration_config
from proxy_manager.storage.upgrade_backup import CHECK_COLUMNS, MigrationVerificationError


def read_database(path):
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))


def seed_legacy(path, revision="0003", ciphertext=None):
    engine = create_engine(URL.create("sqlite", database=str(path)), connect_args={"autocommit": False})
    config = migration_config()
    password = "fixture-password-加密"
    ciphertext = protect_password(password) if ciphertext is None else ciphertext
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
            connection.exec_driver_sql(
                "INSERT INTO proxies "
                "(profile_id, provider_id, address, port_http, port_socks, username, password_encrypted,"
                " country, status, order_id, expires_on, checked_at, check_result, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("profile", "p1", "192.0.2.1", 8080, 1080, "login", ciphertext, "USA", "Active",
                 "order-1", "2026-12-31", "2026-11-21 03:00:00", "ok", 18),
            )
            if revision != "0001":
                connection.exec_driver_sql("UPDATE proxies SET check_protocol='http'")
            if revision == "0003":
                connection.exec_driver_sql(
                    "INSERT INTO proxy_annotations VALUES ('profile', 'p1', '地区', '标签')"
                )
            connection.exec_driver_sql(
                "INSERT INTO sync_runs VALUES (1, 'profile', '2026-11-21', '2026-11-21', 'success', 1, NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO operations VALUES (10, '2026-11-21', 'sync', 'success'), "
                "(11, '2026-11-21', 'check', 'ok'), (12, '2026-11-21', 'export', 'failed')"
            )
    finally:
        engine.dispose()
    return password, ciphertext


def test_blank_database_and_repeat_startup(tmp_path):
    path = tmp_path / "proxy data % test.sqlite3"
    database = Database(path)
    try:
        assert set(inspect(database.engine).get_table_names()) == {
            "alembic_version", "operations", "proxies", "proxy_annotations", "sync_runs",
            "vps_profiles", "kiwivm_service_snapshots", "kiwivm_usage_points",
            "vps_network_samples", "vps_network_rollups", "vps_system_samples", "monitoring_sync_runs",
        }
        assert not CHECK_COLUMNS.intersection(column["name"] for column in inspect(database.engine).get_columns("proxies"))
        with database.engine.begin() as connection:
            connection.execute(text("INSERT INTO operations VALUES (1, '2026-11-21', 'sync', 'success')"))
        migrate(database.engine)
    finally:
        database.close()
    reopened = Database(path)
    reopened.close()
    with read_database(path) as current:
        assert current.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005",)
        assert current.execute("SELECT action FROM operations").fetchall() == [("sync",)]
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("revision", ["0001", "0002", "0003"])
def test_legacy_upgrade_checks_backup_and_all_retained_rows(tmp_path, revision):
    path = tmp_path / "legacy.sqlite3"
    password, ciphertext = seed_legacy(path, revision)
    with read_database(path) as before:
        before_columns = [row[1] for row in before.execute("PRAGMA table_info(proxies)")]
        expected_proxies = [
            tuple(value for column, value in zip(before_columns, row) if column not in CHECK_COLUMNS)
            for row in before.execute("SELECT * FROM proxies")
        ]
        expected_sync_runs = before.execute("SELECT * FROM sync_runs").fetchall()
        expected_operations = before.execute("SELECT * FROM operations WHERE action != 'check'").fetchall()
        expected_annotations = (
            before.execute("SELECT * FROM proxy_annotations").fetchall() if revision == "0003" else []
        )
        assert unprotect_password(before.execute("SELECT password_encrypted FROM proxies").fetchone()[0]) == password

    for _ in range(2):
        database = Database(path)
        database.close()
    [backup] = (tmp_path / "backups").glob("*.sqlite3")
    with backup.open("rb") as handle:
        assert file_digest(handle, "sha256").hexdigest() == backup.with_suffix(".sqlite3.sha256").read_text().split()[0]
    with read_database(backup) as saved, read_database(path) as current:
        assert saved.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert saved.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
        assert saved.execute("SELECT outcome FROM operations WHERE action='check'").fetchall() == [("ok",)]
        assert current.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert current.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005",)
        assert current.execute("SELECT * FROM proxies").fetchall() == expected_proxies
        assert current.execute("SELECT * FROM proxy_annotations").fetchall() == expected_annotations
        assert current.execute("SELECT * FROM sync_runs").fetchall() == expected_sync_runs
        assert current.execute("SELECT * FROM operations").fetchall() == expected_operations
        after = current.execute("SELECT password_encrypted FROM proxies").fetchone()[0]
        assert after == ciphertext
        assert unprotect_password(after) == password


def test_post_migration_mismatch_rolls_back_ddl_and_operations(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path)
    original = storage.verify_snapshot

    def corrupt_after_upgrade(conn, backup, *, migrated):
        if migrated:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005",)
            assert conn.execute("SELECT outcome FROM operations WHERE action='check'").fetchone() is None
            conn.execute("UPDATE proxies SET port_http=9000")
        original(conn, backup, migrated=migrated)

    monkeypatch.setattr(storage, "verify_snapshot", corrupt_after_upgrade)
    with pytest.raises(MigrationVerificationError, match="sqlite_preserved_values_mismatch"):
        Database(path)
    with read_database(path) as current:
        assert current.execute("SELECT version_num FROM alembic_version").fetchone() == ("0003",)
        assert current.execute("SELECT port_http, check_protocol FROM proxies").fetchone() == (8080, "http")
        assert current.execute("SELECT outcome FROM operations WHERE action='check'").fetchall() == [("ok",)]
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_unreadable_password_aborts_upgrade(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, ciphertext=b"invalid-DPAPI")
    with pytest.raises(PasswordProtectionError):
        Database(path)
    with read_database(path) as current:
        assert current.execute("SELECT version_num FROM alembic_version").fetchone() == ("0003",)
        assert current.execute("SELECT password_encrypted, check_result FROM proxies").fetchone() == (b"invalid-DPAPI", "ok")


def test_backup_integrity_failure_blocks_upgrade(tmp_path, monkeypatch):
    from proxy_manager.storage import upgrade_backup

    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path)

    def fail_integrity(connection):
        raise MigrationVerificationError("sqlite_integrity_check_failed")

    monkeypatch.setattr(upgrade_backup, "_integrity", fail_integrity)
    with pytest.raises(MigrationVerificationError, match="sqlite_integrity_check_failed"):
        Database(path)
    with read_database(path) as current:
        assert current.execute("SELECT version_num FROM alembic_version").fetchone() == ("0003",)
    assert not list((tmp_path / "backups").iterdir())
