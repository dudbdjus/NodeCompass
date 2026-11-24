from contextlib import closing
import json
from pathlib import Path
import sqlite3

from alembic import command
import pytest
from sqlalchemy import URL, create_engine

from proxy_manager.storage.database import Database, migration_config
from proxy_manager.storage.history_startup import confirm_history_startup, prepare_history_startup
from proxy_manager.storage.vps_repository import ChartPoint, VpsRepository
from proxy_manager.storage.upgrade_backup import MigrationVerificationError
from proxy_manager.ui.vps_panel import VpsPanel


def test_desktop_open_refuses_existing_0004_before_backup(tmp_path):
    path = tmp_path / "existing.sqlite3"
    engine = create_engine(URL.create("sqlite", database=str(path)), connect_args={"autocommit": False})
    try:
        with engine.begin() as conn:
            config = migration_config()
            config.attributes["connection"] = conn
            command.upgrade(config, "0004")
    finally:
        engine.dispose()

    before = path.read_bytes()
    with pytest.raises(MigrationVerificationError, match="explicit_legacy_migration_required"):
        Database(path, allow_legacy=False)
    assert path.read_bytes() == before
    assert not (tmp_path / "backups").exists()


def test_first_history_preflight_is_read_only_even_if_retention_requested(database):
    path = Path(database.engine.url.database)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO vps_profiles(id,veid,ssh_port,configuration) VALUES ('a','1',22,'{}')")
        conn.execute("INSERT INTO vps_network_samples(profile_id,captured,state) VALUES ('a',1,'baseline')")
        conn.execute("INSERT INTO vps_network_rollups VALUES ('a','minute','2000-01-01 02:00','eth0',1,0,5,'0','0')")
        conn.execute("""INSERT INTO kiwivm_service_snapshots(profile_id,endpoint,captured,payload)
                        VALUES ('a','getRawUsageStats',1,'{}'),('a','getRawUsageStats',2,'{}')""")
    before = path.read_bytes()
    assert prepare_history_startup(path, requested=True) is False
    plan = json.loads(path.with_suffix(".sqlite3.history-preflight.json").read_text(encoding="utf-8"))
    assert plan["integrity"] == "ok"
    assert plan["ssh_dry_run"]["vps_network_samples"][0] == 1
    assert plan["ssh_dry_run"]["vps_network_rollups"][0] == 1
    assert plan["kiwivm_dry_run"]["ranges"][0]["count"] == 1
    assert prepare_history_startup(path, requested=True) is False
    confirm_history_startup(path)
    assert prepare_history_startup(path, requested=False) is False
    assert prepare_history_startup(path, requested=True) is True
    assert path.read_bytes() == before
    assert not path.with_suffix(".sqlite3.kiwi-retention.json").exists()
    assert not path.parent.joinpath("backups").exists()


def test_background_cleanup_stays_paused_while_panel_is_usable(database, qtbot, monkeypatch):
    panel = VpsPanel(database)
    qtbot.addWidget(panel)
    panel.next_retention = 0
    panel.next_maintenance = 0
    monkeypatch.setattr(panel.retention, "run_daily", lambda *_: pytest.fail("KiwiVM cleanup started"))
    monkeypatch.setattr(panel.repo, "prune", lambda *_: pytest.fail("SSH cleanup started"))
    panel._poll_retention()
    panel._write_history()
    assert panel.retention_job is None and panel.writer is None
    assert panel.shutdown()


def test_candidate_handles_history_rows_without_network_fields(database):
    path = Path(database.engine.url.database)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO vps_profiles(id,veid,ssh_port,configuration) VALUES ('a','1',22,'{}')")
        conn.execute("INSERT INTO kiwivm_usage_points VALUES ('a',1600000000,0,'{}')")
        conn.execute("""INSERT INTO kiwivm_usage_points VALUES
                      ('a',1600000300,0,'{"network_in_bytes":120}')""")
    assert VpsRepository(database).kiwi_points("a", 0) == [
        ChartPoint(1600000000, None, None), ChartPoint(1600000300, 120, None),
    ]
