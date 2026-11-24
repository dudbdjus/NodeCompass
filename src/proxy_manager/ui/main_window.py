from collections.abc import Callable
from pathlib import Path
from threading import Event

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMenu, QMessageBox, QSystemTrayIcon, QTabWidget

from proxy_manager.branding import APP_NAME
from proxy_manager.provider.errors import ProviderError
from proxy_manager.security.credentials import CredentialError, CredentialStore
from proxy_manager.services.export import ExportService
from proxy_manager.services.geoip import GeoIP
from proxy_manager.services.proxy_query import ProxyQuery
from proxy_manager.services.sync import SyncResult, SyncService
from proxy_manager.storage.repositories import Repository
from proxy_manager.ui.dashboard import Dashboard
from proxy_manager.ui.pages.history import HistoryPage
from proxy_manager.ui.pages.geo_settings import GeoSettings
from proxy_manager.ui.pages.general_settings import GeneralSettings
from proxy_manager.ui.pages.overview import OverviewPage
from proxy_manager.ui.pages.settings import SettingsPage
from proxy_manager.ui.pages.proxies import ProxiesPage
from proxy_manager.ui.panel_monitor import PanelMonitor
from proxy_manager.ui.vps_panel import VpsPanel
from proxy_manager.ui.workers import Worker
from proxy_manager.ui.workspace import Workspace


SYNC_ERRORS = {
    "timeout": "请求超时",
    "transport_error": "无法连接 Proxy-Seller",
    "business_error": "Proxy-Seller 返回业务错误，请检查 API Key 或账户权限",
    "invalid_response": "Proxy-Seller 响应不符合已知格式",
    "inconsistent_pagination": "分页数据不一致",
    "duplicate_proxy_id": "供应商返回重复的代理 ID",
    "rate_limited": "请求受限，请稍后重试",
    "password_encryption_failed": "代理密码加密失败",
    "database_write_failed": "本地数据库写入失败",
    "sync_in_progress": "同步已在运行",
    "invalid_key": "API Key 格式无效",
}


def sync_error_label(reason: str | None) -> str:
    if reason and reason.startswith("http_error_"):
        return f"Proxy-Seller 返回 HTTP {reason.removeprefix('http_error_')}"
    return SYNC_ERRORS.get(reason, "本地操作失败")


