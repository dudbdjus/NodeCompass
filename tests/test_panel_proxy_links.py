from types import SimpleNamespace

from proxy_manager.provider.panel_client import PanelClient, PanelRows
from proxy_manager.services.geoip import Location
from proxy_manager.services.panel_geo import Finding, Result
from proxy_manager.services.panel_proxy_links import PanelProxyLink
from proxy_manager.services.proxy_query import ProxyQuery
from proxy_manager.ui.pages.proxies import ProxiesPage
from proxy_manager.ui.panel_inventory import CITY_COLUMN, DATA_COLUMN, PanelInventory
from proxy_manager.ui.proxy_table_model import TAG_COLUMN, USAGE_COLUMN


def test_both_pages_share_location_and_usage_without_guessing_ambiguous_ip(qtbot):
    client = PanelClient("ISP-11-27.02.04", up=128, down=1024, record_id=11)
    other = PanelClient("ISP-12-27.04.02", up=0, down=0, record_id=12)
    direct = PanelClient("ISP-99-27.04.02", record_id=99)
    geo = SimpleNamespace(lookup=lambda ip: Location("ok", ip, country="美国", province="弗吉尼亚州", city="阿什本"))
    row = SimpleNamespace(address="8.8.8.8", region="原始备注", country="USA", tag="手工标签",
                          status="active", profile_id="profile", provider_id="p1", order_id=None, username=None,
                          port_http=None, port_socks=None, expires_on=None)
    repo = SimpleNamespace(list_proxies=lambda _: [row], save_annotation=lambda *_: None)
    isp = ProxiesPage(ProxyQuery(repo), geo=geo)
    panel = PanelInventory(geo=geo)
    qtbot.addWidget(isp)
    qtbot.addWidget(panel)
    panel.proxy_links_changed.connect(isp.update_panel_links)
    isp.reload("profile")
    panel.display(PanelRows((client, other, direct), 3), 1)
    users = {c.record_id: c.name for c in (client, other, direct)}
    panel.geo_result = Result("ok", {11: Finding("located", "::ffff:8.8.8.8"),
                                    12: Finding("located", "1.1.1.1"),
                                    99: Finding("default_direct")}, users)
    panel.repaint_geo()
    assert isp.model.index(0, TAG_COLUMN).data() == "ISP-11"
    assert isp.model.index(0, USAGE_COLUMN).data() == panel.table.item(0, DATA_COLUMN).text() == "1.12 KiB"
    assert [isp.model.index(0, i).data() for i in (2, 3, 4)] == ["美国", "弗吉尼亚州", "阿什本"]
    assert [panel.table.item(0, i).text() for i in (1, 2, CITY_COLUMN)] == ["美国", "弗吉尼亚州", "阿什本"]
    assert panel.table.item(2, CITY_COLUMN).text() == "默认直连"
    assert "3X-UI 面板当前计数" in panel.table.item(0, DATA_COLUMN).toolTip()
    isp.search.setText("ISP-11")
    assert isp.model.rowCount() == 1
    panel.geo_result.rows[12] = Finding("located", "8.8.8.8")
    panel.repaint_geo()
    isp.search.clear()
    assert isp.model.index(0, TAG_COLUMN).data() == "手工标签"
    assert isp.model.index(0, USAGE_COLUMN).data() == "—"
    assert row.tag == "手工标签" and row.region == "原始备注"


def test_natural_tag_and_numeric_usage_sorts_put_unknown_last(qtbot):
    def proxy(ident, address):
        return SimpleNamespace(address=address, region="", country="", tag="", status="active",
                               profile_id="profile", provider_id=ident, order_id=None, username=None,
                               port_http=None, port_socks=None, expires_on=None)

    rows = [proxy("unmatched", "1.1.1.1"), proxy("ten", "8.8.8.8"), proxy("two", "9.9.9.9")]
    repo = SimpleNamespace(list_proxies=lambda _: rows, save_annotation=lambda *_: None)
    page = ProxiesPage(ProxyQuery(repo))
    qtbot.addWidget(page)
    page.reload("profile")
    page.update_panel_links({
        "8.8.8.8": PanelProxyLink("ISP-10", "ISP-10", "1.00 MiB", "", 1048576),
        "9.9.9.9": PanelProxyLink("ISP-2", "ISP-2", "900.00 KiB", "", 921600),
    })
    ids = lambda: [row.provider_id for row in page.model.rows]
    assert ids() == ["two", "ten", "unmatched"]
    page.table.horizontalHeader().sectionClicked.emit(TAG_COLUMN)
    assert ids() == ["ten", "two", "unmatched"]
    page.table.horizontalHeader().sectionClicked.emit(USAGE_COLUMN)
    assert ids() == ["ten", "two", "unmatched"]
    page.table.horizontalHeader().sectionClicked.emit(USAGE_COLUMN)
    assert ids() == ["two", "ten", "unmatched"]


def test_relay_panel_link_retains_visible_usage_but_is_not_sortable():
    link = PanelProxyLink("中转VPS", "中转VPS", "9.00 GB", "", 9 * 2**30)
    assert link.sortable_usage is None
    assert link.usage == "9.00 GB"
