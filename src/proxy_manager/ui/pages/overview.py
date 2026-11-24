from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget

from proxy_manager.storage.models import Proxy, SyncRun

OUTCOME_LABELS = {
    "success": "成功", "failed": "失败", "cancelled": "已取消",
}


class OverviewPage(QWidget):
    def __init__(self):
        super().__init__()
        self.account = QLabel()
        self.total = QLabel()
        self.last_success = QLabel()
        self.last_sync = QLabel()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(4)
        layout.setColumnMinimumWidth(2, 12)
        layout.setColumnStretch(5, 1)
        for row, column, title, value in (
            (0, 0, "API Key", self.account),
            (1, 0, "已保存 Proxyseller 代理", self.total),
            (0, 3, "上次成功同步（UTC）", self.last_success),
            (1, 3, "最近同步（UTC）", self.last_sync),
        ):
            layout.addWidget(QLabel(title), row, column)
            layout.addWidget(value, row, column + 1)

    def refresh(
        self, has_key: bool, rows: list[Proxy], runs: list[SyncRun], latest_success: SyncRun | None,
    ) -> None:
        self.account.setText("已保存" if has_key else "未配置")
        self.total.setText(str(len(rows)))
        self.last_success.setText(
            latest_success.finished_at.isoformat(sep=" ", timespec="seconds") if latest_success else "—"
        )
        latest = runs[0] if runs else None
        if latest is not None:
            time = latest.finished_at.isoformat(sep=" ", timespec="seconds")
            self.last_sync.setText(f"{time}  {OUTCOME_LABELS[latest.outcome]}")
        else:
            self.last_sync.setText("—")
