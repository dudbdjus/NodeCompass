from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTableView, QVBoxLayout, QWidget,
)

from proxy_manager.display_labels import natural_key, status_label
from proxy_manager.services.proxy_query import ProxyQuery
from proxy_manager.storage.models import Proxy
from proxy_manager.ui.proxy_details_dialog import ProxyDetailsDialog
from proxy_manager.ui.proxy_table_model import EDITABLE_COLUMNS, EXPIRY_COLUMN, TAG_COLUMN, USAGE_COLUMN, ProxyTableModel


class ProxiesPage(QWidget):
    sync_requested = Signal()
    cancel_requested = Signal()
    export_requested = Signal()
    annotation_status = Signal(str)

    def __init__(self, query: ProxyQuery, *, geo=None):
        super().__init__()
        self.query = query
        self.all_rows: list[Proxy] = []
        self.geo = geo
        self.model = ProxyTableModel(query.repository.save_annotation, self.annotation_status.emit, self, geo=geo)
        self.model.dataChanged.connect(self.annotation_changed)
        self.sort_column = TAG_COLUMN
        self.sort_order = Qt.SortOrder.AscendingOrder
        self.search = QLineEdit(placeholderText="搜索 IP、国家、州/省、城市、标签等")
        self.country = QComboBox()
        self.status = QComboBox()
        self.sync_button = QPushButton("同步 Proxyseller")
        self.cancel_button = QPushButton("取消同步")
        self.detail_button = QPushButton("查看详情")
        self.busy = False
        self.export_button = QPushButton("导出筛选结果")
        self.counter = QLabel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        rows = self.table.verticalHeader()
        row_height = self.table.fontMetrics().height() + 8
        rows.setMinimumSectionSize(row_height)
        rows.setDefaultSectionSize(row_height)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.setSortIndicator(self.sort_column, self.sort_order)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.sort_by)
        self.table.doubleClicked.connect(self.open_row_details)
        self.table.selectionModel().selectionChanged.connect(self.update_selection)

        filters = QHBoxLayout()
        for widget in (self.search, self.country, self.status):
            filters.addWidget(widget)
        self.actions = QHBoxLayout()
        for button in (
            self.sync_button, self.cancel_button, self.detail_button,
            self.export_button,
        ):
            self.actions.addWidget(button)
        self.actions.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(filters)
        layout.addLayout(self.actions)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.counter)

        self.search.textChanged.connect(self.apply_filters)
        self.country.currentIndexChanged.connect(self.apply_filters)
        self.status.currentIndexChanged.connect(self.apply_filters)
        self.sync_button.clicked.connect(self.sync_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.detail_button.clicked.connect(self.open_details)
        self.export_button.clicked.connect(self.export_requested)
        self.set_busy(False, False, False)
        self.reload(None)

    def reload(self, profile_id: str | None) -> None:
        self.all_rows = self.query.load(profile_id)
        self.refresh_country_choices()
        self._facet(self.status, "全部状态", {status_label(row.status) for row in self.all_rows})
        self.apply_filters()

    def refresh_country_choices(self):
        self._facet(self.country, "全部国家", {self.model.labels(row)[0] for row in self.all_rows})

    def refresh_geo(self):
        self.refresh_country_choices()
        self.apply_filters()

    def update_panel_links(self, links):
        if self.model.set_panel_links(links) and (
                self.sort_column in (TAG_COLUMN, USAGE_COLUMN) or self.search.text().strip()):
            self.apply_filters()

    @staticmethod
    def _facet(combo: QComboBox, label: str, values: set[str | None]) -> None:
        selected = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(label, "")
        for value in sorted(filter(None, values)):
            combo.addItem(value, value)
        combo.setCurrentIndex(max(combo.findData(selected), 0))
        combo.blockSignals(False)

    def sort_by(self, column: int) -> None:
        if column not in (TAG_COLUMN, USAGE_COLUMN, EXPIRY_COLUMN):
            return
        if self.sort_column == column:
            self.sort_order = (Qt.SortOrder.DescendingOrder if self.sort_order == Qt.SortOrder.AscendingOrder
                               else Qt.SortOrder.AscendingOrder)
        else:
            self.sort_order = Qt.SortOrder.DescendingOrder if column == USAGE_COLUMN else Qt.SortOrder.AscendingOrder
        self.sort_column = column
        header = self.table.horizontalHeader()
        header.setSortIndicator(column, self.sort_order)
        self.apply_filters()

    def annotation_changed(self, first, _last, roles):
        if Qt.ItemDataRole.EditRole in roles and (self.search.text().strip()
                                                 or first.column() == self.sort_column == TAG_COLUMN):
            QTimer.singleShot(0, self, self.apply_filters)

    def apply_filters(self) -> None:
        previous = self.selected_proxy()
        rows = self.query.filter(
            self.all_rows, self.search.text(), self.country.currentData() or "",
            self.status.currentData() or "", geo=self.geo, panel_links=self.model.panel_links,
        )
        key = {TAG_COLUMN: lambda row: natural_key(tag) if (tag := self.model.tag_for(row)) else None,
               USAGE_COLUMN: self.model.usage_bytes_for,
               EXPIRY_COLUMN: lambda row: row.expires_on}[self.sort_column]
        keyed = [(key(row), row) for row in rows]
        known = [(value, row) for value, row in keyed if value is not None]
        known.sort(key=lambda pair: pair[0], reverse=self.sort_order == Qt.SortOrder.DescendingOrder)
        rows = [row for _, row in known] + [row for value, row in keyed if value is None]
        changed = rows != self.model.rows
        if changed:
            self.model.replace(rows)
        if changed and previous is not None:
            for index, row in enumerate(rows):
                if (row.profile_id, row.provider_id) == (previous.profile_id, previous.provider_id):
                    self.table.selectRow(index)
                    break
        self.counter.setText(f"显示 {len(rows)} 条，共 {len(self.all_rows)} 条")
        self.update_selection()

    def selected_proxy(self) -> Proxy | None:
        indexes = self.table.selectionModel().selectedRows()
        return self.model.rows[indexes[0].row()] if indexes else None

    def update_selection(self) -> None:
        row = self.selected_proxy()
        self.detail_button.setEnabled(row is not None)
        self.export_button.setEnabled(bool(self.model.rows) and not self.busy)

    def open_row_details(self, index: QModelIndex) -> None:
        if index.column() not in EDITABLE_COLUMNS:
            self.open_details()

    def open_details(self) -> None:
        row = self.selected_proxy()
        if row is not None:
            ProxyDetailsDialog(row, self, geo=self.geo).exec()

    def set_busy(self, busy: bool, has_key: bool, cancellable: bool) -> None:
        self.busy = busy
        self.model.editable = not busy
        self.sync_button.setEnabled(has_key and not busy)
        self.cancel_button.setEnabled(cancellable)
        self.update_selection()
