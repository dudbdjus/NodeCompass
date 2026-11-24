from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from proxy_manager.branding import APP_ENGLISH_NAME
from proxy_manager.ui.workers import Worker


class GeoSettings(QWidget):
    changed = Signal()

    def __init__(self, geo):
        super().__init__()
        self.geo = geo
        self.worker = None
        self.info = QLabel(textFormat=Qt.TextFormat.PlainText)
        self.message = QLabel(textFormat=Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        attribution = QLabel(
            f'{APP_ENGLISH_NAME} uses the IP2Location LITE database for '
            '<a href="https://www.ip2location.com">IP geolocation</a>. '
            '数据库可能不是最新。')
        attribution.setOpenExternalLinks(True)
        explanation = QLabel(
            "Proxyseller 与 3X-UI 只使用本地库。3X-UI 根据已核实的配置代理地址查地区，"
            "不会通过代理发送检测流量。")
        explanation.setWordWrap(True)
        self.select = QPushButton("从本地导入新版 IP2Location LITE DB3 (.BIN)…")
        self.select.clicked.connect(self.choose)
        layout = QVBoxLayout(self)
        for child in (self.info, explanation, attribution, self.select, self.message):
            layout.addWidget(child)
        layout.addStretch()
        self.timer = QTimer(self, interval=100)
        self.timer.timeout.connect(self.finish)
        self.show_version()

    def show_version(self):
        state = self.geo.version or {"missing": "未找到数据库", "invalid": "数据库不可读取"}.get(
            self.geo.status, "不可用")
        family = {4: " · IPv4", 6: " · IPv4 / IPv6"}.get(self.geo.ip_version, "")
        self.info.setText(f"{self.geo.source} · {state}{family} · 本地离线查询")

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 IP2Location LITE DB3 数据库", "", "BIN (*.BIN *.bin)")
        if path:
            self.import_path(Path(path))

    def import_path(self, path):
        if self.worker:
            return
        self.worker = Worker(lambda: self.geo.import_file(path))
        self.select.setEnabled(False)
        self.message.setText("正在校验并导入本地数据库…")
        self.worker.start()
        self.timer.start()

    def finish(self):
        if self.worker is None or self.worker.is_alive():
            return
        ok = self.worker.result is True
        self.worker = None
        self.timer.stop()
        self.select.setEnabled(True)
        self.show_version()
        self.message.setText("已导入并刷新。" if ok else "导入失败；请核对 DB3 BIN 文件及数据目录权限。")
        if ok:
            self.changed.emit()

    def shutdown(self):
        return self.worker is None or not self.worker.is_alive()
