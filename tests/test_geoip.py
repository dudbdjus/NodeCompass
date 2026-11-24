from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from proxy_manager.services.geoip import GeoIP, Location, parse_location, proxy_location_fields
from proxy_manager.services.proxy_query import ProxyQuery
from proxy_manager.ui.pages.geo_settings import GeoSettings
from proxy_manager.ui.pages.proxies import ProxiesPage


def test_partial_fields_retained():
    loc = parse_location({"subdivisions": [{"names": {"en": "California"}}],
                          "city": {"names": {"zh-CN": "示例市", "en": "Example"}}}, "8.8.8.8", "test")
    assert (loc.country, loc.province, loc.city, loc.status) == ("", "加利福尼亚州", "示例市", "ok")
    assert parse_location(None, "8.8.8.8", "test").status == "no_record"


def test_chinese_place_names_prefer_database_and_never_fall_back_to_english():
    loc = parse_location({
        "country": {"iso_code": "US"},
        "subdivisions": [{"names": {"en": "Washington"}}],
        "city": {"names": {"zh-CN": "Seattle", "en": "Seattle (Queen Anne)"}},
    }, "8.8.8.8", "test")
    assert loc.label == "美国 · 华盛顿州 · 西雅图（安妮女王区）"
    loc = parse_location({
        "subdivisions": [{"names": {"en": "Unknown Province"}}, {"names": {"en": "São Paulo"}}],
        "city": {"names": {"zh_Hant": "聖保羅", "en": "São Paulo"}},
    }, "8.8.8.8", "test")
    assert loc.region == "圣保罗 · 聖保羅"
    loc = parse_location({"city": {"names": {"en": "Unknown City"}}}, "8.8.8.8", "test")
    assert loc.city == "城市暂无中文名"


def test_isp_display_filter_and_edit_role(qtbot):
    row = SimpleNamespace(address="8.8.8.8", region="自定义地区", country="GB", tag="", status="active",
                          profile_id="test", provider_id="1", order_id="", username="", expires_on=None,
                          port_http=None, port_socks=None)
    geo = SimpleNamespace(lookup=lambda _: Location("ok", country="美国", province="California", city="Example"))
    repo = SimpleNamespace(save_annotation=lambda *args: pytest.fail("unexpected write"),
                           list_proxies=lambda _: [row])
    page = ProxiesPage(ProxyQuery(repo), geo=geo)
    qtbot.addWidget(page)
    page.reload("test")
    page.country.setCurrentIndex(page.country.findData("美国"))
    assert page.model.rowCount() == 1
    assert page.model.index(0, 2).data() == "美国"
    assert page.model.index(0, 4).data(Qt.ItemDataRole.EditRole) == "自定义地区"
    row.region = ""
    assert page.model.index(0, 3).data() == "California"
    assert page.model.index(0, 4).data() == "Example"
    assert page.model.index(0, 4).data(Qt.ItemDataRole.EditRole) == ""
    assert row.country == "GB"
    assert proxy_location_fields(row, None) == ("英国", "", "")


def test_source_and_license_visible(qtbot, tmp_path):
    page = GeoSettings(GeoIP(tmp_path, source_directory=tmp_path))
    qtbot.addWidget(page)
    text = " ".join(w.text() for w in page.findChildren(type(page.info)))
    assert "IP2Location LITE" in text and "https://www.ip2location.com" in text
    assert "本地离线查询" in text and "未找到数据库" in text
    assert "Ping0" not in text and "MaxMind" not in text and "DB-IP" not in text
