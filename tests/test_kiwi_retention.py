from contextlib import closing
from datetime import UTC, datetime
from hashlib import file_digest
import json
import sqlite3
from threading import Event, Thread

import pytest

from proxy_manager.storage import kiwi_retention as retention
from proxy_manager.storage.database import Database
from proxy_manager.storage.maintenance_lock import MaintenanceBusy, maintenance_lock


NOW = datetime(2026, 11, 22, 1, tzinfo=UTC).timestamp()
DAY = 86400
SNAPSHOT, POINT = retention.cutoffs(NOW)
SERVICE, LIVE, HISTORY = retention._SOURCES


def execute(path, sql, params=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def register(path, profile):
    execute(path, "INSERT INTO vps_profiles(id,veid,ssh_port,configuration) VALUES (?,?,22,'{}')",
            (profile, "1"))


def snapshot(path, profile, endpoint, captured, reset=None):
    payload = {"billing": {"reset_timestamp": reset}}
    if endpoint == LIVE:
        payload = {"service": payload}
    execute(path, "INSERT INTO kiwivm_service_snapshots(profile_id,endpoint,captured,payload) VALUES (?,?,?,?)",
            (profile, endpoint, captured, json.dumps(payload)))


def point(path, profile, timestamp):
    execute(path, "INSERT INTO kiwivm_usage_points VALUES (?,?,0,'{}')", (profile, timestamp))


def seed(path, profile="a", previous=None):
    previous = int(NOW - 25 * DAY) if previous is None else previous
    register(path, profile)
    snapshot(path, profile, SERVICE, previous - DAY, previous)
    snapshot(path, profile, SERVICE, NOW - 300, int(NOW + 3 * DAY))
    snapshot(path, profile, LIVE, NOW - 30, int(NOW + 3 * DAY))


def digest(path):
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "isolated.sqlite3"
    db = Database(path)
    db.close()
    seed(path)
    clock = [NOW - DAY - 1, 100]
    job = retention.KiwiRetention(path, wall_clock=lambda: clock[0], steady_clock=lambda: clock[1])
    assert job.run_daily(Event()) == "waiting"
    clock[:] = [NOW, DAY + 101]
    return path, job, clock


def test_cutoffs_use_utc_and_calendar_months():
    leap = datetime(2024, 2, 29, 15, 20, 17, tzinfo=UTC)
    snap, raw = retention.cutoffs(leap.timestamp())
    assert datetime.fromtimestamp(snap, UTC) == datetime(2023, 12, 1, 15, 20, 17, tzinfo=UTC)
    assert datetime.fromtimestamp(raw, UTC) == datetime(2022, 2, 28, 15, 20, 17, tzinfo=UTC)


def test_snapshot_exact_90_day_boundary_and_sources(context):
    path, job, _ = context
    for endpoint in (SERVICE, LIVE, HISTORY):
        for dt in (-1, 0, 1):
            snapshot(path, "a", endpoint, SNAPSHOT + dt, int(SNAPSHOT + 3 * DAY))
    expected = {entry.source: (entry.count, entry.first, entry.last) for entry in job.plan().ranges}
    assert expected == {source: (1, SNAPSHOT - 1, SNAPSHOT - 1) for source in (SERVICE, LIVE, HISTORY)}
    assert job.run_daily(Event()) == "completed"
    remaining = execute(path, "SELECT endpoint,captured FROM kiwivm_service_snapshots WHERE captured BETWEEN ? AND ?",
                        (SNAPSHOT - 1, SNAPSHOT + 1))
    assert sorted(remaining) == sorted((src, SNAPSHOT + dt) for src in (SERVICE, LIVE, HISTORY) for dt in (0, 1))


def test_raw_exact_24_month_boundary(context):
    path, job, _ = context
    for timestamp in (POINT - 1, POINT, POINT + 1):
        point(path, "a", timestamp)
    [item] = job.plan().ranges
    assert item.source == "usage_points" and item.count == 1 and item.first == POINT - 1
    job.run_daily(Event())
    assert execute(path, "SELECT timestamp FROM kiwivm_usage_points ORDER BY timestamp") == [(POINT,), (POINT + 1,)]


def test_current_cycle_protected_even_if_older_than_both_cutoffs(context):
    path, job, _ = context
    boundary = POINT - DAY
    seed(path, "old-cycle", boundary)
    future = int(NOW + 3 * DAY)
    snapshot(path, "old-cycle", SERVICE, SNAPSHOT - DAY, future)
    snapshot(path, "old-cycle", LIVE, SNAPSHOT - DAY, future)
    snapshot(path, "old-cycle", SERVICE, SNAPSHOT - 2 * DAY, None)
    for ts in (boundary - 1, boundary, POINT):
        point(path, "old-cycle", ts)
    [planned] = job.plan().ranges
    assert planned.profile_id == "old-cycle" and planned.first == boundary - 1
    job.run_daily(Event())
    assert execute(path, "SELECT timestamp FROM kiwivm_usage_points WHERE profile_id='old-cycle' ORDER BY timestamp") == [
        (boundary,), (POINT,)]
    assert len(execute(path, "SELECT id FROM kiwivm_service_snapshots WHERE profile_id='old-cycle'")) == 6


def test_inactive_and_unknown_cycle_remain_isolated(context):
    path, job, _ = context
    register(path, "unknown")
    snapshot(path, "unknown", SERVICE, SNAPSHOT - DAY, None)
    point(path, "a", POINT - 1)
    point(path, "unknown", POINT - 1)
    plan = job.plan()
    assert [entry.profile_id for entry in plan.ranges] == ["a"]
    assert plan.deferred_profiles == ["unknown"]
    job.run_daily(Event())
    assert execute(path, "SELECT profile_id FROM kiwivm_usage_points") == [("unknown",)]


def test_dry_run_does_not_change_sqlite_or_sidecars(context):
    path, job, _ = context
    point(path, "a", POINT - 1)
    before = {item.name: digest(item) for item in path.parent.iterdir()}
    assert job.plan().ranges[0].count == 1
    after = {item.name: digest(item) for item in path.parent.iterdir()}
    assert before == after


def test_failed_backup_prevents_all_deletion(context, monkeypatch):
    path, job, _ = context
    point(path, "a", POINT - 1)

    def fail(*args, **kwargs):
        raise OSError("simulated")

    monkeypatch.setattr(retention, "verified_backup", fail)
    with pytest.raises(OSError):
        job.run_daily(Event())
    assert execute(path, "SELECT timestamp FROM kiwivm_usage_points") == [(POINT - 1,)]
    audit = job.audit_path.read_text(encoding="utf-8")
    assert '"event": "plan"' in audit and '"committed_rows": 0' in audit


def test_failure_rolls_back_current_transaction(context):
    path, job, _ = context
    point(path, "a", POINT - 1)
    execute(path, """CREATE TRIGGER fail_cleanup BEFORE INSERT ON monitoring_sync_runs
                    BEGIN SELECT RAISE(ABORT, 'injected audit transaction failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        job.run_daily(Event())
    assert execute(path, "SELECT timestamp FROM kiwivm_usage_points") == [(POINT - 1,)]
    assert execute(path, "PRAGMA integrity_check") == [("ok",)]


def test_backup_verified_once_and_repeated_cleanup_is_idempotent(context):
    path, job, clock = context
    point(path, "a", POINT - 1)
    job.run_daily(Event())
    [backup] = list((path.parent / "backups").glob("*.sqlite3"))
    assert execute(backup, "PRAGMA integrity_check") == [("ok",)]
    assert execute(backup, "SELECT timestamp FROM kiwivm_usage_points") == [(POINT - 1,)]
    assert digest(backup) == retention.verify_backup_digest(backup)
    assert job.run_daily(Event()) == "waiting"
    clock[:] = [NOW + DAY + 1, 2 * DAY + 102]
    assert job.run_daily(Event()) == "empty"
    assert list((path.parent / "backups").glob("*.sqlite3")) == [backup]


def test_sync_inserts_are_revalidated_and_keep_new_rows(context, monkeypatch):
    path, job, _ = context
    for dt in (100, 200, 300):
        point(path, "a", POINT - dt)
    original = job._delete_batch
    invoked = []
    monkeypatch.setattr(retention, "_BATCH_SIZE", 1)

    def with_sync(*args):
        if not invoked:
            sync = Thread(target=point, args=(path, "a", POINT - 250))
            sync.start()
            sync.join(3)
            assert not sync.is_alive()
        invoked.append(True)
        return original(*args)

    monkeypatch.setattr(job, "_delete_batch", with_sync)
    job.run_daily(Event())
    assert execute(path, "SELECT timestamp FROM kiwivm_usage_points") == [(POINT - 250,)]
    assert execute(path, "SELECT count(*) FROM monitoring_sync_runs") == [(3,)]


@pytest.mark.parametrize("change", [-4 * DAY, 500 * DAY])
def test_wall_jump_blocks_deletions(context, change):
    path, job, clock = context
    point(path, "a", POINT - 1)
    clock[0] += change
    with pytest.raises(retention.RetentionClockError):
        job.run_daily(Event())
    assert execute(path, "SELECT count(*) FROM kiwivm_usage_points") == [(1,)]
    assert not (path.parent / "backups").exists()


def test_cleanup_preserves_proxy_and_ssh_tables(context):
    path, job, _ = context
    execute(path, """INSERT INTO proxies(profile_id,provider_id,address,port_http,port_socks,order_id)
                    VALUES ('profile','proxy','192.0.2.20',8080,1080,'fixture-order')""")
    execute(path, "INSERT INTO operations VALUES (1,'2026-01-01','sync','success')")
    execute(path, "INSERT INTO sync_runs VALUES (1,'profile','2026-01-01','2026-01-01','success',0,NULL)")
    execute(path, "INSERT INTO proxy_annotations VALUES ('profile','proxy','region','tag')")
    execute(path, "INSERT INTO vps_network_samples(profile_id,captured,state) VALUES ('a',1,'baseline')")
    execute(path, "INSERT INTO vps_system_samples VALUES ('a',1,'{}')")
    execute(path, "INSERT INTO vps_network_rollups VALUES ('a','minute','2020-01-01 00:01','eth0',1,0,5,1,1)")
    tables = ("proxies", "operations", "sync_runs", "proxy_annotations", "vps_network_samples",
              "vps_system_samples", "vps_network_rollups")
    before = {table: execute(path, f"SELECT * FROM {table}") for table in tables}
    point(path, "a", POINT - 1)
    job.run_daily(Event())
    assert {table: execute(path, f"SELECT * FROM {table}") for table in tables} == before


def test_cleanup_and_migration_share_exclusive_gate(context):
    path, job, _ = context
    with maintenance_lock(path):
        with pytest.raises(MaintenanceBusy):
            job.run_daily(Event())
        with pytest.raises(MaintenanceBusy):
            Database(path)


def test_concurrent_cycle_change_preserves_remaining_history(context, monkeypatch):
    path, job, _ = context
    point(path, "a", POINT - 2)
    point(path, "a", POINT - 1)
    original = job._delete_batch
    monkeypatch.setattr(retention, "_BATCH_SIZE", 1)

    def after_first_commit(*args):
        count = original(*args)
        if count:
            snapshot(path, "a", LIVE, NOW, int(NOW + 4 * DAY))
        return count

    monkeypatch.setattr(job, "_delete_batch", after_first_commit)
    job.run_daily(Event())
    assert execute(path, "SELECT timestamp FROM kiwivm_usage_points") == [(POINT - 1,)]


def test_background_retention_cancels_and_is_not_reentered(database, qtbot, monkeypatch):
    from proxy_manager.ui.vps_panel import VpsPanel

    panel = VpsPanel(database, retention_enabled=True)
    qtbot.addWidget(panel)
    started = Event()
    calls = []

    def blocked(cancel):
        calls.append(True)
        started.set()
        assert cancel.wait(3)
        return "cancelled"

    monkeypatch.setattr(panel.retention, "run_daily", blocked)
    panel.next_retention = 0
    panel._poll_retention()
    qtbot.waitUntil(started.is_set)
    panel.next_retention = 0
    panel._poll_retention()
    qtbot.waitUntil(panel.shutdown)
    assert len(calls) == 1
    assert panel.retention_job is None
