from datetime import date, datetime, timezone

from proxy_manager.provider.panel_client import PanelClient
from proxy_manager.storage.client_usage import ClientUsage, billing_period


def test_beijing_month_boundary_and_short_month():
    assert billing_period("ISP-17-27.01.08", datetime(2026, 11, 7, 15, 59, tzinfo=timezone.utc)) == date(2026, 10, 8)
    assert billing_period("ISP-17-27.01.08", datetime(2026, 11, 7, 16, tzinfo=timezone.utc)) == date(2026, 11, 8)
    assert billing_period("month-end-31", datetime(2026, 2, 28, 16, tzinfo=timezone.utc)) == date(2026, 2, 28)
    assert billing_period("month-end-31", datetime(2028, 2, 28, 16, tzinfo=timezone.utc)) == date(2028, 2, 29)
    assert billing_period("invalid-00", datetime(2026, 3, 1, tzinfo=timezone.utc)) is None


def test_usage_survives_reopening_and_rolls_over_without_touching_panel_totals(tmp_path):
    path = tmp_path / "client-usage.sqlite3"
    name = "fixture-08"
    panel = "https://fixture.test/panel"
    cutoff = datetime(2026, 11, 7, 16, tzinfo=timezone.utc).timestamp()

    def observe(up, down, stamp, url=panel):
        return ClientUsage(path).observe(url, (PanelClient(name, up=up, down=down),), captured=cutoff + stamp)[name]

    assert observe(100, 200, -10) == (0, 0, date(2026, 10, 8))
    assert observe(300, 500, -5) == (200, 300, date(2026, 10, 8))
    assert observe(400, 600, 0) == (0, 0, date(2026, 11, 8))
    assert observe(500, 800, 5) == (100, 200, date(2026, 11, 8))
    assert observe(5, 850, 10) == (105, 250, date(2026, 11, 8))
    assert observe(500, 800, 15, "https://different.test/panel") == (0, 0, date(2026, 11, 8))
    assert observe(8, 900, -1) == (None, None, date(2026, 10, 8))
    assert observe(10, 900, 15) == (110, 300, date(2026, 11, 8))
