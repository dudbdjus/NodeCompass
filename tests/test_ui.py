from unittest.mock import Mock

from proxy_manager.security.credentials import CredentialStore, StoredCredential
from proxy_manager.services.sync import SyncResult, SyncService
from proxy_manager.storage.models import Proxy
from proxy_manager.storage.repositories import Repository
from proxy_manager.ui.main_window import MainWindow


def test_sidebar_dashboard_keeps_panel_and_vps_sources_separate(qtbot, database, monkeypatch):
    from datetime import UTC, datetime
    from time import time

    from proxy_manager.monitoring.network import Bandwidth
    from proxy_manager.monitoring.scheduler import Sample
    from proxy_manager.monitoring.system import Disk, Memory, Service, Swap, SystemMetrics
    from proxy_manager.provider.kiwivm_client import KiwiVMEndpoint, Snapshot
    from proxy_manager.provider.kiwivm_parser import parse_service_info
    from proxy_manager.provider.panel_client import PanelError, PanelRows, parse_status
    from proxy_manager.storage.vps_repository import VpsProfile, VpsRepository
    from proxy_manager.ui import panel_inventory, panel_monitor

    store = Mock()
    store.load.return_value = None
    monkeypatch.setattr(panel_monitor, "PanelCredentialStore", lambda: store)
    monkeypatch.setattr(panel_inventory, "fetch_all_clients", lambda *_: PanelRows((), 0))
    fetch = Mock(side_effect=[
        parse_status({"success": True, "obj": {
            "cpu": 64, "netIO": {"up": 250000, "down": 500000},
            "tcpCount": 33, "udpCount": 7, "panelVersion": "3.6.0",
            "mem": {"current": 2**29, "total": 2**30},
            "xray": {"state": "running", "version": "v26.7.28"},
        }}),
        PanelError("timeout"),
    ])
    monkeypatch.setattr(panel_monitor, "fetch_status", fetch)
    repo = VpsRepository(database)
    profile = repo.prepare(VpsProfile(veid="123", username="viewer"))
    repo.save(profile)
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    window = MainWindow(Repository(database), credentials, Mock(spec=SyncService))
    qtbot.addWidget(window)
    window.show()
    buttons = window.workspace.navigation
    dashboard = window.dashboard
    assert [buttons.button(i).text() for i in range(6)] == [
        "总览", "Proxyseller", "3X-UI", "搬瓦工VPS", "历史记录", "系统设置",
    ]
    assert buttons.checkedId() == 0
    assert dashboard.metrics["CPU"][0].text() == "—"
    assert dashboard.billing_ring.usage is None
    assert dashboard.socket_count.text() == "—"

    stamp = time() - 15
    service = parse_service_info({"error": 0, "plan_monthly_data": 8 * 2**30,
                                  "data_counter": 2 * 2**30, "monthly_data_multiplier": 2})
    repo.save_kiwi(profile.id, KiwiVMEndpoint.SERVICE_INFO, Snapshot(datetime.fromtimestamp(stamp, UTC), service))
    system = SystemMetrics(22, Memory(2**30, 2**29, 50), Swap(0, None), Disk(2**30, 2**29),
                           (1, 2, 3), 3601, Service("failed"), "running", os_release="Debian GNU/Linux 12 (bookworm)")
    repo.save_samples(profile.id, [Sample(0, Bandwidth("ok", "eth0", 9, 8), system, captured=stamp)])
    window.vps.refresh_data()
    assert window.vps.system.labels["CPU"].text() == "22.0%"
    assert window.vps.system.labels["操作系统"].text() == "Debian GNU/Linux 12 (bookworm)"
    assert dashboard.metrics["CPU"][0].text() == "—"
    assert dashboard.chart.rx == []
    assert dashboard.used.text() == "已用 2.00 GB"
    assert dashboard.billing_ring.usage == 25

    buttons.button(5).click()
    assert window.tabs.currentWidget() is window.settings
    assert [window.settings.tabText(i) for i in range(3)] == ["VPS 连接", "Proxyseller 连接", "3X-UI 面板连接"]
    window.settings.setCurrentWidget(window.panel.settings)
    window.panel.address.setText("https://panel.example.test/private-prefix/")
    window.panel.token.setText("fixture-token")
    window.panel.save_button.click()
    qtbot.waitUntil(lambda: window.panel.stamp is not None)
    assert window.panel.token.text() == ""
    assert dashboard.metrics["CPU"][0].text() == "64.0%"
    assert dashboard.metrics["内存"][0].text() == "50.0%"
    assert dashboard.socket_count.text() == "40"
    assert "4.00 Mbps" in dashboard.speed.text() and "2.00 Mbps" in dashboard.speed.text()
    assert dashboard.chart.rx[0][0][1] == 4
    assert "运行中" in dashboard.xray.text()
    assert window.vps.system.labels["3X-UI 版本"].text() == "3.6.0（3X-UI 面板）"
    assert window.vps.overview.xray_version.text() == "Xray 版本：v26.7.28"
    window.vps.render_system()
    assert window.vps.system.labels["Xray 版本"].text() == "v26.7.28（3X-UI 面板）"
    window.panel.start()
    qtbot.waitUntil(lambda: window.panel.worker is None)
    assert len(window.panel.points) == 2 and window.panel.points[-1].rx is None
    assert "已保留上次成功数据" in dashboard.panel_status.text()
    assert dashboard.socket_count.text() == "40"
    assert fetch.call_count == 2

    buttons.button(1).click()
    assert window.tabs.currentWidget() is window.proxies
    buttons.button(2).click()
    assert window.tabs.currentWidget() is window.panel
    assert window.panel.inventory.table.horizontalHeaderItem(panel_inventory.ONLINE_COLUMN).text() == "在线"
    buttons.button(3).click()
    assert buttons.checkedId() == 3
    assert window.tabs.currentWidget() is window.vps
    window.panel.remove()
    assert window.vps.system.labels["3X-UI 版本"].text() == "—"
    assert dashboard.chart.rx == []
    assert dashboard.socket_count.text() == "—"


