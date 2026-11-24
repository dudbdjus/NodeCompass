from proxy_manager.ui.cards import display_gb
from proxy_manager.ui.panel_inventory import _bytes
from proxy_manager.ui.vps_charts import RING_REMAINING, RING_UNKNOWN, RING_USED, UsageRing


def test_display_gb_preserves_existing_byte_scale():
    assert display_gb(2**30) == _bytes(2**30) == "1.00 GB"
    assert display_gb(1024 * 2**30) == "1,024.00 GB"
    assert _bytes(2**20) == "1.00 MiB"
    assert display_gb(None) == "—"


def test_traffic_ring_paints_used_grey_and_remaining_blue(qtbot):
    ring = UsageRing()
    qtbot.addWidget(ring)
    ring.resize(128, 128)
    ring.show()

    def colors():
        image = ring.grab().toImage()
        scale = image.devicePixelRatio()
        return tuple(image.pixelColor(round(x * scale), round(y * scale)).name()
                     for x, y in ((102, 26), (11, 64)))

    ring.show_usage(None)
    assert colors() == (RING_UNKNOWN, RING_UNKNOWN)
    ring.show_usage(25)
    assert colors() == (RING_USED, RING_REMAINING)
    assert "剩余 75.0%" in ring.toolTip()
