from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel

from proxy_manager.display_labels import status_label
from proxy_manager.services.geoip import proxy_location_fields
from proxy_manager.storage.models import Proxy


class ProxyDetailsDialog(QDialog):
    def __init__(self, row: Proxy, parent=None, *, geo=None):
        super().__init__(parent)
        self.setWindowTitle("Proxyseller 代理详情")
        layout = QFormLayout(self)
        country, province, city = proxy_location_fields(row, geo)
        for label, value in (
            ("代理 ID", row.provider_id),
            ("订单 ID", row.order_id),
            ("IP 地址", row.address),
            ("HTTP 端口", row.port_http),
            ("SOCKS5 端口", row.port_socks),
            ("用户名", row.username),
            ("密码", "已加密保存" if row.password_encrypted is not None else "未提供"),
            ("国家", country or "—"),
            ("州/省", province or "—"),
            ("城市", city or "—"),
            ("手工地区备注", row.region or "—"),
            ("状态", status_label(row.status)),
            ("到期日", row.expires_on),
            ("标签", row.tag),
        ):
            field = QLabel(str(value) if value is not None else "—")
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addRow(label, field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