class MainWindow(QMainWindow):
    exit_ready = Signal()

    def __init__(self, repository: Repository, credentials: CredentialStore, sync: SyncService,
                 *, retention_enabled: bool = False, startup=None):
        super().__init__()
        self.repository = repository
        self.credentials = credentials
        self.sync = sync
        self.export_service = ExportService(repository)
        self.credential = None
        self.key_read_failed = False
        try:
            self.credential = credentials.load()
        except CredentialError:
            self.key_read_failed = True
        self.worker: Worker | None = None
        self.job_timer = QTimer(self)
        self.job_timer.setInterval(50)
        self.job_timer.timeout.connect(self.job_finished)
        self.cancel: Event | None = None
        self.on_done: Callable[[object], None] | None = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "assets" / "app.png")))
        self.tray = None
        self.exit_requested = False
        self.resize(1280, 820)
        self.setMinimumSize(1000, 630)
        directory = Path(repository.database.engine.url.database).parent
        self.geo = GeoIP(directory)
        self.proxies = ProxiesPage(ProxyQuery(repository), geo=self.geo)
        self.overview = OverviewPage()
        self.proxies.layout().addWidget(self.overview)
        self.history = HistoryPage()
        self.isp_settings = SettingsPage(directory)
        self.vps = VpsPanel(repository.database, retention_enabled=retention_enabled)
        self.panel = PanelMonitor(geo=self.geo, ssh_context=self.geo_ssh_context)
        self.geo_settings = GeoSettings(self.geo)
        self.general_settings = GeneralSettings(directory, startup)
        self.settings = QTabWidget()
        self.settings.addTab(self.vps.settings_scroll, "VPS 连接")
        self.settings.addTab(self.isp_settings, "Proxyseller 连接")
        self.settings.addTab(self.panel.settings, "3X-UI 面板连接")
        self.settings.addTab(self.geo_settings, "IP 归属地")
        self.settings.addTab(self.general_settings, "常规")
        self.geo_settings.changed.connect(self.refresh_geo)
        self.dashboard = Dashboard()
        self.workspace = Workspace((("总览", self.dashboard), ("Proxyseller", self.proxies), ("3X-UI", self.panel),
                                    ("搬瓦工VPS", self.vps),
                                    ("历史记录", self.history), ("系统设置", self.settings)))
        self.tabs = self.workspace.stack
        self.tabs.currentChanged.connect(
            lambda index: self.panel.inventory.set_linked_active(self.tabs.widget(index) is self.proxies))
        self.panel.inventory.proxy_links_changed.connect(self.proxies.update_panel_links)
        self.vps.billing_changed.connect(self.refresh_billing)
        self.panel.changed.connect(self.refresh_panel)
        self.dashboard.system_details.clicked.connect(lambda: self.tabs.setCurrentWidget(self.panel))
        self.isp_settings.save_requested.connect(self.save_key)
        self.isp_settings.delete_requested.connect(self.delete_key)
        self.setCentralWidget(self.workspace)
        self.proxies.sync_requested.connect(self.start_sync)
        self.proxies.cancel_requested.connect(self.cancel_sync)
        self.proxies.export_requested.connect(self.start_export)
        self.proxies.annotation_status.connect(self.annotation_finished)
        self.reload()
        self.refresh_billing()
        self.refresh_panel()
        self.update_buttons()

    def refresh_billing(self) -> None:
        self.dashboard.show_billing(self.vps)

    def refresh_geo(self) -> None:
        self.proxies.refresh_geo()
        self.panel.inventory.repaint_geo()

    def geo_ssh_context(self):
        profile = self.vps.repo.active()
        return profile, self.vps.repo.trusted(profile.endpoint) if profile.endpoint else None

    def refresh_panel(self) -> None:
        self.dashboard.show_panel(self.panel)
        status = self.panel.status
        self.vps.show_panel_versions(status.panel_version if status else None,
                                     status.xray_version if status else None)

    @property
    def profile_id(self) -> str | None:
        return self.credential.profile_id if self.credential else None

    def reload(self) -> None:
        self.proxies.reload(self.profile_id)
        self.refresh_history()

    def refresh_history(self) -> None:
        profile_id = self.profile_id
        runs = self.repository.sync_history(profile_id) if profile_id else []
        latest_success = self.repository.latest_success(profile_id) if profile_id else None
        self.overview.refresh(self.credential is not None, self.proxies.all_rows, runs, latest_success)
        self.history.refresh(runs, self.repository.operations(), sync_error_label)

    def save_key(self) -> None:
        if self.worker is not None:
            return
        key = self.isp_settings.key_input.text().strip()
        if self.credential is not None and key and self.credential.key != key:
            if not self.confirm_key_action("替换 API Key", "替换后，应用将为新 Key 创建独立的本地数据视图。继续？"):
                return
        try:
            self.credential = self.credentials.save(key)
        except ProviderError:
            self.record_key_action("key_save", "failed", "请输入有效的 API Key。")
            return
        except CredentialError:
            self.record_key_action("key_save", "failed", "保存失败，请检查 Windows 凭证管理器。")
            return
        self.key_read_failed = False
        self.isp_settings.key_input.clear()
        self.record_key_action("key_save", "success", "API Key 已保存。请在 Proxyseller 页开始同步。")

    def delete_key(self) -> None:
        if self.worker is not None or not self.confirm_key_action(
            "移除 API Key", "将移除 Windows 凭证中保存的 API Key。继续？",
        ):
            return
        try:
            self.credentials.delete()
        except CredentialError:
            self.record_key_action("key_delete", "failed", "移除失败，请检查 Windows 凭证管理器。")
            return
        self.credential = None
        self.key_read_failed = False
        self.isp_settings.key_input.clear()
        self.record_key_action("key_delete", "success", "已移除本地 API Key。")

    def confirm_key_action(self, title: str, message: str) -> bool:
        choice = QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return choice == QMessageBox.StandardButton.Yes

    def record_key_action(self, action: str, outcome: str, message: str) -> None:
        self.update_buttons()
        try:
            self.repository.log_operation(action, outcome)
            self.reload()
        except Exception:
            message += " 本地记录更新失败。"
        self.statusBar().showMessage(message)

    def update_buttons(self) -> None:
        busy = self.worker is not None
        has_key = self.credential is not None
        self.proxies.set_busy(busy, has_key, self.cancel is not None)
        self.isp_settings.update_state(has_key, busy, self.key_read_failed)

    def start_sync(self) -> None:
        credential = self.credential
        if credential is None or self.worker is not None:
            return
        cancel = Event()
        self.cancel = cancel
        self.start_job(lambda: self.sync.run(credential, cancel), self.sync_finished, "正在同步 Proxyseller…")

    def cancel_sync(self) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.proxies.cancel_button.setEnabled(False)
            self.statusBar().showMessage("正在取消同步…")

    def sync_finished(self, result: SyncResult) -> None:
        self.reload()
        if result.outcome == "success":
            message = f"同步完成，共 {result.count} 条"
        elif result.outcome == "cancelled":
            message = "同步已取消，已保留上次数据。"
        else:
            message = f"同步失败，已保留上次数据。{sync_error_label(result.reason)}"
        self.statusBar().showMessage(message)

    def start_export(self) -> None:
        rows = list(self.proxies.model.rows)
        if not rows or self.worker is not None:
            return
        dialog = QFileDialog(self, "导出当前筛选结果")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilters(["CSV (*.csv)", "TXT (*.txt)"])
        dialog.setDefaultSuffix("csv")
        dialog.filterSelected.connect(
            lambda selected: dialog.setDefaultSuffix("txt" if selected.startswith("TXT") else "csv")
        )
        dialog.selectFile("proxies")
        if not dialog.exec():
            return
        path = Path(dialog.selectedFiles()[0])
        if path.suffix.lower() not in {".txt", ".csv"}:
            self.statusBar().showMessage("请选择 CSV 或 TXT 文件。")
            return
        self.start_job(lambda: self.export_service.run(path, rows), self.export_finished, "正在导出…")

    def annotation_finished(self, message: str) -> None:
        self.refresh_history()
        self.statusBar().showMessage(message)

    def export_finished(self, count: int) -> None:
        self.refresh_history()
        self.statusBar().showMessage(f"已导出 {count} 条代理记录。")

    def start_job(self, job, on_done, message: str) -> None:
        if self.worker is not None:
            return
        self.on_done = on_done
        self.worker = Worker(job)
        self.job_timer.start()
        self.statusBar().showMessage(message)
        self.update_buttons()
        self.worker.start()

    def job_finished(self) -> None:
        worker = self.worker
        if worker is None or worker.is_alive():
            return
        self.job_timer.stop()
        on_done = self.on_done
        self.worker = None
        self.on_done = None
        self.cancel = None
        self.update_buttons()
        try:
            if not worker.failed:
                on_done(worker.result)
                return
        except Exception:
            pass
        try:
            self.refresh_history()
        except Exception:
            pass
        self.statusBar().showMessage("操作失败，请检查本地数据目录和凭证。")

    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def show_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable() or self.windowIcon().isNull():
            return False
        if self.tray is None:
            tray = self.tray = QSystemTrayIcon(self.windowIcon(), self)
            tray.setToolTip(APP_NAME)
            menu = self.tray_menu = QMenu(self)
            menu.addAction(f"打开{APP_NAME}", self.restore_window)
            menu.addAction("退出软件", self.request_exit)
            tray.setContextMenu(menu)
            tray.activated.connect(self.tray_activated)
        self.tray.show()
        return True

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_window()

    def request_exit(self):
        self.exit_requested = True
        self.close()

    def closeEvent(self, event) -> None:
        if (not self.exit_requested and self.general_settings.close_behavior == "tray" and self.show_tray()):
            event.ignore()
            self.hide()
            return
        if self.worker is not None:
            if self.exit_requested:
                self.restore_window()
                self.exit_requested = False
            self.statusBar().showMessage("请等待当前操作完成。同步操作可以取消。")
            event.ignore()
            return
        # An asynchronous shutdown retry is still an exit, never a tray action.
        self.exit_requested = True
        geo_stopped = self.geo_settings.shutdown()
        if not geo_stopped:
            self.statusBar().showMessage("正在等待归属地数据库导入完成…")
            event.ignore()
            QTimer.singleShot(150, self, self.close)
            return
        panel_stopped = self.panel.shutdown()
        vps_stopped = self.vps.shutdown()
        if not (panel_stopped and vps_stopped):
            self.statusBar().showMessage("正在停止监控后台任务…")
            event.ignore()
            QTimer.singleShot(150, self, self.close)
            return
        self.geo.close()
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(event)
        self.exit_ready.emit()
