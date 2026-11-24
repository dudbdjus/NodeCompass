from time import monotonic, time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from proxy_manager.provider.panel_client import PanelCredentials, PanelError, fetch_status
from proxy_manager.security.panel_credentials import PanelCredentialStore
from proxy_manager.storage.vps_repository import ChartPoint
from .dashboard import network_rate
from .panel_inventory import PanelInventory
from .vps_views import plain
from .workers import Worker


ERROR_MESSAGES = {
    "address": "请输入 HTTPS 面板地址，包含面板在浏览器中使用的路径前缀。",
    "token": "请输入面板 API Token。",
    "save": "无法保存面板连接，请检查 Windows 凭证管理器。",
    "load": "无法读取面板凭证，请重新保存。",
    "delete": "无法移除面板凭证。",
    "auth": "面板拒绝认证，请核对 API Token。",
    "path": "未找到面板状态接口，请核对地址和路径前缀。",
    "redirect": "面板返回重定向，请直接填写最终 HTTPS 面板地址。",
    "rate": "面板请求受限，将于 60 秒后重试。",
    "timeout": "面板读取超时。",
    "network": "无法通过 HTTPS 连接面板，请检查地址、网络和证书。",
    "response": "面板状态响应无法读取。",
    "http": "面板返回 HTTP 错误。",
}
PAUSE_ERRORS = {"auth", "path", "redirect", "response"}


class PanelMonitor(QWidget):
    changed = Signal()

    def __init__(self, store=None, *, geo=None, ssh_context=None):
        super().__init__()
        self.store = store if store is not None else PanelCredentialStore()
        self.credentials = None
        self.worker = None
        self.closing = False
        self.next_poll = None
        self.status = None
        self.stamp = None
        self.points = []

        self.settings = QWidget()
        form = QFormLayout()
        self.address = QLineEdit(placeholderText="https://面板地址:端口/面板路径/")
        self.token = QLineEdit(placeholderText="留空保留同一地址已保存的 Token")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("面板地址", self.address)
        form.addRow("API Token", self.token)
        self.save_button = QPushButton("保存并读取状态")
        self.remove_button = QPushButton("移除连接")
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        self.settings_status = plain()
        settings_layout = QVBoxLayout(self.settings)
        settings_layout.addLayout(form)
        settings_layout.addLayout(actions)
        settings_layout.addWidget(self.settings_status)
        settings_layout.addStretch()

        self.inventory = PanelInventory(geo=geo, ssh_context=ssh_context)
        QVBoxLayout(self).addWidget(self.inventory)

        message = "请在系统设置中配置 3X-UI 面板。"
        try:
            self.credentials = self.store.load()
        except PanelError:
            message = ERROR_MESSAGES["load"]
        if self.credentials:
            self.address.setText(self.credentials.base_url)
            self.next_poll = monotonic()
            message = "已保存面板连接，等待读取。"
        self.message = message
        self.inventory.set_credentials(self.credentials)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save_button.clicked.connect(self.save)
        self.remove_button.clicked.connect(self.remove)
        self.publish(message)

    def save(self):
        if self.worker is not None:
            return
        address = self.address.text().strip().rstrip("/")
        token = self.token.text().strip()
        if not token and self.credentials and address == self.credentials.base_url.rstrip("/"):
            token = self.credentials.token
        try:
            credentials = PanelCredentials(address, token)
            self.store.save(credentials)
        except PanelError as error:
            self.settings_status.setText(ERROR_MESSAGES[str(error)])
            return
        if self.credentials != credentials:
            self.reset_data()
        self.credentials = credentials
        self.inventory.set_credentials(credentials)
        self.token.clear()
        self.next_poll = None
        self.start()

    def remove(self):
        if self.worker is not None:
            return
        try:
            self.store.delete()
        except PanelError as error:
            self.settings_status.setText(ERROR_MESSAGES[str(error)])
            return
        self.credentials = self.next_poll = None
        self.inventory.set_credentials(None)
        self.address.clear()
        self.token.clear()
        self.reset_data()
        self.publish("请在系统设置中配置 3X-UI 面板。")

    def reset_data(self):
        self.status = self.stamp = None
        self.points = []

    def start(self):
        if self.closing or self.worker is not None or self.credentials is None:
            return
        credentials = self.credentials

        def read():
            try:
                return fetch_status(credentials)
            except PanelError as error:
                return str(error)

        self.worker = Worker(read)
        self.worker.start()
        self.publish("正在读取 3X-UI 面板…")

    def tick(self):
        if self.worker is None:
            if self.next_poll is not None and monotonic() >= self.next_poll:
                self.start()
            return
        if self.worker.is_alive():
            return
        worker, self.worker = self.worker, None
        now = time()
        result = "response" if worker.failed else worker.result
        if isinstance(result, str):
            message = ERROR_MESSAGES.get(result, ERROR_MESSAGES["response"])
            if self.status is not None:
                message += " 已保留上次成功数据。"
            self.next_poll = None if result in PAUSE_ERRORS else monotonic() + (60 if result == "rate" else 5)
            point = ChartPoint(now, None, None)
        else:
            self.status, self.stamp = result, now
            self.next_poll = monotonic() + 5
            message = "3X-UI 面板已更新。"
            point = ChartPoint(now, network_rate(result.download), network_rate(result.upload))
        self.points = [sample for sample in self.points[-180:] if sample.timestamp >= now - 900] + [point]
        self.publish(message)

    def publish(self, message):
        self.message = message
        self.settings_status.setText(message)
        idle = self.worker is None
        self.save_button.setEnabled(idle)
        self.remove_button.setEnabled(idle and self.credentials is not None)
        self.address.setEnabled(idle)
        self.token.setEnabled(idle)
        self.changed.emit()

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        inventory_stopped = self.inventory.shutdown()
        return inventory_stopped and (self.worker is None or not self.worker.is_alive())
