from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from sqlalchemy.exc import SQLAlchemyError

from proxy_manager.display_labels import status_label
from proxy_manager.services.geoip import IP2_OFFLINE, normalize_ip, proxy_location_fields
from proxy_manager.storage.models import Proxy, ProxyAnnotation

HEADERS = (
    "IP 地址", "标签", "国家", "州/省", "城市", "状态",
    "到期日", "本期已用流量",
)
EXPIRY_COLUMN = HEADERS.index("到期日")
TAG_COLUMN = HEADERS.index("标签")
COUNTRY_COLUMN, PROVINCE_COLUMN, CITY_COLUMN = (HEADERS.index(name) for name in ("国家", "州/省", "城市"))
EDITABLE_COLUMNS = {CITY_COLUMN: "region", TAG_COLUMN: "tag"}
USAGE_COLUMN = HEADERS.index("本期已用流量")


class ProxyTableModel(QAbstractTableModel):
    def __init__(
        self, save_annotation: Callable[[Proxy, str, str], ProxyAnnotation],
        notify: Callable[[str], None], parent=None, *, geo=None,
    ):
        super().__init__(parent)
        self.rows: list[Proxy] = []
        self.editable = True
        self.save_annotation = save_annotation
        self.notify = notify
        self.geo = geo
        self.panel_links = {}

    def labels(self, row):
        return proxy_location_fields(row, self.geo)

    def panel_link(self, row):
        return self.panel_links.get(str(normalize_ip(row.address)))

    def tag_for(self, row):
        link = self.panel_link(row)
        return link.tag if link else row.tag

    def usage_bytes_for(self, row):
        link = self.panel_link(row)
        return link.sortable_usage if link else None

    def set_panel_links(self, links):
        if links == self.panel_links:
            return False
        self.panel_links = links
        if self.rows:
            for column in (TAG_COLUMN, USAGE_COLUMN):
                self.dataChanged.emit(self.index(0, column), self.index(len(self.rows) - 1, column),
                                      [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole])
        return True

    def replace(self, rows: list[Proxy]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section]
        return super().headerData(section, orientation, role)

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and self.editable and index.column() in EDITABLE_COLUMNS:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        column = index.column()
        row = self.rows[index.row()]
        link = self.panel_link(row)
        if role == Qt.ItemDataRole.ToolTipRole and column in (COUNTRY_COLUMN, PROVINCE_COLUMN, CITY_COLUMN):
            value = self.labels(row)[column - COUNTRY_COLUMN]
            note = f"来源：{IP2_OFFLINE}" if value else f"{IP2_OFFLINE} 暂无此字段。"
            if column == CITY_COLUMN:
                note += f"\n原手工地区：{row.region or '—'}（双击编辑备注，不覆盖离线城市）"
            return note
        if role == Qt.ItemDataRole.ToolTipRole and column == TAG_COLUMN:
            if link:
                note = f"\n{link.verification_note}" if link.verification_note else ""
                return f"3X-UI 客户端：{link.client}\n原手工标签：{row.tag or '—'}（双击编辑保留值）{note}"
            return "暂无唯一匹配的 3X-UI 客户端；保留手工标签。"
        if role == Qt.ItemDataRole.ToolTipRole and column == USAGE_COLUMN:
            return ("\n".join(filter(None, (f"3X-UI 客户端：{link.client}", link.usage_detail, link.verification_note))) if link
                    else "没有唯一匹配的 3X-UI 配置代理 IP，未关联流量。")
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        if role == Qt.ItemDataRole.EditRole and column in EDITABLE_COLUMNS:
            return getattr(row, EDITABLE_COLUMNS[column])
        country, province, city = self.labels(row)
        fields = (
            row.address, link.tag if link else row.tag or "—", country, province, city,
            status_label(row.status), row.expires_on.isoformat() if row.expires_on else None,
            link.usage if link else "—",
        )
        value = fields[column]
        return str(value) if value not in (None, "") else "—"

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not self.flags(index) & Qt.ItemFlag.ItemIsEditable:
            return False
        row = self.rows[index.row()]
        field = EDITABLE_COLUMNS[index.column()]
        value = str(value).strip()
        if getattr(row, field) == value:
            return True
        try:
            row.annotation = self.save_annotation(row, field, value)
        except SQLAlchemyError:
            self.notify("保存失败，请检查本地数据库。")
            return False
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole,
                                             Qt.ItemDataRole.ToolTipRole])
        self.notify(f"已保存{HEADERS[index.column()]}。")
        return True