def test_vps_renewal_date_saves_reopens_and_updates_both_displays(qtbot, database):
    from proxy_manager.storage.vps_repository import VpsProfile, VpsRepository

    profiles = VpsRepository(database)
    profiles.save(profiles.prepare(VpsProfile(veid="123")))
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    repository = Repository(database)
    service = Mock(spec=SyncService)
    window = MainWindow(repository, credentials, service)
    qtbot.addWidget(window)
    settings = window.vps.settings
    assert window.dashboard.renewal.text() == "续费时间：未填写"
    settings.renewal.setText("2028-02-29")
    settings.save.click()
    assert window.dashboard.renewal.text() == window.vps.overview.renewal.text() == "续费时间：2028-02-29"
    settings.renewal.setText("2027-02-29")
    settings.save_renewal.click()
    assert "有效日期" in settings.renewal_status.text()
    assert window.dashboard.renewal.text() == "续费时间：2028-02-29"
    assert window.close()

    reopened = MainWindow(repository, credentials, service)
    qtbot.addWidget(reopened)
    assert reopened.vps.settings.renewal.text() == "2028-02-29"
    assert reopened.dashboard.renewal.text() == reopened.vps.overview.renewal.text() == "续费时间：2028-02-29"
    reopened.vps.settings.renewal.clear()
    reopened.vps.settings.save_renewal.click()
    assert reopened.dashboard.renewal.text() == reopened.vps.overview.renewal.text() == "续费时间：未填写"


