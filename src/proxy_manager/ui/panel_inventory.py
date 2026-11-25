from dataclasses import replace
from html import escape
from threading import Event
from time import monotonic, time

import qrcode
from qrcode.exceptions import DataOverflowError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from proxy_manager.display_labels import natural_key
from proxy_manager.provider.panel_client import PanelError, fetch_all_clients, fetch_subscription_url
from proxy_manager.services.geoip import SOURCE
from proxy_manager.services.panel_geo import Result, inspect_panel, record_ids
from proxy_manager.services.panel_proxy_links import LIVE_ROUTE_NOTE, is_relay_client, unique_proxy_links
from .vps_views import local_stamp, plain
from .workers import Worker
from .cards import BYTES_PER_DISPLAY_GB


CLIENT_INTERVAL_MS = 5000
GEO_RETRY_INTERVAL = 60
GEO_STATES = {
    "default_direct": "默认直连", "direct": "直连", "blocked": "阻断",
    "ambiguous": "出口不确定", "domain": "域名待确认", "nonpublic": "非公网地址",
    "ssh_unavailable": "暂不可用 · SSH 未配置或指纹未确认", "changed": "配置变化，等待重新核对",
}
PANEL_HEADERS = ("客户端", "国家", "州/省", "城市", "分组", "在线", "数据", "当前速度")
NAME_COLUMN, COUNTRY_COLUMN, PROVINCE_COLUMN, CITY_COLUMN, GROUP_COLUMN, ONLINE_COLUMN, DATA_COLUMN, SPEED_COLUMN = range(
    len(PANEL_HEADERS))
SORTABLE_COLUMNS = {NAME_COLUMN, GROUP_COLUMN, ONLINE_COLUMN, DATA_COLUMN}
ONLINE_TEXT = QColor("#00632d")
ONLINE_BACKGROUND = QColor("#ddf5e5")
ERRORS = {
    "auth": "面板拒绝认证，请核对 API Token。",
    "path": "面板没有提供此接口，请核对面板版本。",
    "redirect": "面板返回重定向，请核对 HTTPS 面板地址。",
    "rate": "面板请求受限，请稍后刷新。",
    "timeout": "读取超时，请稍后刷新。",
    "network": "无法连接面板，请检查网络和 HTTPS 证书。",
    "subscription": "面板未启用订阅，或未提供有效订阅地址。",
    "pagination": "面板列表在分页读取期间发生变化，请重新刷新。",
}


def _bytes(value):
    if value is None:
        return "—"
    for unit, size in (("TiB", 2**40), ("GB", BYTES_PER_DISPLAY_GB), ("MiB", 2**20), ("KiB", 2**10)):
        if value >= size:
            return f"{value / size:,.2f} {unit}"
    return f"{value:,} B"


def _client_speed(totals, previous, elapsed):
    if previous is None or not 1 <= elapsed <= 15:
        return "—"
    up, down = totals
    old_up, old_down = previous
    if None in (up, down, old_up, old_down) or up < old_up or down < old_down:
        return "—"
    multiplier = 8 / (elapsed * 1_000_000)
    return f"↑ {(up - old_up) * multiplier:.2f} / ↓ {(down - old_down) * multiplier:.2f} Mbps"


def attach_confirmed_ids(data, ids):
    """Join only the exact roster by its API email; never infer from the visible label."""
    if (len(ids) != len(data.rows) or ids.keys() != {row.name for row in data.rows}
            or any(row.record_id is not None and row.record_id != ids[row.name] for row in data.rows)):
        return None
    return replace(data, rows=tuple(replace(row, record_id=ids[row.name]) for row in data.rows))


