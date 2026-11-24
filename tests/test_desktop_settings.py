from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from proxy_manager.desktop_startup import ENTRY, RUN_KEY, StartupRegistration
from proxy_manager.paths import data_directory
from proxy_manager.security.credentials import CredentialStore
from proxy_manager.services.sync import SyncService
from proxy_manager.storage.repositories import Repository
from proxy_manager.ui.main_window import MainWindow
from proxy_manager.ui.pages.general_settings import GeneralSettings


class Registry:
    HKEY_CURRENT_USER = "current user"
    REG_SZ = 1
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {}

    def OpenKey(self, hive, path, reserved, access):
        assert (hive, path, reserved) == (self.HKEY_CURRENT_USER, RUN_KEY, 0)
        return nullcontext(access)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        assert key == self.KEY_QUERY_VALUE and name == ENTRY
        if name not in self.values:
            raise FileNotFoundError()
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        assert key == self.KEY_SET_VALUE and name == ENTRY and reserved == 0
        self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        assert key == self.KEY_SET_VALUE and name == ENTRY
        self.values.pop(name)


def test_autostart_only_registers_on_click_and_keeps_isolated_directory(qtbot, tmp_path, monkeypatch):
    directory = tmp_path / "isolated data"
    directory.mkdir()
    exe = tmp_path / "portable preview.exe"
    exe.touch()
    registry = Registry()
    startup = StartupRegistration(exe, directory, registry=registry)
    page = GeneralSettings(directory, startup)
    qtbot.addWidget(page)
    assert page.close_behavior == "exit"
    assert not page.auto_start.isChecked() and not registry.values
    page.auto_start.click()
    assert registry.values[ENTRY] == (f'"{exe}" --data-dir "{directory}"', registry.REG_SZ)
    page.auto_start.click()
    assert not registry.values and not page.auto_start.isChecked()
    monkeypatch.setenv("PROXY_MANAGER_DATA_DIR", str(tmp_path / "another"))
    assert data_directory(str(directory)) == directory


def test_close_choice_persists_and_other_registration_is_not_removed(qtbot, tmp_path):
    exe = tmp_path / "ProxyManager.exe"
    exe.touch()
    registry = Registry()
    startup = StartupRegistration(exe, tmp_path, registry=registry)
    registry.values[ENTRY] = ("another build", registry.REG_SZ)
    startup.set_enabled(False)
    assert registry.values[ENTRY][0] == "another build"
    page = GeneralSettings(tmp_path, startup)
    qtbot.addWidget(page)
    page.close_choice.setCurrentIndex(page.close_choice.findData("tray"))
    reopened = GeneralSettings(tmp_path, startup)
    qtbot.addWidget(reopened)
    assert reopened.close_behavior == "tray"


def test_close_to_tray_restores_and_explicit_exit_has_priority(qtbot, database, monkeypatch):
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    window = MainWindow(Repository(database), credentials, Mock(spec=SyncService))
    qtbot.addWidget(window)
    assert not window.windowIcon().isNull()
    window.general_settings.close_choice.setCurrentIndex(0)
    hide_tray = Mock()
    window.tray = SimpleNamespace(hide=hide_tray)
    monkeypatch.setattr(window, "show_tray", lambda: True)
    window.show()
    window.close()
    assert not window.isVisible() and not window.exit_requested
    assert not window.panel.closing and not window.vps.closing
    window.restore_window()
    assert window.isVisible()
    window.close()
    with qtbot.waitSignal(window.exit_ready):
        window.request_exit()
    hide_tray.assert_called_once()
    assert window.panel.closing and window.vps.closing
