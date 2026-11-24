import argparse
import sys

from PySide6.QtCore import QCoreApplication, QEvent, QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from proxy_manager.branding import APP_NAME
from proxy_manager.logging_config import record_boot_failure
from proxy_manager.desktop_startup import StartupRegistration

from proxy_manager.paths import data_directory
from proxy_manager.security.credentials import CredentialStore
from proxy_manager.services.sync import SyncService
from proxy_manager.storage.database import Database
from proxy_manager.storage.history_startup import confirm_history_startup, prepare_history_startup
from proxy_manager.storage.repositories import Repository
from proxy_manager.storage.upgrade_backup import MigrationVerificationError
from proxy_manager.ui.main_window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--enable-vps-retention", action="store_true")
    options, qt_args = parser.parse_known_args()
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    database = None
    directory = None
    lock = None
    stage = "data_directory"
    try:
        directory = data_directory(options.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stage = "instance_lock"
        lock = QLockFile(str(directory / "instance.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(None, APP_NAME, "软件已打开，或本地目录无法加锁。")
            return 1
        stage = "database"
        path = directory / "proxies.sqlite3"
        database = Database(path, allow_legacy=False)
        stage = "history_preflight"
        allow_retention = prepare_history_startup(path, requested=options.enable_vps_retention)
        stage = "main_window"
        startup = StartupRegistration(sys.executable, directory) if getattr(sys, "frozen", False) else None
        window = MainWindow(Repository(database), CredentialStore(), SyncService(database),
                            retention_enabled=allow_retention, startup=startup)
        app.setWindowIcon(window.windowIcon())
        window.exit_ready.connect(app.quit)
        window.show()
        confirm_history_startup(path)
        result = app.exec()
        # Dispose the window and its native children while QApplication and the database still exist.
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return result
    except Exception as error:
        record_boot_failure(directory, stage, error)
        if isinstance(error, MigrationVerificationError) and str(error) == "explicit_legacy_migration_required":
            message = "检测到旧版数据库。请使用通过独立授权的迁移流程。"
        else:
            message = "启动失败，请检查本地数据目录和 Windows 凭证。"
        QMessageBox.critical(None, APP_NAME, message)
        return 1
    finally:
        if database is not None:
            database.close()
        if lock is not None and lock.isLocked():
            lock.unlock()