def subscription_qr(url):
    code = qrcode.QRCode(border=4)
    code.add_data(url)
    code.make(fit=True)
    modules = code.get_matrix()
    scale = max(3, 256 // len(modules))
    image = QImage(len(modules) * scale, len(modules) * scale, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    with QPainter(image) as painter:
        for y, row in enumerate(modules):
            for x, black in enumerate(row):
                if black:
                    painter.fillRect(x * scale, y * scale, scale, scale, Qt.GlobalColor.black)
    return QPixmap.fromImage(image)


class PanelInventory(QWidget):
    proxy_links_changed = Signal(object)

    def __init__(self, *, geo=None, ssh_context=None):
        super().__init__()
        self.geo = geo
        self.ssh_context = ssh_context
        self.geo_wait_for_clients = False
        self.geo_worker = None
        self.geo_cancel = None
        self.geo_result = None
        self.geo_pending = True
        self.geo_expected = None
        self.geo_retry_at = 0
        self.geo_message = "等待读取配置代理地区"
        self.linked_active = False
        self.credentials = None
        self.data = None
        self.page = 1
        self.sort_column = NAME_COLUMN
        self.visible_rows = ()
        self.speeds = {}
        self.stamp = None
        self.online_stale = False
        self.counters = {}
        self.sample_time = None
        self.job = None
        self.dialog = None
        self.pending_qr = None
        self.closing = False
        self.client_paused = False
        self.refresh = QPushButton("刷新客户端")
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.status = plain("请在系统设置中配置 3X-UI 面板。")
        self.geo_status = plain("等待读取配置代理地区")
        self.table = QTableWidget(0, len(PANEL_HEADERS))
        self.table.setHorizontalHeaderLabels(PANEL_HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicator(NAME_COLUMN, Qt.SortOrder.AscendingOrder)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.sort_by)
        for index in range(self.table.columnCount()):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        actions = QHBoxLayout()
        for button in (self.refresh, self.previous, self.next):
            actions.addWidget(button)
        actions.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(plain("流量直接取自 3X-UI 面板。中转VPS 不参与流量排序；双击客户端名称查看订阅二维码。"))
        if self.geo is not None:
            layout.addWidget(plain("地区随客户端列表显示。点击「刷新客户端」可重新核对配置代理地址；"
                                   "地区不代表全部流量的实际出口。"))
            layout.addWidget(self.geo_status)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.finish)
        self.client_timer = QTimer(self)
        self.client_timer.setSingleShot(True)
        self.client_timer.setInterval(CLIENT_INTERVAL_MS)
        self.client_timer.timeout.connect(self.load)
        self.geo_timer = QTimer(self, interval=500)
        self.geo_timer.timeout.connect(self.geo_tick)
        self.refresh.clicked.connect(self.refresh_clients)
        self.previous.clicked.connect(lambda: self.change_page(-1))
        self.next.clicked.connect(lambda: self.change_page(1))
        self.table.cellDoubleClicked.connect(self.open_subscription)
        self.update_buttons()

    def set_credentials(self, credentials):
        if self.credentials == credentials:
            return
        self.credentials = credentials
        self.invalidate_geo()
        self.geo_wait_for_clients = False
        self.data = None
        self.page = 1
        self.visible_rows = ()
        self.table.setRowCount(0)
        self.reset_sampling()
        self.client_paused = False
        if self.dialog:
            self.dialog.close()
        self.status.setText("等待读取客户端…" if credentials else "请在系统设置中配置 3X-UI 面板。")
        self.update_buttons()
        self.schedule(immediate=True)

    def update_buttons(self):
        self.refresh.setEnabled(self.job is None and self.credentials is not None)
        self.previous.setEnabled(self.page > 1)
        self.next.setEnabled(self.data is not None and self.page * self.data.page_size < self.data.total)

    def schedule(self, *, immediate=False):
        if self.closing or not self.tracking_visible() or self.client_paused or self.job or self.credentials is None:
            self.client_timer.stop()
        elif not self.client_timer.isActive():
            self.client_timer.start(0 if immediate and self.data is None else CLIENT_INTERVAL_MS)

    def showEvent(self, event):
        super().showEvent(event)
        self.schedule(immediate=True)
        if self.geo is not None:
            self.geo_timer.start()

    def hideEvent(self, event):
        if not self.linked_active:
            self.client_timer.stop()
            self.geo_timer.stop()
            if not self.closing:
                self.reset_sampling(stale=True)
        super().hideEvent(event)

    def tracking_visible(self):
        return self.isVisible() or (self.linked_active and self.window().isVisible())

    def set_linked_active(self, active):
        self.linked_active = active
        if self.tracking_visible() and not self.closing:
            self.schedule(immediate=True)
            if self.geo is not None:
                self.geo_timer.start()
        else:
            self.client_timer.stop()
            self.geo_timer.stop()
            if not self.closing:
                self.reset_sampling(stale=True)

    def refresh_clients(self):
        if self.closing or self.job is not None or self.credentials is None:
            return
        if self.geo is not None:
            if self.geo_cancel is not None:
                self.geo_cancel.set()
            self.geo_expected = None
            self.geo_pending = True
            self.geo_retry_at = 0
            self.geo_wait_for_clients = True
            self.geo_message = "正在刷新客户端与配置代理地区…"
            self.repaint_geo()
        self.load()

    def load(self):
        if self.closing or self.job is not None or self.credentials is None:
            return
        credentials = self.credentials

        def read():
            data = fetch_all_clients(credentials)
            captured = monotonic()
            if self.geo is not None and any(row.record_id is None for row in data.rows):
                try:
                    data = attach_confirmed_ids(data, record_ids(credentials)) or data
                except Exception:
                    # Traffic and QR remain usable without an ID-confirmed GeoIP join.
                    pass
            return data, captured

        self.status.setText("正在读取 3X-UI 客户端…")
        self.start_job("clients", read)

    def start_job(self, kind, action):
        def run():
            try:
                return action()
            except PanelError as error:
                return str(error)

        worker = Worker(run)
        self.job = (worker, kind, self.credentials)
        self.client_timer.stop()
        self.update_buttons()
        worker.start()
        self.timer.start()

    def display(self, data, sampled):
        elapsed = sampled - self.sample_time if self.sample_time is not None else 0
        self.speeds = {client.name: _client_speed((client.up, client.down), self.counters.get(client.name), elapsed)
                       for client in data.rows}
        previous = self.geo_signature()
        if self.geo is not None and previous and all(ident is not None for ident, _ in previous):
            # The paginated list omits IDs on some panels. A transient failure of
            # the supplemental read is not a roster change when the API emails
            # are identical and all explicitly supplied IDs still agree.
            data = attach_confirmed_ids(data, {user: ident for ident, user in previous}) or data
        self.data = data
        if self.geo is not None and previous != self.geo_signature():
            self.invalidate_geo()
        self.counters = {client.name: (client.up, client.down) for client in data.rows if client.name}
        self.sample_time = sampled
        self.stamp = time()
        self.online_stale = False
        if self.geo_result is not None and not self.geo_pending:
            self.geo_message = ""
        self.render()

    def sort_by(self, column):
        header = self.table.horizontalHeader()
        if column in SORTABLE_COLUMNS:
            self.sort_column = column
            self.page = 1
            if self.data is not None:
                self.render()
        order = Qt.SortOrder.DescendingOrder if self.sort_column in {ONLINE_COLUMN, DATA_COLUMN} else Qt.SortOrder.AscendingOrder
        header.setSortIndicator(self.sort_column, order)

    def change_page(self, offset):
        if self.data is not None:
            self.page += offset
            self.render()

    def total_bytes(self, client):
        up, down = client.up, client.down
        return up + down if up is not None and down is not None else -1

    def usage_display(self, client):
        up, down = client.up, client.down
        total = self.total_bytes(client)
        scope = ("来源：3X-UI 面板当前计数" if total >= 0 else "面板暂无完整流量计数")
        if is_relay_client(client.name):
            scope += " · 不参与流量排序"
        return _bytes(total if total >= 0 else None), f"↑ 上传 {_bytes(up)}    ↓ 下载 {_bytes(down)}\n{scope}"

    def render(self):
        rows = sorted(self.data.rows, key=lambda client: natural_key(client.name))
        if self.sort_column == GROUP_COLUMN:
            rows.sort(key=lambda client: (not client.group, natural_key(client.group or "")))
        elif self.sort_column == ONLINE_COLUMN and not self.online_stale:
            rows.sort(key=lambda client: {True: 0, False: 1}.get(client.online, 2))
        elif self.sort_column == DATA_COLUMN:
            rows.sort(key=lambda client: (is_relay_client(client.name), -self.total_bytes(client)))
        pages = max(1, (len(rows) + self.data.page_size - 1) // self.data.page_size)
        self.page = max(1, min(self.page, pages))
        start = (self.page - 1) * self.data.page_size
        selected = self.table.currentRow()
        selected_name = self.visible_rows[selected].name if 0 <= selected < len(self.visible_rows) else None
        self.visible_rows = rows[start:start + self.data.page_size]
        self.table.setRowCount(len(self.visible_rows))
        self.table.clearSelection()
        for index, client in enumerate(self.visible_rows):
            state = {True: "在线", False: "离线"}.get(client.online, "—") if not self.online_stale else "—"
            locations, tooltip = self.geo_display(client)
            usage, usage_detail = self.usage_display(client)
            values = (client.name or "—", *locations, client.group or "—", state,
                      usage, self.speeds.get(client.name, "—"))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if client.online and not self.online_stale:
                    item.setForeground(ONLINE_TEXT)
                    item.setBackground(ONLINE_BACKGROUND)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if column == DATA_COLUMN:
                    item.setToolTip(usage_detail)
                if column in (COUNTRY_COLUMN, PROVINCE_COLUMN, CITY_COLUMN) and tooltip:
                    item.setToolTip(tooltip)
                self.table.setItem(index, column, item)
            if selected_name == client.name:
                self.table.selectRow(index)
        if not self.online_stale:
            self.status.setText(f"共 {self.data.total} 条 · 第 {self.page} / {pages} 页 · 更新于 {local_stamp(self.stamp)}")
        self.update_buttons()
        cached = f"{self.geo_message} · 暂显示上次核对结果" if self.geo_result and self.geo_message else ""
        self.geo_status.setText(cached or self.geo_message or "配置代理地区已核对")
        self.proxy_links_changed.emit(unique_proxy_links(
            self.data, self.geo_result, self.usage_display, self.total_bytes, verification_note=cached))

    def reset_sampling(self, stale=False):
        self.sample_time = None
        self.counters.clear()
        self.speeds.clear()
        if self.data is not None:
            self.online_stale |= stale
            self.render()

    def open_subscription(self, row, column):
        if column != NAME_COLUMN or not 0 <= row < len(self.visible_rows) or self.credentials is None or self.dialog is not None:
            return
        client = self.visible_rows[row]
        self.dialog = QDialog(self)
        self.dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.dialog.setWindowTitle("订阅二维码")
        layout = QVBoxLayout(self.dialog)
        layout.addWidget(plain(client.name))
        self.qr_image = QLabel()
        self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr_image)
        self.qr_status = plain("正在读取订阅地址…" if client.sub_id else "此客户端没有订阅 ID。")
        layout.addWidget(self.qr_status)
        self.dialog.finished.connect(self.dialog_closed)
        self.dialog.open()
        if client.sub_id:
            self.pending_qr = client.sub_id
            self.load_subscription()

    def dialog_closed(self, *_):
        self.dialog = self.pending_qr = None

    def load_subscription(self):
        if self.job is None and self.pending_qr is not None and self.credentials is not None:
            credentials, sub_id = self.credentials, self.pending_qr
            self.pending_qr = None
            self.start_job(self.dialog, lambda: fetch_subscription_url(credentials, sub_id))

    def finish(self):
        worker, kind, credentials = self.job
        if worker.is_alive():
            return
        self.timer.stop()
        self.job = None
        if self.credentials == credentials:
            if kind == "clients":
                self.finish_clients(worker)
            elif self.dialog is kind:
                self.finish_subscription(worker)
        self.update_buttons()
        self.load_subscription()
        self.schedule()

    def finish_clients(self, worker):
        self.geo_wait_for_clients = False
        if worker.failed or isinstance(worker.result, str):
            if self.geo is not None:
                if worker.result in {"auth", "path", "redirect"}:
                    self.invalidate_geo()
                self.geo_message = ("客户端列表暂未更新" if self.geo_result is not None
                                    else "暂不可用 · 等待客户端列表读取成功")
            self.reset_sampling(stale=True)
            self.client_paused = worker.result in {"auth", "path", "redirect", "rate"}
            message = ERRORS.get(worker.result, "无法读取客户端列表或面板流量。")
            self.status.setText(message + (" 已保留上次成功的流量数据。" if self.data else ""))
        else:
            self.client_paused = False
            self.display(*worker.result)
            if not self.tracking_visible():
                self.reset_sampling()

    def finish_subscription(self, worker):
        if worker.failed or worker.result in ERRORS:
            self.qr_status.setText(ERRORS.get(worker.result, "无法读取订阅地址。"))
            return
        try:
            self.qr_image.setPixmap(subscription_qr(worker.result))
        except DataOverflowError:
            self.qr_status.setText("订阅地址过长，无法生成二维码。")
            return
        self.qr_status.setText("请使用支持订阅导入的客户端扫描。")

    def shutdown(self):
        self.closing = True
        self.client_timer.stop()
        self.timer.stop()
        self.geo_timer.stop()
        if self.geo_cancel is not None:
            self.geo_cancel.set()
        if self.dialog:
            self.dialog.close()
        return ((self.job is None or not self.job[0].is_alive())
                and (self.geo_worker is None or not self.geo_worker.is_alive()))

    def geo_signature(self):
        if self.data is None:
            return frozenset()
        return frozenset((r.record_id, r.name) for r in self.data.rows)

    def invalidate_geo(self):
        if self.geo_cancel is not None:
            self.geo_cancel.set()
        self.geo_expected = self.geo_result = None
        self.geo_pending = True
        self.geo_retry_at = 0
        self.geo_message = "等待读取配置代理地区"
        self.geo_status.setText(self.geo_message)
        self.proxy_links_changed.emit({})

    def repaint_geo(self):
        if self.data is not None:
            self.render()

    def geo_tick(self):
        if self.geo is None or self.closing or not self.tracking_visible():
            return
        if self.geo_worker is not None:
            if self.geo_worker.is_alive():
                return
            result = self.geo_worker.result
            self.geo_worker = self.geo_cancel = None
            expected = self.geo_expected
            if expected and expected[:2] == (self.credentials, self.geo_signature()):
                confirmed = (
                    attach_confirmed_ids(self.data, {user: ident for ident, user in result.users.items()})
                    if result and result.status == "ok" and set(result.rows) == set(result.users) else None)
                if confirmed is not None:
                    self.data = confirmed
                    self.geo_result = result
                    self.geo_pending = any(
                        finding.status in {"ambiguous", "default_direct", "direct"}
                        and not is_relay_client(result.users[ident])
                        for ident, finding in result.rows.items()
                    )
                    self.geo_retry_at = monotonic() + GEO_RETRY_INTERVAL if self.geo_pending else 0
                    self.geo_message = ""
                else:
                    diagnostic = getattr(result, "diagnostic", "")
                    unavailable = {
                        "ssh_host_key_changed": "暂不可用 · SSH 主机指纹不匹配",
                        "snapshot_unavailable": "暂不可用 · 无法核对 Xray 运行配置",
                        "cancelled": "暂不可用 · 核对出站超时",
                        "timeout": "暂不可用 · 3X-UI 路由接口超时",
                        "auth": "暂不可用 · 面板拒绝读取路由",
                        "shape_or_credentials": "暂不可用 · 配置格式或凭证读取失败",
                    }.get(diagnostic, "暂不可用 · SSH 读取失败" if diagnostic.startswith("ssh_")
                          else "暂不可用 · 未能核对配置代理地区")
                    self.geo_message = GEO_STATES.get(getattr(result, "status", None), unavailable)
                    if (getattr(result, "status", None) in {"ok", "changed", "ssh_unavailable"}
                            or diagnostic in {"ssh_host_key_changed", "auth", "path", "redirect"}):
                        # Conflicting evidence makes the previous association unusable.
                        self.geo_result = None
                    if getattr(result, "status", None) in {"ok", "changed"}:
                        self.geo_message = GEO_STATES["changed"]
                    self.geo_retry_at = monotonic() + GEO_RETRY_INTERVAL
                self.repaint_geo()
        if (not self.credentials or self.data is None or not self.data.rows or self.online_stale
                or self.geo_wait_for_clients or not self.geo_pending or monotonic() < self.geo_retry_at):
            return
        self.geo_message = "正在核对配置代理地区…"
        self.repaint_geo()
        credentials = self.credentials
        self.geo_expected = (credentials, self.geo_signature())
        cancel = self.geo_cancel = Event()

        def read():
            profile, pin = self.ssh_context()
            result = inspect_panel(credentials, profile, pin, cancel)
            # SSH settings can change while a worker is active.
            return result if self.ssh_context() == (profile, pin) else Result("changed")

        self.geo_worker = Worker(read)
        self.geo_worker.start()

    def geo_display(self, client):
        if self.geo is None:
            return ("—", "—", client.comment or "—"), None
        match = self.geo_result.rows.get(client.record_id) if self.geo_result else None
        detail = []
        if match is None:
            locations = ("—", "—", self.geo_message)
        elif match.ip:
            loc = self.geo.lookup(match.ip)
            locations = (loc.country or "—", loc.province or "—", loc.city or "—") if loc.label else (
                "—", "—", "未收录归属地" if loc.status == "no_record" else "归属地数据库暂不可用")
            detail.extend((f"国家：{loc.country or '—'}", f"州／省：{loc.province or '—'}",
                           f"城市：{loc.city or '—'}", f"数据来源：{getattr(loc, 'source', SOURCE)}",
                           f"数据库版本：{loc.version or '—'}", f"配置代理 IP：{match.ip}"))
        else:
            locations = ("—", "—", GEO_STATES.get(match.status, "暂不可用"))
        if match and match.conditional:
            detail.append("存在条件分流")
        if match and match.live_only:
            detail.append(LIVE_ROUTE_NOTE)
        if match and match.status == "default_direct":
            detail.append("未命中专属代理；启动配置默认出站为 freedom")
        if match and self.geo_message:
            detail.append(f"{self.geo_message} · 上次核对结果")
        detail.extend(("常规公网流量的配置代理位置，并非实测最终出口。",
                       f"3X-UI 原始备注：{client.comment or '—'}"))
        return locations, "<qt>" + "<br>".join(escape(x) for x in detail) + "</qt>"