def test_proxy_list_filters_and_sync_runs_in_worker(qtbot, database):
    profile = StoredCredential("fixture", "fake")
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = profile
    sync = Mock(spec=SyncService)
    sync.run.return_value = SyncResult("success", 2)
    with database.sessions.begin() as session:
        session.add_all([
            Proxy(profile_id="fixture", provider_id="1", address="192.0.2.1", country="USA"),
            Proxy(profile_id="fixture", provider_id="2", address="192.0.2.2", country="DEU"),
        ])
    window = MainWindow(Repository(database), credentials, sync)
    qtbot.addWidget(window)
    window.tabs.setCurrentWidget(window.proxies)
    window.show()
    footer_gap = window.proxies.height() - window.overview.geometry().bottom() - 1
    assert footer_gap == window.proxies.layout().contentsMargins().bottom()
    assert window.proxies.table.height() > window.proxies.height() * 0.75
    assert window.proxies.model.rowCount() == 2
    window.proxies.search.setText("192.0.2.2")
    assert window.proxies.model.rowCount() == 1
    window.proxies.table.selectRow(0)
    assert window.proxies.selected_proxy().provider_id == "2"
    assert window.proxies.detail_button.isEnabled()
    window.proxies.sync_button.click()
    qtbot.waitUntil(lambda: window.worker is None)
    assert "同步完成" in window.statusBar().currentMessage()
    assert sync.run.call_args.args[0] == profile

def test_settings_and_history_use_only_redacted_metadata(qtbot, database, monkeypatch):
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    credentials.save.return_value = StoredCredential("fixture", "fake-credential")
    repo = Repository(database)
    window = MainWindow(repo, credentials, Mock(spec=SyncService))
    qtbot.addWidget(window)
    window.show()
    assert not window.proxies.sync_button.isEnabled()
    window.workspace.navigation.button(5).click()
    window.settings.setCurrentWidget(window.isp_settings)
    assert window.tabs.currentWidget() is window.settings
    window.isp_settings.key_input.setText("fake-credential")
    window.isp_settings.save_key.click()
    credentials.save.assert_called_once_with("fake-credential")
    assert window.isp_settings.key_input.text() == ""
    assert window.proxies.sync_button.isEnabled()
    assert window.history.operation_table.rowCount() == 1
    assert "fake-credential" not in window.statusBar().currentMessage()

    monkeypatch.setattr(window, "confirm_key_action", lambda *_: True)
    window.isp_settings.delete_key.click()
    credentials.delete.assert_called_once()
    assert not window.proxies.sync_button.isEnabled()
    assert [operation.action for operation in repo.operations()] == ["key_delete", "key_save"]

def test_ui_workflow_with_mock_supplier_and_export(qtbot, database, respx_mock, monkeypatch, tmp_path):
    from uuid import uuid4

    import httpx
    from PySide6.QtWidgets import QFileDialog

    key = "fixture-ui-end-to-end"
    store = CredentialStore(f"ProxyManager/test/{uuid4().hex}")
    repo = Repository(database)
    route = respx_mock.get(
        f"https://proxy-seller.com/personal/api/v2/{key}/proxy/list/isp?page=1&per_page=100",
    ).mock(return_value=httpx.Response(200, json={
        "status": "success", "errors": [],
        "data": {
            "items": [{
                "id": "42", "ip_only": "192.0.2.42", "port_http": 8080,
                "login": "fixture-user", "password": "fixture-password", "country_alpha3": "USA",
            }],
            "page": 1, "per_page": 100, "total": 1, "total_pages": 1,
        },
    }))
    export_path = tmp_path / "filtered.txt"
    monkeypatch.setattr(QFileDialog, "exec", lambda self: 1)
    monkeypatch.setattr(QFileDialog, "selectedFiles", lambda self: [str(export_path)])
    try:
        window = MainWindow(repo, store, SyncService(database))
        qtbot.addWidget(window)
        window.show()
        window.workspace.navigation.button(5).click()
        window.settings.setCurrentWidget(window.isp_settings)
        window.isp_settings.key_input.setText(key)
        window.isp_settings.save_key.click()
        window.tabs.setCurrentWidget(window.proxies)
        window.proxies.sync_button.click()
        qtbot.waitUntil(lambda: window.worker is None)
        assert route.call_count == 1
        assert window.overview.total.text() == "1"
        assert window.history.sync_table.rowCount() == 1

        window.proxies.search.setText("usa")
        window.proxies.table.selectRow(0)
        assert window.proxies.selected_proxy().provider_id == "42"
        window.proxies.export_button.click()
        qtbot.waitUntil(lambda: window.worker is None)
        assert export_path.read_text(encoding="utf-8") == "http://192.0.2.42:8080\n"
        assert [row.action for row in repo.operations()] == ["export", "sync", "key_save"]
    finally:
        store.delete()

