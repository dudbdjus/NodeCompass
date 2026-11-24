from collections import deque
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtCore import QSettings, QTimer, Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QMessageBox, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from proxy_manager.monitoring.network import Bandwidth
from proxy_manager.monitoring.scheduler import MonitoringWorker, Sample
from proxy_manager.monitoring.system import Version
from proxy_manager.security.ssh_credentials import SecretKind, SshCredentialStore
from proxy_manager.security.vps_credentials import KiwiVMCredentialStore
from proxy_manager.services.vps_jobs import ApiSchedule, SshConnectionJob, refresh_kiwi, ssh_auth
from proxy_manager.storage.kiwi_retention import KiwiRetention
from proxy_manager.storage.maintenance_lock import MaintenanceBusy
from proxy_manager.storage.vps_repository import ChartPoint, VpsRepository
from .vps_views import BandwidthView, ConnectionView, KiwiView, OverviewView, SystemView, plain, renewal_text
from .workers import Worker


_SSH_STATUS = {
    "success": "SSH 连接测试通过。",
    "ssh_host_key_changed": "服务器指纹已变化，连接已拒绝。请通过可信渠道核对。",
    "ssh_authentication_failed": "SSH 认证被拒绝，请检查已保存的用户名和凭证。",
    "ssh_authentication_timeout": "SSH 认证超时。",
    "ssh_command_timeout": "SSH 采集超时，当前样本缺失。",
    "ssh_disconnected": "SSH 已断开，当前样本缺失。",
    "ssh_connection_timeout": "SSH 连接超时。",
    "ssh_retry_exhausted": "SSH 连续失败，自动重连已停止。",
    "ssh_password_required": "请先保存 SSH 密码。",
    "ssh_username_required": "请先保存 SSH 用户名。",
    "ssh_private_key_unavailable": "请检查私钥文件和私钥口令。",
}


