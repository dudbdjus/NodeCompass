from types import SimpleNamespace

from proxy_manager.monitoring.ssh_client import Endpoint, HostIdentity
from proxy_manager.provider.panel_client import PanelClient, PanelCredentials, PanelRows
from proxy_manager.services.geoip import Location
from proxy_manager.services.panel_geo import Finding, Result
from proxy_manager.ui import panel_inventory as ui


def test_region_refresh_retries_then_updates_with_client_roster(qtbot, monkeypatch):
    now = [1000]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    monkeypatch.setattr(ui, "time", lambda: now[0])
    responses = iter((
        Result("unavailable", diagnostic="snapshot_unavailable"),
        Result("ok", {1: Finding("located", "8.8.8.8")}, {1: "client-1"}),
        Result("ok", {2: Finding("located", "8.8.8.8")}, {2: "client-2"}),
    ))
    monkeypatch.setattr(ui, "inspect_panel", lambda *args: next(responses))
    endpoint = Endpoint("192.0.2.1")
    identity = HostIdentity(endpoint, "fixture", "fixture")

    class ImmediateWorker:
        def __init__(self, read):
            self.read = read

        def start(self):
            self.result = self.read()

        def is_alive(self):
            return False

    monkeypatch.setattr(ui, "Worker", ImmediateWorker)
    panel = ui.PanelInventory(geo=SimpleNamespace(lookup=lambda _: Location("ok", city="山景城")),
                              ssh_context=lambda: (SimpleNamespace(endpoint=endpoint), identity))
    qtbot.addWidget(panel)
    panel.show()
    panel.geo_timer.stop()
    panel.credentials = PanelCredentials("https://example.test", "fixture-token")
    panel.display(PanelRows((PanelClient("client-1"),), 1), now[0])
    panel.geo_tick()
    panel.geo_tick()
    assert panel.geo_retry_at == now[0] + ui.GEO_RETRY_INTERVAL
    assert panel.geo_result is None
    assert "无法核对 Xray" in panel.table.item(0, ui.CITY_COLUMN).text()
    now[0] += ui.GEO_RETRY_INTERVAL
    panel.geo_tick()
    panel.geo_tick()
    assert panel.table.item(0, ui.CITY_COLUMN).text() == "山景城"
    assert panel.data.rows[0].record_id == 1
    panel.display(PanelRows((PanelClient("client-1"),), 1), now[0])
    now[0] += 86401
    panel.geo_tick()
    assert panel.geo_worker is None  # No daily reload of an unchanged roster.
    assert panel.geo_result.users == {1: "client-1"} and panel.data.rows[0].record_id == 1
    panel.display(PanelRows((PanelClient("client-2", record_id=2),), 1), now[0])
    assert panel.geo_result is None and panel.geo_retry_at == 0
    panel.geo_tick()
    panel.geo_tick()
    assert panel.geo_result.users == {2: "client-2"}


def test_manual_client_refresh_waits_for_list_then_verifies_regions(qtbot, monkeypatch):
    panel = ui.PanelInventory(geo=object())
    qtbot.addWidget(panel)
    panel.show()
    panel.geo_timer.stop()
    panel.credentials = PanelCredentials("https://example.test", "fixture-token")
    started = []
    monkeypatch.setattr(panel, "load", lambda: started.append(True))
    panel.geo_retry_at = ui.monotonic() + 999
    panel.update_buttons()
    panel.refresh.click()
    assert started == [True] and panel.geo_retry_at == 0
    assert panel.geo_wait_for_clients
    assert "正在刷新客户端与配置代理地区" in panel.geo_message
    panel.geo_tick()
    assert panel.geo_worker is None


def test_unchanged_new_client_is_rechecked_until_route_becomes_available(qtbot, monkeypatch):
    now = [1000]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    name = "ISP-23-26.12.25"
    responses = iter((
        Result("ok", {23: Finding("default_direct")}, {23: name}),
        Result("ok", {23: Finding("located", "8.8.4.23", True, "proxy-23", live_only=True)}, {23: name}),
    ))
    monkeypatch.setattr(ui, "inspect_panel", lambda *_: next(responses))

    class ImmediateWorker:
        def __init__(self, read):
            self.read = read

        def start(self):
            self.result = self.read()

        def is_alive(self):
            return False

    monkeypatch.setattr(ui, "Worker", ImmediateWorker)
    panel = ui.PanelInventory(geo=SimpleNamespace(lookup=lambda _: Location("ok", city="纽约市")),
                              ssh_context=lambda: (None, None))
    received = []
    panel.proxy_links_changed.connect(received.append)
    qtbot.addWidget(panel)
    panel.show()
    panel.geo_timer.stop()
    panel.credentials = PanelCredentials("https://example.test", "fixture-token")
    panel.display(PanelRows((PanelClient(name, record_id=23),), 1), now[0])
    panel.geo_tick()
    panel.geo_tick()
    assert panel.geo_pending and not received[-1]
    now[0] += ui.GEO_RETRY_INTERVAL - 1
    panel.geo_tick()
    assert panel.geo_worker is None
    now[0] += 1
    panel.geo_tick()
    panel.geo_tick()
    assert not panel.geo_pending
    assert panel.table.item(0, ui.CITY_COLUMN).text() == "纽约市"
    assert received[-1]["8.8.4.23"].tag == "ISP-23"
    assert "运行中路由" in received[-1]["8.8.4.23"].verification_note


def test_recheck_and_transient_failures_keep_known_geo_until_conflicting_result(qtbot, monkeypatch):
    now = [1000]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    data = PanelRows((PanelClient("ISP-10-old", up=10, down=20, record_id=10),), 1)
    confirmed = Result("ok", {10: Finding("located", "8.8.8.8")}, {10: "ISP-10-old"})
    results = iter((Result("unavailable", diagnostic="timeout"), Result("changed")))
    monkeypatch.setattr(ui, "inspect_panel", lambda *_: next(results))

    class ImmediateWorker:
        def __init__(self, job):
            self.job = job

        def start(self):
            self.result = self.job()

        def is_alive(self):
            return False

    monkeypatch.setattr(ui, "Worker", ImmediateWorker)
    panel = ui.PanelInventory(geo=SimpleNamespace(lookup=lambda _: Location("ok", city="山景城")),
                              ssh_context=lambda: (None, None))
    qtbot.addWidget(panel)
    received = []
    panel.proxy_links_changed.connect(received.append)
    panel.show()
    panel.geo_timer.stop()
    panel.credentials = PanelCredentials("https://example.test", "fixture-token")
    panel.display(data, now[0])
    panel.geo_result, panel.geo_pending, panel.geo_message = confirmed, False, ""
    panel.repaint_geo()
    monkeypatch.setattr(panel, "load", lambda: None)
    panel.refresh.click()
    assert panel.geo_result is confirmed
    assert received[-1]["8.8.8.8"].tag == "ISP-10"
    assert "上次核对" in panel.geo_status.text()
    panel.finish_clients(SimpleNamespace(failed=False, result=(data, now[0])))
    panel.geo_tick()
    panel.geo_tick()
    assert panel.table.item(0, ui.CITY_COLUMN).text() == "山景城"
    assert "超时" in received[-1]["8.8.8.8"].verification_note
    assert received[-1]["8.8.8.8"].usage_bytes == 30
    now[0] += ui.GEO_RETRY_INTERVAL
    panel.geo_tick()
    panel.geo_tick()
    assert panel.geo_result is None and received[-1] == {}
    assert "配置变化" in panel.table.item(0, ui.CITY_COLUMN).text()
