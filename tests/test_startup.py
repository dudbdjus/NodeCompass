from unittest.mock import Mock

from proxy_manager.security.credentials import CredentialStore
from proxy_manager.services.sync import SyncService
from proxy_manager.storage.repositories import Repository
from proxy_manager.ui.main_window import MainWindow


def test_window_opens_and_closes(qtbot, database):
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    window = MainWindow(Repository(database), credentials, SyncService(database))
    qtbot.addWidget(window)

    window.show()
    assert window.isVisible()
    assert window.windowTitle() == "节点罗盘"

    window.close()
    assert not window.isVisible()

def test_application_entry_creates_database_and_releases_lock(qapp, tmp_path, monkeypatch):
    from proxy_manager import app as app_module

    def check_window():
        window = next(
            widget for widget in qapp.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        )
        window.close()
        return 0

    app_adapter = Mock()
    app_adapter.exec.side_effect = check_window
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    message_box = Mock()
    monkeypatch.setattr(app_module, "QApplication", Mock(return_value=app_adapter))
    monkeypatch.setattr(app_module, "data_directory", lambda _explicit=None: tmp_path)
    monkeypatch.setattr(app_module, "CredentialStore", Mock(return_value=credentials))
    monkeypatch.setattr(app_module, "QMessageBox", message_box)

    assert app_module.main() == 0
    assert (tmp_path / "proxies.sqlite3").is_file()
    assert not (tmp_path / "instance.lock").exists()
    assert not message_box.mock_calls
