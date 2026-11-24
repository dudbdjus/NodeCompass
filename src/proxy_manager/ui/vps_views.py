from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from time import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from proxy_manager.monitoring.ssh_client import Endpoint
from proxy_manager.provider.kiwivm_client import KiwiVMEndpoint
from .cards import card, display_gb, text_label
from .vps_charts import TrafficChart


def local_stamp(timestamp):
    if timestamp is not None:
        try:
            return datetime.fromtimestamp(timestamp, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except (ValueError, OSError, OverflowError):
            pass
    return "—"


def label_value(value):
    return "—" if value is None or value == "" else str(value)


def renewal_text(value):
    return f"续费时间：{value or '未填写'}"


def percentage(value):
    return f"{value:.1f}%" if value is not None else "—"


def plain(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


def usage_bar():
    bar = QProgressBar()
    bar.setProperty("role", "usage")
    bar.setRange(0, 1000)
    bar.setTextVisible(False)
    bar.setVisible(False)
    return bar


def show_usage(bar, usage):
    bar.setVisible(usage is not None)
    bar.setValue(max(0, min(1000, int(usage * 10))) if usage is not None else 0)


class Readings(QWidget):
    def __init__(self, title, rows):
        super().__init__()
        self.outer = QVBoxLayout(self)
        group = QGroupBox(title)
        form = QFormLayout(group)
        self.labels = {}
        for row in rows:
            self.labels[row] = plain("—")
            form.addRow(row, self.labels[row])
        self.outer.addWidget(group)

    def fill(self, values):
        for key, label in self.labels.items():
            label.setText(label_value(values.get(key)))


class OverviewView(Readings):
    def __init__(self):
        super().__init__("KiwiVM 服务资料", ("VPS 名称", "IP 地址", "地理位置", "虚拟化", "套餐", "内存规格",
                                           "磁盘规格", "Swap 规格", "周期额度", "已用流量", "剩余流量",
                                           "下次流量重置", "最近成功更新"))
        cards = QHBoxLayout()
        self.billing_card, billing = card("计费流量", "KiwiVM · 当期已用 / 额度")
        self.usage_value = text_label("—", "metric")
        self.usage_detail = text_label("—", "muted")
        self.usage = usage_bar()
        billing.addWidget(self.usage_value)
        billing.addWidget(self.usage)
        billing.addWidget(self.usage_detail)
        cards.addWidget(self.billing_card, 2)
        service, service_layout = card("VPS 套餐")
        self.plan = text_label("—", "reading")
        self.location = text_label("—", "muted")
        service_layout.addWidget(self.plan)
        service_layout.addWidget(self.location)
        self.renewal = text_label(renewal_text(None), "reading")
        service_layout.addWidget(self.renewal)
        self.os_value = plain("操作系统：—")
        self.panel_version = plain("3X-UI 版本：—")
        self.xray_version = plain("Xray 版本：—")
        for label in (self.os_value, self.panel_version, self.xray_version):
            label.setWordWrap(True)
            service_layout.addWidget(label)
        cards.addWidget(service, 1)
        self.outer.insertLayout(0, cards)
        network, network_layout = card("网卡实时带宽", "SSH · 过去 1 小时")
        self.rate = text_label("RX — Mbps    TX — Mbps", "reading")
        self.chart = TrafficChart("VPS 网卡 · Mbps", colors=("#2166d9", "#00859d"), grid=True)
        self.chart.setMinimumHeight(170)
        network_layout.addWidget(self.rate)
        network_layout.addWidget(self.chart, 1)
        self.outer.insertWidget(1, network, 1)

    def show_data(self, profile, latest):
        stamp, data = latest.get(KiwiVMEndpoint.SERVICE_INFO, (None, {}))
        live_stamp, live = latest.get(KiwiVMEndpoint.LIVE_SERVICE_INFO, (None, {}))
        service = live.get("service", {})
        info = {**{key: value for key, value in service.items() if value is not None},
                **{key: value for key, value in data.items() if value is not None and key != "billing"}}
        billing = data.get("billing") or service.get("billing") or {}
        if not any(value is not None for value in billing.values()):
            billing = service.get("billing") or {}
        quota, used, available, usage = billing_values(billing)
        self.usage_value.setText(display_gb(billing.get("raw_used_bytes")))
        self.usage_detail.setText(f"额度 {display_gb(billing.get('raw_quota_bytes'))} · 已用 {percentage(usage)}")
        show_usage(self.usage, usage)
        self.plan.setText(label_value(info.get("plan") or profile.plan))
        self.location.setText(label_value(info.get("node_location") or info.get("node_datacenter") or profile.location))
        stamps = [value for value in (stamp, live_stamp) if value is not None]
        as_reported = lambda value: f"{value:,}（API 原值）" if value is not None else None
        self.fill({
            "VPS 名称": info.get("hostname") or live.get("live_hostname") or profile.name,
            "IP 地址": ", ".join(info.get("ip_addresses") or ()) or profile.ssh_host,
            "地理位置": info.get("node_location") or info.get("node_datacenter") or profile.location,
            "虚拟化": info.get("vm_type"), "套餐": info.get("plan") or profile.plan,
            "内存规格": as_reported(info.get("plan_ram")), "磁盘规格": as_reported(info.get("plan_disk")),
            "Swap 规格": as_reported(info.get("plan_swap")), "周期额度": quota, "已用流量": used, "剩余流量": available,
            "下次流量重置": local_stamp(billing.get("reset_timestamp")), "最近成功更新": local_stamp(max(stamps) if stamps else None),
        })

    def show_network(self, points, last, gap_seconds):
        now = time()
        window = (now - 3600, now + 1)
        visible = [point for point in points if window[0] <= point.timestamp <= window[1]]
        self.chart.update_series(visible, gap_seconds, bounds=window)
        receive = f"{last.rx:.2f}" if last and last.rx is not None else "—"
        send = f"{last.tx:.2f}" if last and last.tx is not None else "—"
        self.rate.setText(f"最近有效  RX {receive} Mbps    TX {send} Mbps")


def billing_values(billing):
    quota, used = billing.get("raw_quota_bytes"), billing.get("raw_used_bytes")
    if quota is None or used is None:
        remaining = None
    else:
        remaining = max(0, quota - used)
    usage = (Decimal(used) * 100 / Decimal(quota)
             if quota and used is not None and billing.get("multiplier") is not None else None)
    return (*(f"{n:,} 原始字节" if n is not None else None for n in (quota, used, remaining)), usage)


def cadence_text(seconds):
    for unit, divisor in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds >= divisor and seconds % divisor == 0:
            return f"{seconds // divisor} {unit}"
    return f"{seconds} 秒"


class KiwiView(Readings):
    def __init__(self):
        super().__init__("KiwiVM · 当前计费周期", ("周期额度", "已用流量", "剩余流量", "原始计量比例",
                                                "下次流量重置", "计费更新时间", "历史更新时间"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.outer.addWidget(self.progress)
        self.resolution = QComboBox()
        self.history_note = plain()
        self.outer.addWidget(self.resolution)
        self.outer.addWidget(self.history_note)
        self.chart = TrafficChart("KiwiVM 原始数据点 · 字节")
        self.outer.addWidget(self.chart, 1)

    def show_data(self, latest, cadences):
        service_stamp, service = latest.get(KiwiVMEndpoint.SERVICE_INFO, (None, {}))
        live_stamp, live = latest.get(KiwiVMEndpoint.LIVE_SERVICE_INFO, (None, {}))
        billing = service.get("billing") or {}
        if not any(value is not None for value in billing.values()):
            billing = live.get("service", {}).get("billing", {})
            service_stamp = live_stamp
        quota, used, available, usage = billing_values(billing)
        history_stamp, history = latest.get(KiwiVMEndpoint.RAW_USAGE_STATS, (None, {}))
        current = self.resolution.currentData()
        cadence = history.get("observed_interval_seconds") or 0
        self.fill({"周期额度": quota, "已用流量": used, "剩余流量": available, "原始计量比例": percentage(usage),
                   "下次流量重置": local_stamp(billing.get("reset_timestamp")), "计费更新时间": local_stamp(service_stamp),
                   "历史更新时间": local_stamp(history_stamp)})
        self.progress.setValue(min(1000, int(usage * 10)) if usage is not None else 0)
        self.progress.setFormat(percentage(usage) if usage is not None else "比例暂不可用")
        self.resolution.blockSignals(True)
        self.resolution.clear()
        for interval in cadences or (0,):
            self.resolution.addItem(cadence_text(interval) if interval else "未识别固定时间粒度", interval)
        if current not in cadences:
            current = cadence
        self.resolution.setCurrentIndex(max(0, self.resolution.findData(current)))
        self.resolution.setEnabled(len(cadences) > 1)
        self.resolution.blockSignals(False)
        self.history_note.setText("原始点的累计或区间语义待核对。按返回的时间戳绘图，缺点处留空。"
                                  "\n当前额度显示 API 原始字节。GB 显示与 KiwiVM 口径一致（1 GB = 2³⁰ 字节）。")
    def show_points(self, points):
        cadence = self.resolution.currentData()
        self.chart.update_series(points, cadence * 1.5 if cadence else None)


class BandwidthView(Readings):
    def __init__(self):
        super().__init__("SSH · 实时带宽", ("VPS 接收 RX", "VPS 发送 TX", "采样间隔", "界面状态", "当前网卡",
                                          "最近有效速率", "当前范围实采流量"))
        bar = QHBoxLayout()
        self.range = QComboBox()
        for title, hours in (("最近 1 小时", 1), ("最近 24 小时", 24), ("最近 7 天（分钟汇总）", 168)):
            self.range.addItem(title, hours)
        self.interface = QComboBox()
        self.interface.addItem("自动选择出口", None)
        self.interface.setToolTip("选择一个接口。可在连接设置中手动输入接口名。")
        bar.addWidget(self.range)
        bar.addWidget(self.interface)
        self.outer.addLayout(bar)
        self.chart = TrafficChart("SSH 本地采样 · Mbps")
        self.outer.addWidget(self.chart, 1)

    def show_data(self, points, last, status, totals):
        self.fill({"VPS 接收 RX": f"{last.rx:.2f} Mbps" if last and last.rx is not None else None,
                   "VPS 发送 TX": f"{last.tx:.2f} Mbps" if last and last.tx is not None else None,
                   "采样间隔": "5 秒", "界面状态": status, "当前网卡": last.interface if last else None,
                   "最近有效速率": local_stamp(last.timestamp if last else None),
                   "当前范围实采流量": f"RX {totals[0]:,} B / TX {totals[1]:,} B" if totals is not None else None})
        hours = self.range.currentData()
        now = time()
        self.chart.update_series(points, 15 if hours <= 48 else 90, bounds=(now - hours * 3600, now + 1))


class SystemView(Readings):
    def __init__(self):
        super().__init__("SSH · Linux 系统监控", ("负载（1 / 5 / 15 分钟）", "运行时间", "x-ui.service",
                                                  "Xray 进程", "操作系统", "3X-UI 版本", "Xray 版本", "最近成功更新"))
        self.panel_versions = (None, None)
        cards = QHBoxLayout()
        self.bars = []
        for title, key in (("CPU", "CPU"), ("内存", "内存（可用 / 总量）"), ("交换空间", "Swap"),
                           ("存储", "根分区磁盘（已用 / 总量）")):
            frame, layout = card(title)
            value = text_label("—", "reading")
            bar = usage_bar()
            layout.addWidget(value)
            layout.addWidget(bar)
            self.labels[key] = value
            self.bars.append(bar)
            cards.addWidget(frame, 1)
        self.outer.insertLayout(0, cards)
        self.outer.addStretch()

    def show_panel_versions(self, panel_version, xray_version):
        self.panel_versions = panel_version, xray_version
        for key, version in zip(("3X-UI 版本", "Xray 版本"), self.panel_versions):
            self.labels[key].setText(f"{version}（3X-UI 面板）" if version else "—")

    def show_data(self, latest):
        stamp, data = latest or (None, {})
        memory, swap, disk = (data.get(name) or {} for name in ("memory", "swap", "root_disk"))
        service, xray = data.get("x_ui") or {}, data.get("xray")
        load, uptime = data.get("load"), data.get("uptime_seconds")
        disk_use = 100 * disk["used_bytes"] / disk["total_bytes"] if disk and disk["total_bytes"] else None
        for bar, usage in zip(self.bars, (data.get("cpu_percent"), memory.get("used_percent"), swap.get("used_percent"), disk_use)):
            show_usage(bar, usage)

        def size(value):
            return display_gb(value, group=False)

        state = {"active": "运行中", "inactive": "未运行", "failed": "失败", "not_found": "未找到",
                 "unknown": "不可用", "activating": "正在启动", "deactivating": "正在停止"}
        self.fill({
            "CPU": percentage(data.get("cpu_percent")),
            "内存（可用 / 总量）": (f"{size(memory['available_bytes'])} / {size(memory['total_bytes'])} · "
                                  f"{percentage(memory['used_percent'])}") if memory else None,
            "Swap": "未配置" if swap and swap["total_bytes"] == 0 else (
                f"{size(swap['total_bytes'])} · {percentage(swap['used_percent'])}" if swap else None),
            "根分区磁盘（已用 / 总量）": (f"{size(disk['used_bytes'])} / {size(disk['total_bytes'])} · "
                                        f"{percentage(disk_use)}") if disk else None,
            "负载（1 / 5 / 15 分钟）": " / ".join(f"{value:.2f}" for value in load) if load else None,
            "运行时间": f"{int(uptime) // 86400} 天 {int(uptime) % 86400 // 3600} 小时 {int(uptime) % 3600 // 60} 分" if uptime is not None else None,
            "x-ui.service": state.get(service.get("state"), service.get("state")),
            "Xray 进程": {"running": "运行中", "not_observed": "未观察到", "unknown": "不可用"}.get(xray),
            "操作系统": data.get("os_release"),
            "最近成功更新": local_stamp(stamp),
        })
        self.show_panel_versions(*self.panel_versions)


class ConnectionView(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.fields = {}
        for name, title in (("name", "VPS 名称"), ("location", "地理位置备注"), ("plan", "套餐备注"),
                            ("veid", "KiwiVM VEID"), ("api_key", "KiwiVM API Key"), ("ssh_host", "SSH IP 地址"),
                            ("username", "SSH 用户名"), ("password", "SSH 密码"), ("key_path", "私钥文件"),
                            ("passphrase", "私钥口令"), ("interface", "网卡（留空自动识别）"),
                            ("x_ui_version", "3X-UI 版本（手动确认）"), ("xray_version", "Xray 版本（手动确认）")):
            field = self.fields[name] = QLineEdit()
            field.setMaxLength(1024 if name == "key_path" else 512)
            if name in {"api_key", "password", "passphrase"}:
                field.setEchoMode(QLineEdit.EchoMode.Password)
                field.setPlaceholderText("留空保留已保存凭证")
            if name == "key_path":
                row = QHBoxLayout()
                row.addWidget(field)
                choose = QPushButton("浏览")
                choose.clicked.connect(self.choose_key)
                row.addWidget(choose)
                form.addRow(title, row)
            else:
                form.addRow(title, field)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.auth = QComboBox()
        self.auth.addItem("密码", "password")
        self.auth.addItem("私钥", "private_key")
        self.auto_api = QCheckBox("启动时轮询 KiwiVM")
        self.auto_ssh = QCheckBox("启动时采集 SSH（需已有已确认指纹）")
        self.live = QSpinBox()
        self.live.setRange(1, 1440)
        self.live.setSuffix(" 分钟")
        self.pin = plain("尚未信任主机")
        self.renewal = QLineEdit(placeholderText="YYYY-MM-DD，留空可清除")
        self.renewal.setMaxLength(32)
        self.save_renewal = QPushButton("保存续费时间")
        self.save_renewal.setToolTip("保存当前已配置 VPS 的续费时间，可在监控运行时使用。")
        self.renewal_status = plain()
        renewal_row = QHBoxLayout()
        renewal_row.addWidget(self.renewal, 1)
        renewal_row.addWidget(self.save_renewal)
        form.addRow("续费时间", renewal_row)
        form.addRow("", self.renewal_status)
        form.addRow("SSH 端口", self.port)
        form.addRow("SSH 认证方式", self.auth)
        form.addRow("已保存主机指纹", self.pin)
        form.addRow("KiwiVM 实时刷新", self.live)
        form.addRow("自动刷新", self.auto_api)
        form.addRow("", self.auto_ssh)
        buttons = QHBoxLayout()
        self.save = QPushButton("保存连接设置")
        self.test = QPushButton("测试 SSH 连接")
        self.forget = QPushButton("移除主机信任")
        self.remove_secret = QPushButton("移除当前凭证")
        for button in (self.save, self.test, self.forget, self.remove_secret):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(plain("先保存连接设置，再测试 SSH。服务监控使用固定的 x-ui.service。"))
        layout.addStretch()

    def choose_key(self):
        file, _ = QFileDialog.getOpenFileName(self, "选择本地 SSH 私钥")
        if file:
            self.fields["key_path"].setText(file)

    def load(self, profile):
        for name, field in self.fields.items():
            field.setText(getattr(profile, name, None) or "")
        self.port.setValue(profile.ssh_port)
        self.auth.setCurrentIndex(self.auth.findData(profile.auth_method))
        self.auto_api.setChecked(profile.auto_api)
        self.auto_ssh.setChecked(profile.auto_ssh)
        self.live.setValue(profile.live_minutes)

    def read(self, profile):
        changes = {name: field.text().strip() for name, field in self.fields.items()
                   if name not in {"api_key", "password", "passphrase"}}
        veid = changes["veid"]
        if veid and (not veid.isascii() or not veid.isdecimal() or not 1 <= len(veid.lstrip("0")) <= 20):
            raise ValueError("invalid_veid")
        changes["veid"] = veid.lstrip("0") or None
        changes["ssh_host"] = Endpoint(changes["ssh_host"], self.port.value()).host if changes["ssh_host"] else None
        changes["interface"] = changes["interface"] or None
        return replace(profile, **changes, ssh_port=self.port.value(), auth_method=self.auth.currentData(),
                       auto_api=self.auto_api.isChecked(), auto_ssh=self.auto_ssh.isChecked(), live_minutes=self.live.value())

    def renewal_value(self):
        value = self.renewal.text().strip()
        if value and date.fromisoformat(value).isoformat() != value:
            raise ValueError("invalid_renewal_date")
        return value
