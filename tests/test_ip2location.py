import struct

import pytest

from proxy_manager.services.geoip import GeoIP, IP2_OFFLINE
from proxy_manager.ui.pages.geo_settings import GeoSettings


def db3(file, city, *, edition=3, ipv6=False):
    # Small valid header and contiguous ranges followed by the referenced text.
    count = 1
    ipv6_addr = 65 if ipv6 else 0
    strings = bytearray()
    string_start = 120 if ipv6 else 64

    def add(raw):
        pos = string_start + len(strings)
        strings.extend(bytes([len(raw)]) + raw)
        return pos

    country = add(b"US")
    add(b"United States")
    region = add(b"California")
    place = add(city)
    header = struct.pack("<5B6I3B", edition, 4, 26, 11, 15, count, 33,
                         count if ipv6 else 0, ipv6_addr, 0, 0, 1, 0, 0)
    rows = struct.pack("<8I", 0, country, region, place, 2**32-1, 0, 0, 0)
    if ipv6:
        rows += bytes(16) + struct.pack("<3I", country, region, place) + bytes([255])*16 + bytes(12)
    file.write_bytes(header + rows + strings)


def test_db3_file_import_replace_reject_and_ipv6(tmp_path):
    g = GeoIP(tmp_path / "data", source_directory=tmp_path)
    assert g.lookup("8.8.8.8").status == "missing"
    source = tmp_path / "ip-data.bin"
    try:
        db3(source, b"Los Angeles")
        assert g.import_file(source)
        a = g.lookup("8.8.8.8")
        assert (a.country, a.province, a.city, a.source) == ("美国", "加利福尼亚州", "洛杉矶", IP2_OFFLINE)
        assert g.lookup("::ffff:8.8.8.8") is a
        assert g.lookup("2001:4860::8888").status == "no_record"
        db3(source, b"San Francisco", ipv6=True)
        assert g.import_file(source)
        assert g.lookup("8.8.8.8").city == g.lookup("2001:4860::8888").city == "旧金山"
        db3(source, b"Los Angeles", edition=2)
        assert not g.import_file(source)
        source.write_bytes(b"incomplete")
        assert not g.import_file(source)
        assert g.lookup("8.8.8.8").city == "旧金山"
    finally:
        g.close()


def test_settings_import_refreshes_location_without_changing_display_source(qtbot, tmp_path):
    g = GeoIP(tmp_path / "data", source_directory=tmp_path)
    page = GeoSettings(g)
    qtbot.addWidget(page)
    valid = tmp_path / "first.BIN"
    db3(valid, b"Ashburn")
    with qtbot.waitSignal(page.changed):
        page.import_path(valid)
    assert "2026-11-15" in page.info.text()
    assert g.lookup("8.8.8.8").city == "阿什本"
    g.close()
