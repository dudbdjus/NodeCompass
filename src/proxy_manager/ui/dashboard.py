from time import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from proxy_manager.provider.kiwivm_client import KiwiVMEndpoint
from .cards import card, display_gb, text_label
from .vps_charts import TrafficChart, UsageRing
from .vps_views import billing_values, local_stamp, percentage, renewal_text


def count_text(value, formatting="{}"):
    return formatting.format(value) if value is not None else "—"


def network_rate(bytes_per_second):
    return bytes_per_second * 8 / 1_000_000 if bytes_per_second is not None else None


class Dashboard(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("dashboard")
        self.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 8, 2)
        layout.setSpacing(14)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metrics = {}
        for title, caption in (("CPU", "处理器使用率"), ("内存", "已用 / 总量"),
                               ("交换空间", "交换空间总量"), ("存储", "已用 / 总量")):
            frame, stack = card(title)
            value = text_label("—", "metric")
            detail = text_label(caption, "muted")
            stack.addWidget(value)
            stack.addWidget(detail)
            self.metrics[title] = (value, detail, caption)
            metrics.addWidget(frame, 1)
        layout.addLayout(metrics)
        self.system_stamp = text_label("", "muted")
        layout.addWidget(self.system_stamp)

        panels = QHBoxLayout()
        panels.setSpacing(12)
        speed, speed_layout = card("整体速度", "3X-UI · 面板整体收发速率")
        self.speed = text_label("", "reading")
        self.panel_status = text_label("", "muted")
        self.chart = TrafficChart("近 15 分钟 · Mbps", colors=("#2166d9", "#00859d"), grid=True,
                                  legend=("下载", "上传"))
        self.chart.setMinimumHeight(185)
        self._chart_points = None
        speed_layout.addWidget(self.speed)
        speed_layout.addWidget(self.chart, 1)
        speed_layout.addWidget(self.panel_status)
        panels.addWidget(speed, 2)

        billing, billing_layout = card("VPS 流量", "KiwiVM · 当前计费周期")
        self.used = text_label("—", "reading")
        self.billing_ring = UsageRing()
        self.billing_summary = text_label("", "muted")
        self.billing_stamp = text_label("", "muted")
        self.renewal = text_label(renewal_text(None), "reading")
        billing_layout.addWidget(self.billing_ring)
        billing_layout.addWidget(self.used)
        billing_layout.addWidget(self.billing_summary)
        billing_layout.addWidget(self.billing_stamp)
        billing_layout.addStretch()
        billing_layout.addWidget(self.renewal)
        panels.addWidget(billing, 1)
        layout.addLayout(panels)

        details = QHBoxLayout()
        details.setSpacing(12)
        sockets, sockets_layout = card("连接数", "3X-UI · 打开的套接字")
        self.socket_count = text_label("—", "metric")
        sockets_layout.addWidget(self.socket_count)
        self.socket_details = text_label("TCP — · UDP —", "muted")
        sockets_layout.addWidget(self.socket_details)
        sockets_layout.addStretch()
        details.addWidget(sockets, 1)
        services, service_layout = card("面板与 Xray")
        self.x_ui = text_label("", "reading")
        self.xray = text_label("", "reading")
        self.system_details = QPushButton("查看客户端")
        service_layout.addWidget(self.x_ui)
        service_layout.addWidget(self.xray)
        service_layout.addStretch()
        service_layout.addWidget(self.system_details, alignment=Qt.AlignmentFlag.AlignLeft)
        details.addWidget(services, 2)
        layout.addLayout(details)
        layout.addStretch()

    def show_panel(self, monitor):
        self.panel_status.setText(monitor.message)
        self.system_stamp.setText(f"3X-UI 面板 · 最近成功更新 {local_stamp(monitor.stamp)}")
        status = monitor.status
        if status:
            capacities = (status.memory, status.swap, status.disk)
            readings = [(status.cpu, None)] + [
                (item.percent, f"{display_gb(item.used)} / {display_gb(item.total)}" if item.total is not None else None)
                for item in capacities
            ]
            down = count_text(network_rate(status.download), "{:.2f}")
            up = count_text(network_rate(status.upload), "{:.2f}")
            self.speed.setText(f"下载  {down} Mbps     上传  {up} Mbps")
            self.socket_count.setText(count_text(status.sockets))
            self.socket_details.setText(f"TCP {count_text(status.tcp)} · UDP {count_text(status.udp)}")
            self.x_ui.setText(f"3X-UI  {status.panel_version or '—'}")
            xray_state = {"running": "运行中", "stop": "已停止", "error": "错误"}.get(status.xray_state, "—")
            self.xray.setText(f"Xray  {status.xray_version or '—'}  ·  {xray_state}")
        else:
            readings = [(None, None)] * len(self.metrics)
            self.speed.setText("下载  — Mbps     上传  — Mbps")
            self.socket_count.setText("—")
            self.socket_details.setText("TCP — · UDP —")
            self.x_ui.setText("3X-UI  —")
            self.xray.setText("Xray  —")
        for (value, detail, caption), (reading, description) in zip(self.metrics.values(), readings):
            value.setText(percentage(reading))
            detail.setText(description or caption)
        if self._chart_points is not monitor.points:
            self._chart_points = monitor.points
            now = time()
            self.chart.update_series(monitor.points, 12, bounds=(now - 900, now + 1))

    def show_billing(self, panel):
        service_stamp, service = panel.latest_kiwi.get(KiwiVMEndpoint.SERVICE_INFO, (None, {}))
        billing = service.get("billing") or {}
        if not any(value is not None for value in billing.values()):
            service_stamp, live = panel.latest_kiwi.get(KiwiVMEndpoint.LIVE_SERVICE_INFO, (None, {}))
            billing = live.get("service", {}).get("billing") or {}
        _, _, _, usage = billing_values(billing)
        used = billing.get("raw_used_bytes")
        quota = billing.get("raw_quota_bytes")
        remaining = max(0, quota - used) if quota is not None and used is not None else None
        self.used.setText(f"已用 {display_gb(used)}")
        self.billing_ring.show_usage(usage)
        self.billing_summary.setText(
            f"剩余 {display_gb(remaining)} · 额度 {display_gb(quota)}"
            if billing else "暂无计费数据"
        )
        self.billing_stamp.setText(
            f"下次重置  {local_stamp(billing.get('reset_timestamp'))}\n"
            f"最近更新  {local_stamp(service_stamp)}\nGB 显示与 KiwiVM 口径一致（1 GB = 2³⁰ 字节）"
        )
        self.renewal.setText(renewal_text(panel.renewal_date))
