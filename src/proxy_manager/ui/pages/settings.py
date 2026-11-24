from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

class SettingsPage(QWidget):
    save_requested = Signal()
    delete_requested = Signal()

    def __init__(self, directory: Path):
        super().__init__()
        self.key_status = QLabel()
        self.key_input = QLineEdit(placeholderText="粘贴 Proxyseller API Key")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setClearButtonEnabled(True)
        self.save_key = QPushButton("保存 API Key")
        self.delete_key = QPushButton("移除 API Key")
        self.open_directory = QPushButton("打开本地数据目录")

        directory_label = QLabel(str(directory))
        directory_label.setTextFormat(Qt.TextFormat.PlainText)
        directory_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form = QFormLayout()
        form.addRow("凭证状态", self.key_status)
        form.addRow("API Key", self.key_input)
        form.addRow("数据目录", directory_label)

        key_actions = QHBoxLayout()
        key_actions.addWidget(self.save_key)
        key_actions.addWidget(self.delete_key)
        key_actions.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(key_actions)
        layout.addWidget(self.open_directory)
        layout.addStretch()

        self.save_key.clicked.connect(self.save_requested)
        self.delete_key.clicked.connect(self.delete_requested)
        self.key_input.returnPressed.connect(self.save_requested)
        self.open_directory.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
        )

    def update_state(self, has_key: bool, busy: bool, read_failed: bool) -> None:
        if read_failed:
            self.key_status.setText("无法读取 Windows 凭证")
        else:
            self.key_status.setText("已保存在 Windows 凭证管理器" if has_key else "未配置")
        self.key_input.setEnabled(not busy)
        self.save_key.setEnabled(not busy)
        self.delete_key.setEnabled((has_key or read_failed) and not busy)
