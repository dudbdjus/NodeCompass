from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLabel, QVBoxLayout, QWidget


class GeneralSettings(QWidget):
    def __init__(self, directory, startup=None):
        super().__init__()
        self.startup = startup
        self.preferences = QSettings(str(Path(directory) / "desktop.ini"), QSettings.Format.IniFormat, self)
        self.auto_start = QCheckBox("Windows 登录后自动打开软件")
        self.startup_status = QLabel()
        self.startup_status.setWordWrap(True)
        self.close_choice = QComboBox()
        self.close_choice.addItem("最小化到托盘", "tray")
        self.close_choice.addItem("关闭软件", "exit")
        saved = self.preferences.value("close_behavior", "exit")
        index = self.close_choice.findData(saved)
        self.close_choice.setCurrentIndex(index if index >= 0 else self.close_choice.findData("exit"))
        self.close_status = QLabel()
        self.close_status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("开机自启动", self.auto_start)
        form.addRow("", self.startup_status)
        form.addRow("点击关闭按钮", self.close_choice)
        form.addRow("", QLabel("最小化到托盘时，可从托盘菜单打开窗口或退出软件。"))
        form.addRow("", self.close_status)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch()
        self.auto_start.clicked.connect(self.save_startup)
        self.close_choice.currentIndexChanged.connect(self.save_close_choice)
        self.refresh_startup()

    @property
    def close_behavior(self):
        return self.close_choice.currentData()

    def refresh_startup(self):
        self.auto_start.setEnabled(self.startup is not None)
        if self.startup is None:
            self.startup_status.setText("请在 Windows EXE 中配置开机自启动。")
            return
        try:
            registered = self.startup.registered_command()
        except OSError:
            self.auto_start.setEnabled(False)
            self.startup_status.setText("暂时无法读取 Windows 启动项。")
            return
        owned = registered == self.startup.command
        self.auto_start.setChecked(owned)
        self.startup_status.setText(
            "已开启。登录 Windows 时会使用当前程序和数据目录。"
            if owned else "已绑定到另一份程序。勾选可改为启动当前这份。"
            if registered is not None else "未开启。")

    def save_startup(self, checked):
        if self.startup is None:
            return
        try:
            self.startup.set_enabled(checked)
        except (OSError, ValueError) as error:
            self.refresh_startup()
            self.startup_status.setText("设置失败：请确认 EXE 仍在原位置且路径不过长。"
                                        if isinstance(error, ValueError) else "无法修改 Windows 启动项。")
        else:
            self.refresh_startup()

    def save_close_choice(self):
        self.preferences.setValue("close_behavior", self.close_behavior)
        self.preferences.sync()
        self.close_status.setText("设置仅本次有效：无法写入本地首选项。"
                                  if self.preferences.status() != QSettings.Status.NoError else "")
