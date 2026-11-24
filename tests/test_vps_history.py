from datetime import UTC, datetime
from time import time

from sqlalchemy import text

from proxy_manager.monitoring.network import Bandwidth, LinkCounters
from proxy_manager.monitoring.scheduler import Sample
from proxy_manager.monitoring.system import Service, SystemMetrics
from proxy_manager.provider.kiwivm_client import KiwiVMEndpoint, Snapshot
from proxy_manager.provider.kiwivm_parser import parse_service_info, parse_usage_history
from proxy_manager.storage.vps_repository import ChartPoint, VpsProfile, VpsRepository
from proxy_manager.ui.vps_charts import series_segments


def setup(database):
    repo = VpsRepository(database)
    profile = repo.prepare(VpsProfile(veid="123", ssh_host="127.0.0.1"))
    repo.save(profile)
    return repo, profile


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, UTC)


def speed(timestamp, rx, tx, *, seconds=5, iface="eth0"):
    link = LinkCounters(iface, 1, rx + 10000, tx + 10000, None)
    bandwidth = Bandwidth("ok", iface, rx * 8 / seconds / 1e6, tx * 8 / seconds / 1e6, link, rx, tx, seconds)
    return Sample(0, bandwidth, None, captured=timestamp)


def test_kiwi_raw_fields_dedup_and_cadence_selection(database):
    repo, profile = setup(database)
    start = 1700000000
    secret = "fixture-secret-unused"
    payload = parse_service_info({"error": 0, "hostname": "fixture", "email": secret, "plan_monthly_data": 10**19,
                                  "data_counter": 10**18, "monthly_data_multiplier": "2.50"}, secret=secret)
    repo.save_kiwi(profile.id, KiwiVMEndpoint.SERVICE_INFO, Snapshot(utc(start), payload))
    def store_history(cadence):
        points = [{"timestamp": start + step * cadence, "network_in_bytes": step * 100,
                   "network_out_bytes": 200 - step * 100} for step in range(2)]
        history = parse_usage_history({"error": 0, "data": points})
        for n in range(2):
            repo.save_kiwi(profile.id, KiwiVMEndpoint.RAW_USAGE_STATS, Snapshot(utc(start + cadence + n), history))
    store_history(300)
    store_history(3600)
    assert repo.kiwi_cadences(profile.id) == (3600, 300)
    assert repo.kiwi_points(profile.id, 300) == [ChartPoint(start, 0, 200), ChartPoint(start + 300, 100, 100)]
    assert repo.kiwi_points(profile.id, 3600)[-1] == ChartPoint(start + 3600, 100, 100)
    data = repo.kiwi_latest(profile.id)[KiwiVMEndpoint.SERVICE_INFO][1]
    assert data["billing"]["raw_quota_bytes"] == 10**19
    assert data["billing"]["raw_used_bytes"] == 10**18
    assert data["billing"]["multiplier"] == "2.50"
    assert secret not in repr(data)
    with database.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kiwivm_usage_points")).scalar_one() == 4
        assert conn.execute(text("SELECT count(*) FROM vps_network_samples")).scalar_one() == 0


def test_unknown_kiwi_history_keeps_last_success(database):
    repo, profile = setup(database)
    valid = parse_usage_history({"error": 0, "data": [{"timestamp": 1700000000, "network_in_bytes": 42}]})
    bad = parse_usage_history({"error": 0, "data": [[77, 88]]})
    repo.save_kiwi(profile.id, KiwiVMEndpoint.RAW_USAGE_STATS, Snapshot(utc(10), valid))
    assert not repo.save_kiwi(profile.id, KiwiVMEndpoint.RAW_USAGE_STATS, Snapshot(utc(11), bad))
    assert repo.kiwi_latest(profile.id)[KiwiVMEndpoint.RAW_USAGE_STATS][0] == 10
    assert repo.kiwi_points(profile.id, 0) == [ChartPoint(1700000000, 42, None)]


def test_rollups_use_actual_elapsed_and_do_not_duplicate_samples(database):
    repo, profile = setup(database)
    start = 1700000040
    samples = [speed(start, 100, 40, seconds=4), speed(start + 7, 300, 70, seconds=7)]
    repo.save_samples(profile.id, samples)
    repo.save_samples(profile.id, samples)
    with database.engine.connect() as conn:
        rollups = conn.execute(text("SELECT samples, gaps, seconds, rx_bytes, tx_bytes FROM vps_network_rollups")).all()
        assert rollups == [(2, 0, 11.0, "400", "110")] * 3
        assert conn.execute(text("SELECT rx_delta, tx_delta, elapsed FROM vps_network_samples ORDER BY captured")).all() == [
            ("100", "40", 4), ("300", "70", 7)]
    points = repo.network_points(profile.id, 168, now=start + 12)
    assert points == [ChartPoint(start, 400 * 8 / 11 / 1e6, 110 * 8 / 11 / 1e6, "eth0")]


def test_missing_samples_and_inactive_intervals_break_both_chart_lines(database):
    repo, profile = setup(database)
    start = 1700000040
    system = SystemMetrics(30, None, None, None, (1, 2, 3), 4, Service("failed"), "running")
    missing = Sample(0, Bandwidth("missing"), None, error="ssh_disconnected", captured=start + 5)
    baseline = Sample(0, Bandwidth("baseline", "eth0"), system, captured=start + 40)
    repo.save_samples(profile.id, [speed(start, 100, 40), missing, baseline, speed(start + 45, 120, 80),
                                   speed(start + 3600, 0, 0)])
    plotted = repo.network_points(profile.id, 24, now=start + 3600)
    assert plotted[1] == ChartPoint(start + 5, None, None)
    assert len(series_segments(plotted, "rx", 15)) == len(series_segments(plotted, "tx", 15)) == 3
    assert plotted[-1].rx == plotted[-1].tx == 0
    assert repo.network_points(profile.id, 168, now=start + 3600)[0] == ChartPoint(start, None, None)
    _, last_system = repo.latest_system(profile.id)
    assert last_system["x_ui"]["state"] == "failed" and last_system["xray"] == "running"


def test_prune_preserves_proxy_data_kiwi_and_daily_monthly_rollups(database):
    repo, profile = setup(database)
    now = utc(time()).timestamp()
    old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    repo.save_samples(profile.id, [speed(old, 100, 20), speed(now - 172801, 200, 40), speed(now, 10, 10)])
    repo.save_kiwi(profile.id, KiwiVMEndpoint.SERVICE_INFO, Snapshot(utc(old),
                   parse_service_info({"error": 0, "hostname": "fixture"})))
    with database.engine.begin() as conn:
        conn.execute(text("INSERT INTO operations VALUES (1,'2020-01-01','sync','success')"))
    repo.prune(now)
    with database.engine.connect() as conn:
        assert conn.execute(text("SELECT captured FROM vps_network_samples")).all() == [(now,)]
        counts = dict(conn.execute(text("SELECT period,count(*) FROM vps_network_rollups GROUP BY period")).all())
        assert counts["minute"] == 2
        assert counts["day"] >= 2 and counts["month"] >= 2
        assert conn.execute(text("SELECT outcome FROM operations")).scalar_one() == "success"
        assert conn.execute(text("SELECT count(*) FROM kiwivm_service_snapshots")).scalar_one() == 1
