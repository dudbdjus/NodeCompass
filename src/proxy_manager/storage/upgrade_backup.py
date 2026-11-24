from contextlib import closing
from datetime import UTC, datetime
from hashlib import file_digest
from itertools import zip_longest
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from time import monotonic
from uuid import uuid4

from proxy_manager.security.dpapi import unprotect_password

HEAD_REVISION = "0005"
LEGACY_REVISIONS = {"0001", "0002", "0003", "0004"}
CHECK_COLUMNS = {"checked_at", "check_result", "check_protocol", "latency_ms"}
TABLE_KEYS = {
    "proxies": "profile_id, provider_id",
    "proxy_annotations": "profile_id, provider_id",
    "operations": "id",
    "sync_runs": "id",
}


class MigrationVerificationError(RuntimeError):
    pass


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, autocommit=True)


def _revision(connection: sqlite3.Connection) -> str | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
    ).fetchone()
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall() if exists else []
    if len(rows) > 1:
        raise MigrationVerificationError("sqlite_revision_mismatch")
    return rows[0][0] if rows else None


def _integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise MigrationVerificationError("sqlite_integrity_check_failed")


def create_upgrade_backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    with closing(_read_only(path)) as source:
        if _revision(source) not in LEGACY_REVISIONS:
            return None
        return verified_backup(path, source, "before-0.3.0")


def verified_backup(path, source, label, *, cancel=None):
    directory = path.parent / "backups"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = directory / f"{path.stem}-{label}-{stamp}-{uuid4().hex}.sqlite3"
    with NamedTemporaryFile(dir=directory, prefix=".proxy-backup-", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with closing(sqlite3.connect(temporary, autocommit=True)) as destination:
            deadline = monotonic() + 30

            def check_deadline(status, remaining, total):
                if cancel is not None and cancel.is_set():
                    raise MigrationVerificationError("sqlite_backup_cancelled")
                if status != sqlite3.SQLITE_DONE and monotonic() >= deadline:
                    raise MigrationVerificationError("sqlite_backup_timeout")

            source.backup(destination, pages=128, progress=check_deadline)
            if cancel is not None:
                destination.set_progress_handler(cancel.is_set, 1000)
            _integrity(destination)
        with temporary.open("rb") as handle:
            digest = file_digest(handle, "sha256").hexdigest()
        temporary.rename(backup)
        with backup.with_suffix(".sqlite3.sha256").open("x", encoding="ascii") as checksum:
            checksum.write(f"{digest}  {backup.name}\n")
        verify_backup_digest(backup)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def verify_backup_digest(backup):
    with backup.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    expected = backup.with_suffix(".sqlite3.sha256").read_text(encoding="ascii")
    if expected != f"{digest}  {backup.name}\n":
        raise MigrationVerificationError("sqlite_backup_hash_mismatch")
    return digest


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [column[1] for column in connection.execute(f'PRAGMA table_info("{table}")')]


def _projection(columns: list[str]) -> str:
    return ", ".join('"' + field.replace('"', '""') + '"' for field in columns)


def verify_snapshot(current: sqlite3.Connection, backup: Path, *, migrated: bool) -> None:
    missing = object()
    if not migrated:
        verify_backup_digest(backup)
    with closing(_read_only(backup)) as original:
        original_revision = _revision(original)
        expected_revision = HEAD_REVISION if migrated else original_revision
        removing_checks = migrated and original_revision in {"0001", "0002", "0003"}
        if original_revision not in LEGACY_REVISIONS or _revision(current) != expected_revision:
            raise MigrationVerificationError("sqlite_revision_mismatch")
        for table, order in TABLE_KEYS.items():
            columns = _columns(original, table)
            if not columns and table == "proxy_annotations":
                if migrated and current.execute("SELECT 1 FROM proxy_annotations LIMIT 1").fetchone():
                    raise MigrationVerificationError("unexpected_proxy_annotations")
                continue
            if removing_checks and table == "proxies":
                columns = [name for name in columns if name not in CHECK_COLUMNS]
            if not columns or columns != _columns(current, table):
                raise MigrationVerificationError("sqlite_columns_mismatch")

            select = f'SELECT {_projection(columns)} FROM "{table}"'
            where = " WHERE action IS NOT 'check'" if removing_checks and table == "operations" else ""
            old_rows = original.execute(f"{select}{where} ORDER BY {order}")
            new_rows = current.execute(f"{select} ORDER BY {order}")
            password_index = columns.index("password_encrypted") if table == "proxies" else None
            for old, new in zip_longest(old_rows, new_rows, fillvalue=missing):
                if old != new:
                    raise MigrationVerificationError("sqlite_preserved_values_mismatch")
                if password_index is not None:
                    new_password = unprotect_password(new[password_index])
                    if migrated and new_password != unprotect_password(old[password_index]):
                        raise MigrationVerificationError("sqlite_password_mismatch")
        if migrated:
            _integrity(current)
            if original_revision == "0004":
                schema = ("SELECT type, name, tbl_name, sql FROM sqlite_master "
                          "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
                old = original.execute(schema).fetchall()
                new = {row[:2]: row for row in current.execute(schema)}
                if any(new.get(row[:2]) != row for row in old):
                    raise MigrationVerificationError("sqlite_preserved_schema_mismatch")
