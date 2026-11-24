from collections.abc import Callable
from datetime import datetime

from PySide6.QtWidgets import (
    QAbstractItemView, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from proxy_manager.storage.models import Operation, SyncRun
from proxy_manager.ui.pages.overview import OUTCOME_LABELS

ACTION_LABELS = {
    "sync": "同步 Proxyseller", "export": "导出 Proxyseller 代理",
    "key_save": "保存 API Key", "key_delete": "移除 API Key",
    "annotation_edit": "编辑地区或标签",
}


def _time(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().hide()
    return table


def _fill(table: QTableWidget, records) -> None:
    table.setRowCount(len(records))
    for row, values in enumerate(records):
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(str(value)))
    table.resizeColumnsToContents()


class HistoryPage(QWidget):
    def __init__(self):
        super().__init__()
        self.sync_table = _table(("开始（UTC）", "结束（UTC）", "结果", "代理数", "失败原因"))
        self.operation_table = _table(("时间（UTC）", "操作", "结果"))
        tabs = QTabWidget()
        tabs.addTab(self.sync_table, "同步历史")
        tabs.addTab(self.operation_table, "操作日志")
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

    def refresh(
        self, runs: list[SyncRun], operations: list[Operation], error_label: Callable,
    ) -> None:
        _fill(self.sync_table, [
            (
                _time(run.started_at), _time(run.finished_at),
                OUTCOME_LABELS.get(run.outcome, "—"), run.count if run.count is not None else "—",
                error_label(run.reason) if run.reason else "—",
            ) for run in runs
        ])
        _fill(self.operation_table, [
            (
                _time(row.happened_at), ACTION_LABELS.get(row.action, "本地操作"),
                OUTCOME_LABELS.get(row.outcome, "—"),
            ) for row in operations
        ])