def test_country_status_expiry_and_inline_annotations(qtbot, database, monkeypatch):
    from datetime import date

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QLineEdit

    from proxy_manager.ui.proxy_table_model import EXPIRY_COLUMN, HEADERS
    from proxy_manager.services.geoip import Location

    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = StoredCredential("fixture", "fake")
    repo = Repository(database)
    with database.sessions.begin() as session:
        session.add_all([
            Proxy(profile_id="fixture", provider_id="a", address="192.0.2.1",
                  country="USA", status="Active", expires_on=date(2027, 1, 2)),
            Proxy(profile_id="fixture", provider_id="b", address="192.0.2.2",
                  country="DEU", status="system _error", expires_on=date(2026, 12, 10)),
            Proxy(profile_id="fixture", provider_id="c", address="192.0.2.3"),
        ])
    window = MainWindow(repo, credentials, Mock(spec=SyncService))
    qtbot.addWidget(window)
    monkeypatch.setattr(window.geo, "lookup", lambda ip: Location(
        "ok", ip, country={"192.0.2.1": "美国", "192.0.2.2": "德国"}.get(ip, "")))
    window.reload()
    window.tabs.setCurrentWidget(window.proxies)
    window.show()
    page = window.proxies
    model = page.model
    assert HEADERS == ("IP 地址", "标签", "国家", "州/省", "城市", "状态", "到期日", "本期已用流量")
    assert model.index(0, 1).data() == "—" and model.index(0, 7).data() == "—"
    assert model.index(0, 2).data() == "美国"
    assert model.index(1, 2).data() == "德国"
    assert model.index(0, 5).data() == "活跃"
    assert model.index(1, 5).data() == "系统错误"
    assert model.index(0, 3).data() == "—" and model.index(0, 4).data() == "—"

    page.table.selectRow(0)
    header = page.table.horizontalHeader()
    point = QPoint(header.sectionViewportPosition(EXPIRY_COLUMN) + 12, header.height() // 2)
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert [row.provider_id for row in model.rows] == ["b", "a", "c"]
    assert page.selected_proxy().provider_id == "a"
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert [row.provider_id for row in model.rows] == ["a", "b", "c"]

    index = model.index(0, 4)
    page.table.setCurrentIndex(index)
    page.table.edit(index)
    editor = page.table.findChild(QLineEdit)
    assert editor is not None
    editor.setText("加州")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: model.index(0, 4).data(Qt.ItemDataRole.EditRole) == "加州")
    assert model.setData(model.index(0, 1), "本地标签")
    window.reload()
    assert model.index(0, 4).data(Qt.ItemDataRole.EditRole) == "加州"
    assert model.index(0, 4).data() == "—"  # The selected source never falls back to the annotation.
    assert model.index(0, 1).data() == "本地标签"
    page.search.setText("美国")
    assert [row.provider_id for row in model.rows] == ["a"]
    page.search.setText("本地标签")
    assert [row.provider_id for row in model.rows] == ["a"]
    page.search.clear()
    page.status.setCurrentIndex(page.status.findText("系统错误"))
    assert [row.provider_id for row in model.rows] == ["b"]