class VpsPanel(QWidget):
    billing_changed = Signal()

    def __init__(self, database, *, retention_enabled: bool = False):
        super().__init__()
        self.repo = VpsRepository(database)
        self.profile = self.repo.active()
        directory = Path(database.engine.url.database).parent
        self.renewals = QSettings(str(directory / "vps-renewals.ini"), QSettings.Format.IniFormat)
        self.renewal_date = self._read_renewal()
        self.schedule = ApiSchedule()
        self.api_job = self.ssh_job = self.connection = self.monitor = self.writer = None
        self.api_cancel = None
        self.retention = KiwiRetention(Path(database.engine.url.database))
        self.retention_enabled = retention_enabled
        self.retention_cancel = Event()
        self.retention_job = None
        self.next_retention = monotonic() + 60
        self.confirmation = None
        self.api_paused = False
        self.closing = False
        self.pending = deque(maxlen=2048)
        self.next_maintenance = 0
        self.write_after = 0
        self.write_failures = 0
        self.history_paused = False
        self.ssh_message = "未采集"
        self.last_rate = None
        self.latest_kiwi = {}
        self.overview, self.kiwi, self.bandwidth, self.system, self.settings = (
            OverviewView(), KiwiView(), BandwidthView(), SystemView(), ConnectionView()
        )
        self.settings.load(self.profile)
        self.settings.renewal.setText(self.renewal_date)

        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.refresh_api = QPushButton("刷新 KiwiVM")
        self.start_ssh = QPushButton("开始 SSH 采集")
        self.cancel_tasks = QPushButton("取消后台任务")
        for button in (self.refresh_api, self.start_ssh, self.cancel_tasks):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.api_status = plain("KiwiVM 尚未刷新。")
        self.ssh_status = plain("SSH 未采集。")
        layout.addWidget(self.api_status)
        layout.addWidget(self.ssh_status)
        self.tabs = QTabWidget()
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.settings_scroll.setWidget(self.settings)
        overview_scroll = QScrollArea()
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        overview_scroll.setWidget(self.overview)
        for widget, title in ((overview_scroll, "VPS 总览"), (self.kiwi, "KiwiVM 流量"),
                              (self.bandwidth, "实时带宽"), (self.system, "系统监控")):
            self.tabs.addTab(widget, title)
        layout.addWidget(self.tabs)
        self.refresh_api.clicked.connect(self.manual_api)
        self.start_ssh.clicked.connect(self.start_monitoring)
        self.cancel_tasks.clicked.connect(self.cancel)
        self.settings.save.clicked.connect(self.save_settings)
        self.settings.save_renewal.clicked.connect(self.save_renewal)
        self.settings.renewal.returnPressed.connect(self.save_renewal)
        self.settings.test.clicked.connect(self.test_connection)
        self.settings.forget.clicked.connect(self.forget_host)
        self.settings.remove_secret.clicked.connect(self.delete_secret)
        self.bandwidth.range.currentIndexChanged.connect(self.render_bandwidth)
        self.bandwidth.interface.currentIndexChanged.connect(self.change_interface)
        self.kiwi.resolution.currentIndexChanged.connect(self.render_kiwi_chart)
        self.refresh_data()
        self._show_pin()
        self._buttons()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        if self.profile.auto_ssh and self.profile.ssh_host:
            QTimer.singleShot(0, self, self.start_monitoring)

    def _busy(self):
        return any((self.api_job, self.ssh_job, self.monitor, self.confirmation, self.writer, self.pending))

    def _buttons(self):
        idle_ssh = self.ssh_job is None and self.monitor is None and self.confirmation is None
        has_host = self.profile.endpoint is not None
        self.settings.save.setEnabled(not self.closing and not self._busy())
        self.settings.save_renewal.setEnabled(not self.closing and bool(self.profile.id))
        self.settings.test.setEnabled(not self.closing and idle_ssh and has_host)
        self.settings.forget.setEnabled(not self.closing and idle_ssh and has_host)
        self.settings.remove_secret.setEnabled(not self.closing and not self._busy() and bool(self.profile.id))
        self.start_ssh.setEnabled(not self.closing and has_host and (idle_ssh or self.monitor is not None))
        self.start_ssh.setText("立即采样" if self.monitor is not None else "开始 SSH 采集")
        self.refresh_api.setEnabled(not self.closing and self.api_job is None and bool(self.profile.veid))
        can_cancel = self.api_job is not None or not idle_ssh or self.profile.auto_api and not self.api_paused
        self.cancel_tasks.setEnabled(not self.closing and can_cancel)

    def _status(self, message):
        self.ssh_message = message
        self.ssh_status.setText(message)

    def _show_pin(self):
        try:
            pin = self.repo.trusted(self.profile.endpoint) if self.profile.endpoint else None
            self.settings.pin.setText(f"{pin.algorithm}  {pin.sha256}" if pin else "尚未信任主机")
        except Exception:
            self.settings.pin.setText("无法读取主机信任记录。")

    def refresh_data(self):
        profile = self.profile
        self.latest_kiwi = self.repo.kiwi_latest(profile.id) if profile.id else {}
        self.overview.show_data(profile, self.latest_kiwi)
        self.overview.renewal.setText(renewal_text(self.renewal_date))
        self.kiwi.show_data(self.latest_kiwi, self.repo.kiwi_cadences(profile.id) if profile.id else ())
        self.render_kiwi_chart()
        self.render_system()
        self.update_interfaces(())
        self.render_bandwidth()
        self.billing_changed.emit()

    def render_bandwidth(self, *_):
        profile = self.profile
        hours = self.bandwidth.range.currentData()
        iface = profile.interface
        points = self.network_points(hours)
        if self.last_rate is None and self.monitor is None:
            self.last_rate = next((point for point in reversed(points) if point.rx is not None or point.tx is not None), None)
        totals = self.repo.observed_bytes(profile.id, hours, iface) if profile.id else None
        self.bandwidth.show_data(points, self.last_rate, self.ssh_message, totals)
        self.overview.show_network(points, self.last_rate, 15 if hours <= 48 else 90)

    def network_points(self, hours):
        profile = self.profile
        points = self.repo.network_points(profile.id, hours) if profile.id else []
        if profile.interface:
            return [point if point.interface == profile.interface else ChartPoint(point.timestamp, None, None)
                    for point in points]
        return points

    def render_system(self):
        profile = self.profile
        latest = self.repo.latest_system(profile.id) if profile.id else None
        self.system.show_data(latest)
        os_name = latest[1].get("os_release") if latest else None
        self.overview.os_value.setText(f"操作系统：{os_name or '—'}")
        self.overview.os_value.setToolTip("来自已验证的 VPS SSH 连接；缺失时请开始 SSH 采集。")

    def show_panel_versions(self, panel_version, xray_version):
        self.system.show_panel_versions(panel_version, xray_version)
        self.overview.panel_version.setText(f"3X-UI 版本：{panel_version or '—'}")
        self.overview.xray_version.setText(f"Xray 版本：{xray_version or '—'}")
        for label in (self.overview.panel_version, self.overview.xray_version):
            label.setToolTip("版本来自系统设置中已配置的 3X-UI 面板。")

    def render_kiwi_chart(self, *_):
        points = self.repo.kiwi_points(self.profile.id, self.kiwi.resolution.currentData() or 0) if self.profile.id else []
        self.kiwi.show_points(points)

    def save_settings(self):
        if self.closing or self._busy():
            return
        try:
            date = self.settings.renewal_value()
        except ValueError:
            self.settings.renewal_status.setText("请输入有效日期，例如 2027-01-15。")
            return
        try:
            proposed = self.settings.read(self.profile)
            profile = self.repo.prepare(proposed)
            fields = self.settings.fields
            key, password, phrase = (fields[name].text() for name in ("api_key", "password", "passphrase"))
            if key and not profile.veid:
                raise ValueError("missing_veid")
            if (password or phrase) and not profile.ssh_host:
                raise ValueError("missing_host")
            if key:
                KiwiVMCredentialStore().save(profile.veid, key)
            ssh_store = SshCredentialStore()
            if password:
                ssh_store.save(profile.credential_id, SecretKind.PASSWORD, password)
            if phrase:
                ssh_store.save(profile.credential_id, SecretKind.PASSPHRASE, phrase)
            self.repo.save(profile)
        except Exception:
            self._status("保存失败，请检查 IP、VEID、凭证及本地存储。")
            return
        self.profile = profile
        self.renewal_date = self._read_renewal()
        self._save_renewal_value(date)
        self.schedule = ApiSchedule()
        self.api_paused = False
        self.last_rate = None
        self.settings.load(profile)
        self._status("连接设置已保存。")
        self.api_status.setText("KiwiVM 尚未刷新。")
        self._show_pin()
        self.refresh_data()
        self._buttons()

    def _read_renewal(self):
        return self.renewals.value(self.profile.id, "", type=str) if self.profile.id else ""

    def save_renewal(self):
        if self.closing or not self.profile.id:
            return
        try:
            date = self.settings.renewal_value()
        except ValueError:
            self.settings.renewal_status.setText("请输入有效日期，例如 2027-01-15。")
            return
        self._save_renewal_value(date)
        self.overview.renewal.setText(renewal_text(self.renewal_date))
        self.billing_changed.emit()

    def _save_renewal_value(self, value):
        if value != self.renewal_date:
            if value:
                self.renewals.setValue(self.profile.id, value)
            else:
                self.renewals.remove(self.profile.id)
            self.renewals.sync()
            if self.renewals.status() != QSettings.Status.NoError:
                self.settings.renewal_status.setText("无法保存续费时间，请检查本地数据目录。")
                return
        self.renewal_date = value
        self.settings.renewal.setText(value)
        self.settings.renewal_status.setText("续费时间已保存。" if value else "")

    def _confirm_destructive(self, title, message):
        return QMessageBox.question(self, title, message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def delete_secret(self):
        if self.closing or self._busy() or not self.profile.id:
            return
        if not self._confirm_destructive("移除本地凭证", "移除本配置保存的 KiwiVM API Key、SSH 密码和私钥口令？"):
            return
        try:
            if self.profile.veid:
                KiwiVMCredentialStore().delete(self.profile.veid)
            for kind in SecretKind:
                SshCredentialStore().delete(self.profile.credential_id, kind)
            self.api_paused = True
            self._status("已移除本地凭证。")
        except Exception:
            self._status("移除凭证失败，请检查 Windows 凭证管理器。")

    def forget_host(self):
        endpoint = self.profile.endpoint
        if self.closing or not endpoint or any((self.ssh_job, self.monitor, self.confirmation)):
            return
        if not self._confirm_destructive("移除 SSH 信任", f"移除 {endpoint.host}:{endpoint.port} 的已确认指纹？"
                                             " 下次测试连接时需通过可信渠道重新核对。"):
            return
        try:
            self.repo.forget(endpoint)
            self._show_pin()
            self._status("已移除主机信任。")
        except Exception:
            self._status("无法移除主机信任。")

    def test_connection(self):
        if self.closing or not self.profile.endpoint or any((self.ssh_job, self.monitor, self.confirmation)):
            return
        try:
            pin = self.repo.trusted(self.profile.endpoint)
            self._launch_connection(pin)
        except Exception:
            self._status("无法读取 SSH 主机信任记录。")

    def _launch_connection(self, pin, *, persist=False):
        self.connection = SshConnectionJob(self.repo, self.profile, pin, persist=persist)
        self.ssh_job = Worker(self.connection.run)
        self.ssh_job.start()
        self._status("正在测试 SSH 连接…" if pin else "正在获取 SSH 主机公钥…")
        self._buttons()

    def _ask_fingerprint(self, pin):
        box = QMessageBox(self)
        box.setWindowTitle("核对 SSH 主机指纹")
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"主机 {pin.endpoint.host}   端口 {pin.endpoint.port}\n"
                    f"密钥算法 {pin.algorithm}\n\n{pin.sha256}")
        box.setInformativeText("请通过 VPS 控制台等可信渠道核对完整指纹，确认一致后才继续认证。")
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        yes = box.addButton("指纹一致，确认并连接", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        self.confirmation = box

        def done(_):
            self.confirmation = None
            if not self.closing and box.clickedButton() is yes:
                self._launch_connection(pin, persist=True)
            else:
                self._status("已取消指纹确认。")
            box.deleteLater()
            self._buttons()

        box.finished.connect(done)
        box.open()
        self._buttons()

    def start_monitoring(self):
        if self.closing or not self.profile.endpoint or any((self.ssh_job, self.confirmation)):
            return
        if self.monitor:
            self.monitor.refresh_now()
            return
        try:
            profile = self.profile
            pin = self.repo.trusted(profile.endpoint)
            if pin is None:
                self._status("请先测试 SSH 连接并确认主机指纹。")
                return
            self.monitor = MonitoringWorker(profile.endpoint, pin, ssh_auth(profile), interface=profile.interface,
                                            x_ui_version=Version(profile.x_ui_version) if profile.x_ui_version else None,
                                            xray_version=Version(profile.xray_version) if profile.xray_version else None)
            self.history_paused = False
            self.write_failures = 0
            self.write_after = 0
            self.monitor.start()
        except Exception as error:
            self.monitor = None
            self._status(_SSH_STATUS.get(str(error), "SSH 配置或凭证不可用。"))
            return
        self._status("正在连接 SSH…")
        self._buttons()

    def update_interfaces(self, interfaces):
        choice = self.bandwidth.interface
        prior = {choice.itemData(index) for index in range(1, choice.count())}
        names = set(interfaces) if interfaces else prior
        if self.profile.interface:
            names.add(self.profile.interface)
        choice.blockSignals(True)
        choice.clear()
        choice.addItem("自动选择出口", None)
        for name in sorted(names):
            choice.addItem(name, name)
        choice.setCurrentIndex(max(0, choice.findData(self.profile.interface)))
        choice.blockSignals(False)

    def change_interface(self, *_):
        if self.closing or not self.profile.id:
            return
        interface = self.bandwidth.interface.currentData()
        profile = replace(self.profile, interface=interface)
        try:
            self.repo.save(profile)
        except Exception:
            self._status("无法保存网卡选择。")
            self.update_interfaces(())
            return
        self.profile = profile
        self.last_rate = None
        self.settings.fields["interface"].setText(interface or "")
        if self.monitor:
            self.monitor.set_interface(interface)
        self.render_bandwidth()

    def manual_api(self):
        self.api_paused = False
        self.start_api(forced=True)

    def start_api(self, *, forced=False):
        if self.closing or self.api_paused or self.api_job is not None or not self.profile.veid:
            return
        due = self.schedule.due(self.profile, forced=forced)
        if not due:
            return
        self.schedule.started(due)
        profile, cancel = self.profile, Event()
        self.api_cancel = cancel
        self.api_job = Worker(lambda: refresh_kiwi(self.repo, profile, due, cancel))
        self.api_status.setText("正在刷新 KiwiVM…")
        self.api_job.start()
        self._buttons()

    def cancel(self):
        self.api_paused = True
        if self.api_cancel:
            self.api_cancel.set()
        if self.confirmation:
            self.confirmation.reject()
        if self.connection:
            self.connection.cancel()
        if self.monitor:
            self.monitor.request_stop()
        self._buttons()

    def _poll_api(self):
        if self.api_job is None or self.api_job.is_alive():
            return
        job, self.api_job = self.api_job, None
        cancelled, self.api_cancel = self.api_cancel.is_set(), None
        if self.closing:
            return
        if cancelled:
            self.api_status.setText("KiwiVM 刷新已取消。")
        elif job.failed:
            self.api_status.setText("KiwiVM 刷新失败，保留上次成功数据。")
        elif "credential" in job.result:
            self.api_status.setText("KiwiVM 凭证缺失或不可用，请检查连接设置。")
        elif any(value != "success" for value in job.result.values()):
            self.api_status.setText("部分 KiwiVM 数据不可用，保留上次成功数据。")
        else:
            self.api_status.setText("KiwiVM 刷新完成。")
        self.latest_kiwi = self.repo.kiwi_latest(self.profile.id)
        self.overview.show_data(self.profile, self.latest_kiwi)
        self.kiwi.show_data(self.latest_kiwi, self.repo.kiwi_cadences(self.profile.id))
        self.render_kiwi_chart()
        self.billing_changed.emit()

    def _poll_ssh(self):
        if self.ssh_job is not None and not self.ssh_job.is_alive():
            job, self.ssh_job = self.ssh_job, None
            cancelled, self.connection = self.connection.cancelled.is_set(), None
            if not self.closing:
                self._show_pin()
                outcome, pin = job.result if not job.failed else ("failed", None)
                if cancelled:
                    self._status("已取消 SSH 测试。")
                elif outcome == "confirm":
                    self._ask_fingerprint(pin)
                else:
                    self._status(_SSH_STATUS.get(outcome, "SSH 连接测试失败。"))
        if self.monitor is None:
            return
        samples = self.monitor.drain()
        if samples:
            self.pending.extend(samples)
            last = samples[-1]
            if last.error:
                self._status(_SSH_STATUS.get(last.error, "SSH 采集失败。"))
            elif last.bandwidth.state == "ok":
                if not self.profile.interface or last.bandwidth.interface == self.profile.interface:
                    self.last_rate = ChartPoint(last.captured, last.bandwidth.rx_mbps, last.bandwidth.tx_mbps,
                                                last.bandwidth.interface)
                self._status("采集中")
            elif last.bandwidth.state == "baseline":
                self._status("已建立采样基线，等待下一次采样。")
            else:
                self._status("采集缺失，保留最近有效数据。")
            if last.interfaces:
                self.update_interfaces(last.interfaces)
        if not self.monitor.running:
            stopped = self.monitor.drain()
            self.pending.extend(stopped)
            if not ((stopped or samples) and (stopped or samples)[-1].error):
                self.pending.append(Sample(monotonic(), Bandwidth("missing"), None))
                self._status("SSH 采集已停止。")
            self.monitor = None

    def _write_history(self):
        if self.writer is not None and not self.writer.is_alive():
            writer, self.writer = self.writer, None
            if writer.failed:
                self.write_failures += 1
                if self.write_failures >= 3:
                    self.history_paused = True
                    if self.monitor:
                        self.monitor.request_stop()
                    self._status("本地监控历史写入连续失败，采集已暂停。")
                else:
                    self._status("本地监控历史写入失败。")
                if not self.closing and not self.history_paused:
                    self.pending.extendleft(reversed(writer.batch))
                    self.write_after = monotonic() + 5
            else:
                self.write_failures = 0
                if not self.closing and writer.batch:
                    self.render_system()
                    self.render_bandwidth()
        if self.history_paused:
            self.pending.clear()
            return
        if self.writer is not None or (monotonic() < self.write_after and not self.closing):
            return
        maintenance = self.retention_enabled and not self.closing and monotonic() >= self.next_maintenance
        if not self.pending and not maintenance:
            return
        profile_id, batch = self.profile.id, list(self.pending)
        self.pending.clear()
        if maintenance:
            self.next_maintenance = monotonic() + 3600

        def persist():
            if batch:
                self.repo.save_samples(profile_id, batch)
            if maintenance:
                try:
                    self.repo.prune()
                except MaintenanceBusy:
                    pass

        self.writer = Worker(persist)
        self.writer.batch = batch
        self.writer.start()

    def tick(self):
        try:
            self._poll_api()
            self._poll_ssh()
            if self.profile.id:
                self._write_history()
            if self.profile.auto_api:
                self.start_api()
        except Exception:
            self._status("无法读取本地监控数据。")
        self._poll_retention()
        self._buttons()

    def _poll_retention(self):
        if self.retention_job is not None:
            if self.retention_job.is_alive():
                return
            job, self.retention_job = self.retention_job, None
            if not job.failed and job.result == "waiting":
                self.next_retention = monotonic() + 3600
        if not self.retention_enabled or self.closing or monotonic() < self.next_retention:
            return
        self.next_retention = monotonic() + 86405
        self.retention_job = Worker(lambda: self.retention.run_daily(self.retention_cancel))
        self.retention_job.start()

    def shutdown(self):
        if not self.closing:
            self.closing = True
            self.retention_cancel.set()
            self.cancel()
            self.setEnabled(False)
        self.tick()
        if self._busy() or self.retention_job is not None:
            return False
        self.timer.stop()
        return True
