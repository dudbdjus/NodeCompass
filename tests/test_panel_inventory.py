from unittest.mock import Mock

from proxy_manager.provider.panel_client import PanelClient, PanelCredentials, PanelError, PanelRows
from proxy_manager.ui import panel_inventory
from proxy_manager.ui.panel_inventory import CITY_COLUMN, DATA_COLUMN, GROUP_COLUMN, ONLINE_COLUMN, SPEED_COLUMN, PanelInventory


def test_client_list_refresh_paginates_and_clears_stale_online_state(qtbot, monkeypatch):
    first = PanelClient("fixture-08", "group", True, 10, 20, "fake-sub")
    second = PanelClient("second-08", online=False)
    fetch = Mock(side_effect=[PanelRows((first, second), 2, page_size=1), PanelError("timeout")])
    monkeypatch.setattr(panel_inventory, "fetch_all_clients", fetch)
    inventory = PanelInventory()
    qtbot.addWidget(inventory)
    inventory.set_credentials(PanelCredentials("https://fixture.test/panel", "fixture"))
    inventory.show()
    qtbot.waitUntil(lambda: inventory.data is not None)
    table = inventory.table
    assert [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())] == [
        "客户端", "国家", "州/省", "城市", "分组", "在线", "数据", "当前速度",
    ]
    assert table.item(0, ONLINE_COLUMN).text() == "在线"
    assert table.item(0, DATA_COLUMN).text() == "30 B"
    assert "上传 10 B" in table.item(0, DATA_COLUMN).toolTip()
    assert "下载 20 B" in table.item(0, DATA_COLUMN).toolTip()
    assert all(table.item(0, i).foreground().color().name() == "#00632d" for i in range(table.columnCount()))
    inventory.next.click()
    assert inventory.page == 2
    assert table.item(0, ONLINE_COLUMN).text() == "离线"
    inventory.refresh.click()
    qtbot.waitUntil(lambda: inventory.job is None)
    assert table.item(0, ONLINE_COLUMN).text() == "—"
    assert "已保留" in inventory.status.text()
    assert inventory.client_timer.isActive()
    inventory.hide()
    assert not inventory.client_timer.isActive()


def test_client_rates_and_qr_lock_to_the_selected_node(qtbot, monkeypatch):
    inventory = PanelInventory()
    qtbot.addWidget(inventory)
    inventory.set_credentials(PanelCredentials("https://fixture.test/panel", "fixture"))
    fetch_qr = Mock(return_value="https://subscription.test/example/fake-sub")
    monkeypatch.setattr(panel_inventory, "fetch_subscription_url", fetch_qr)

    def display(up, down, stamp):
        inventory.display(PanelRows((PanelClient("fixture-08", up=up, down=down, sub_id="fake-sub"),), 1),
                          stamp)
        return inventory.table.item(0, SPEED_COLUMN).text()

    assert display(0, 0, 5) == "—"
    assert display(1_250_000, 2_500_000, 10) == "↑ 2.00 / ↓ 4.00 Mbps"
    assert display(0, 2_500_000, 15) == "—"
    assert display(1000, 2_500_000, 40) == "—"
    inventory.open_subscription(0, 2)
    assert inventory.dialog is None
    inventory.table.cellDoubleClicked.emit(0, 0)
    qtbot.waitUntil(lambda: inventory.job is None)
    fetch_qr.assert_called_once_with(inventory.credentials, "fake-sub")
    assert inventory.dialog.windowTitle() == "订阅二维码"
    assert not inventory.qr_image.pixmap().isNull()
    inventory.dialog.close()
    inventory.set_credentials(None)
    assert inventory.table.rowCount() == 0
    assert inventory.shutdown()



def test_client_sorting_across_pages_and_comment_column(qtbot):
    inventory = PanelInventory()
    qtbot.addWidget(inventory)
    rows = (
        PanelClient("ISP-10", "Zulu", False, 2, 5, comment="华南备注"),
        PanelClient("ISP-2", "Bravo", True, 500, 1000, comment="东京备注"),
        PanelClient("ISP-4", "Zulu", False),
        PanelClient("ISP-1", "Echo", False, 10, 900, comment="首尔备注"),
        PanelClient("ISP-3", "Alpha", True, 0, 0),
    )
    inventory.display(PanelRows(rows, len(rows), page_size=2), 5)
    table = inventory.table

    def visible():
        return [table.item(i, 0).text() for i in range(table.rowCount())]

    assert visible() == ["ISP-1", "ISP-2"]
    assert table.item(1, CITY_COLUMN).text() == "东京备注"
    assert all(table.item(1, column).font().bold() for column in range(table.columnCount()))
    assert table.item(1, CITY_COLUMN).background().color().name() == "#ddf5e5"
    inventory.change_page(1)
    assert visible() == ["ISP-3", "ISP-4"]
    table.horizontalHeader().sectionClicked.emit(GROUP_COLUMN)
    assert visible() == ["ISP-3", "ISP-2"]
    inventory.change_page(1)
    assert visible() == ["ISP-1", "ISP-4"]
    table.horizontalHeader().sectionClicked.emit(ONLINE_COLUMN)
    assert visible() == ["ISP-2", "ISP-3"]
    table.horizontalHeader().sectionClicked.emit(DATA_COLUMN)
    assert visible() == ["ISP-2", "ISP-1"]
    table.horizontalHeader().sectionClicked.emit(0)
    assert visible() == ["ISP-1", "ISP-2"]


def test_panel_traffic_order_keeps_relay_last_and_does_not_accumulate_after_reset(qtbot):
    panel = PanelInventory()
    qtbot.addWidget(panel)
    def rows(up):
        return PanelRows((PanelClient("中转VPS", up=1000, down=1000),
                          PanelClient("ISP-1", up=up, down=0),
                          PanelClient("ISP-2", up=10, down=15)), 3)
    panel.display(rows(100), 1)
    panel.sort_by(DATA_COLUMN)
    assert [row.name for row in panel.visible_rows] == ["ISP-1", "ISP-2", "中转VPS"]
    assert panel.table.item(2, DATA_COLUMN).text() == "1.95 KiB"
    panel.display(rows(0), 6)
    assert [row.name for row in panel.visible_rows] == ["ISP-2", "ISP-1", "中转VPS"]
    assert panel.table.item(1, DATA_COLUMN).text() == "0 B"
