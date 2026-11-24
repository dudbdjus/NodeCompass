from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, URL, create_engine
from sqlalchemy.orm import sessionmaker
from proxy_manager.storage.maintenance_lock import maintenance_lock

from proxy_manager.storage.upgrade_backup import (
    HEAD_REVISION, LEGACY_REVISIONS, MigrationVerificationError, _read_only, _revision,
    create_upgrade_backup, verify_snapshot,
)


def migration_config() -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations")).replace("%", "%%")
    )
    return config


def migrate(engine: Engine, *, allow_legacy: bool = True) -> None:
    path = Path(engine.url.database)
    with maintenance_lock(path):
        if not allow_legacy and path.is_file():
            with closing(_read_only(path)) as existing:
                if _revision(existing) in LEGACY_REVISIONS:
                    raise MigrationVerificationError("explicit_legacy_migration_required")
        _migrate_locked(engine)


def _migrate_locked(engine: Engine) -> None:
    config = migration_config()
    backup = create_upgrade_backup(Path(engine.url.database))
    with engine.begin() as connection:
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            raise MigrationVerificationError("sqlite_ddl_transaction_required")
        revision = _revision(driver)
        if backup is not None:
            verify_snapshot(driver, backup, migrated=False)
        elif revision == HEAD_REVISION:
            return
        elif revision is not None or driver.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            raise MigrationVerificationError("sqlite_backup_required")
        config.attributes["connection"] = connection
        command.upgrade(config, HEAD_REVISION)
        if not driver.in_transaction or _revision(driver) != HEAD_REVISION:
            raise MigrationVerificationError("sqlite_revision_mismatch")
        if backup is not None:
            verify_snapshot(driver, backup, migrated=True)


class Database:
    def __init__(self, path: Path, *, allow_legacy: bool = True):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            URL.create("sqlite", database=str(path)),
            connect_args={"autocommit": False, "timeout": 10},
            hide_parameters=True,
        )
        try:
            migrate(self.engine, allow_legacy=allow_legacy)
        except Exception:
            self.engine.dispose()
            raise
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()
