import asyncio
from datetime import UTC, datetime
from threading import Event
from time import time
from uuid import uuid4
from unittest.mock import Mock

import httpx
from PySide6.QtWidgets import QMessageBox

from proxy_manager.monitoring.network import Bandwidth, LinkCounters
from proxy_manager.monitoring.scheduler import Sample
from proxy_manager.monitoring.ssh_client import HostIdentity, HostKeyConfirmationRequired, SshError
from proxy_manager.monitoring.system import Disk, Memory, Service, Swap, SystemMetrics
from proxy_manager.provider.kiwivm_client import KiwiVMEndpoint, Snapshot
from proxy_manager.provider.kiwivm_parser import parse_service_info, parse_usage_history
from proxy_manager.security import ssh_credentials, vps_credentials
from proxy_manager.security.vps_credentials import KiwiVMCredential
from proxy_manager.services import vps_jobs
from proxy_manager.services.vps_jobs import ApiSchedule, refresh_kiwi
from proxy_manager.storage.vps_repository import VpsProfile, VpsRepository
from proxy_manager.ui import vps_panel
from proxy_manager.ui.vps_panel import VpsPanel


def make_panel(qtbot, database, **config):
    repo = VpsRepository(database)
    profile = repo.prepare(VpsProfile(veid="123", ssh_host="127.0.0.1", username="viewer", **config))
    repo.save(profile)
    panel = VpsPanel(database)
    qtbot.addWidget(panel)
    panel.show()
    return panel, repo, profile


def fake_ssh(monkeypatch, *, mismatch=False):
    actions = []

    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            self.pin = HostIdentity(endpoint, "ssh-ed25519", "SHA256:fixture")

        def connect(self, pin, auth=None):
            if pin is None:
                assert auth is None
                actions.append("discover")
                raise HostKeyConfirmationRequired(self.pin)
            actions.append("verify")
            if mismatch or not pin.matches(self.pin):
                raise SshError("ssh_host_key_changed")
            assert auth is secret_auth
            actions.append("authenticate")

        def close(self):
            actions.append("close")

        def interrupt(self):
            actions.append("interrupt")

    secret_auth = object()
    monkeypatch.setattr(vps_jobs, "SshClient", Client)
    monkeypatch.setattr(vps_jobs, "ssh_auth", lambda profile: secret_auth)
    return actions


def confirm(panel, role):
    box = panel.confirmation
    assert "127.0.0.1" in box.text() and "22" in box.text()
    assert "ssh-ed25519" in box.text() and "SHA256:fixture" in box.text()
    assert "可信渠道" in box.informativeText()
    next(button for button in box.buttons() if box.buttonRole(button) == role).click()


def test_ssh_confirmation_saves_pin_then_connects(qtbot, database, monkeypatch):
    actions = fake_ssh(monkeypatch)
    panel, repo, profile = make_panel(qtbot, database)
    panel.settings.test.click()
    qtbot.waitUntil(lambda: panel.confirmation is not None)
    assert actions == ["discover", "close"]
    assert repo.trusted(profile.endpoint) is None
    confirm(panel, QMessageBox.ButtonRole.AcceptRole)
    qtbot.waitUntil(lambda: panel.ssh_job is None)
    assert actions == ["discover", "close", "verify", "authenticate", "close"]
    assert repo.trusted(profile.endpoint).sha256 == "SHA256:fixture"
    assert "测试通过" in panel.ssh_status.text()
    panel.settings.test.click()
    qtbot.waitUntil(lambda: panel.ssh_job is None)
    assert panel.confirmation is None
    assert actions.count("discover") == 1 and actions.count("authenticate") == 2
    assert panel.shutdown()


def test_cancel_confirmation_sends_no_authentication(qtbot, database, monkeypatch):
    actions = fake_ssh(monkeypatch)
    panel, repo, profile = make_panel(qtbot, database)
    panel.settings.test.click()
    qtbot.waitUntil(lambda: panel.confirmation is not None)
    confirm(panel, QMessageBox.ButtonRole.RejectRole)
    assert panel.ssh_job is None
    assert actions == ["discover", "close"]
    assert repo.trusted(profile.endpoint) is None
    assert panel.shutdown()


def test_saved_pin_mismatch_does_not_open_new_confirmation(qtbot, database, monkeypatch):
    actions = fake_ssh(monkeypatch, mismatch=True)
    panel, repo, profile = make_panel(qtbot, database)
    old = HostIdentity(profile.endpoint, "ssh-ed25519", "SHA256:previous")
    repo.trust(profile, old)
    panel.settings.test.click()
    qtbot.waitUntil(lambda: panel.ssh_job is None)
    assert actions == ["verify", "close"]
    assert panel.confirmation is None
    assert "指纹已变化" in panel.ssh_status.text()
    assert repo.trusted(profile.endpoint) == old
    assert panel.shutdown()


def test_gui_controls_store_encrypted_secrets_outside_sqlite(qtbot, database, monkeypatch, caplog):
    monkeypatch.setattr(vps_credentials, "_TARGET_PREFIX", f"ProxyManager/TestVps/{uuid4()}")
    monkeypatch.setattr(ssh_credentials, "_TARGET_PREFIX", f"ProxyManager/TestSSH/{uuid4()}")
    panel, repo, profile = make_panel(qtbot, database)
    key, password, phrase = "fixture-api-secret", "fixture-ssh-secret", "fixture-key-secret"
    fields = panel.settings.fields
    for field, value in (("api_key", key), ("password", password), ("passphrase", phrase)):
        assert fields[field].echoMode() == fields[field].EchoMode.Password
        fields[field].setText(value)
    try:
        panel.settings.save.click()
        from proxy_manager.security.ssh_credentials import SecretKind, SshCredentialStore
        from proxy_manager.security.vps_credentials import KiwiVMCredentialStore
        assert KiwiVMCredentialStore().load(profile.veid).api_key == key
        assert SshCredentialStore().load(profile.credential_id, SecretKind.PASSWORD) == password
        assert SshCredentialStore().load(profile.credential_id, SecretKind.PASSPHRASE) == phrase
        assert not any(fields[field].text() for field in ("api_key", "password", "passphrase"))
        serialized = repr(repo.active()) + caplog.text
        assert not any(secret in serialized for secret in (key, password, phrase))
        from pathlib import Path
        raw = Path(database.engine.url.database).read_bytes()
        assert not any(secret.encode() in raw for secret in (key, password, phrase))
    finally:
        KiwiVMCredentialStore().delete(profile.veid)
        for kind in SecretKind:
            SshCredentialStore().delete(profile.credential_id, kind)
        assert panel.shutdown()


def test_views_render_sources_metrics_and_real_history_cadences(qtbot, database):
    panel, repo, profile = make_panel(qtbot, database, name="Manual Name", location="Local")
    stamp = time()
    service = parse_service_info({"error": 0, "hostname": "Kiwi Fixture", "ip_addresses": ["192.0.2.10"],
                                  "node_location": "California", "plan_ram": 2048, "plan_monthly_data": 10000,
                                  "data_counter": 2500, "monthly_data_multiplier": 2, "data_next_reset": int(stamp + 3600)})
    repo.save_kiwi(profile.id, KiwiVMEndpoint.SERVICE_INFO, Snapshot(datetime.now(UTC), service))
    for gap in (60, 3600):
        history = parse_usage_history({"error": 0, "data": [
            {"timestamp": 1700000000 + gap * n, "network_in_bytes": n * 123, "network_out_bytes": n * 456}
            for n in range(2)
        ]})
        repo.save_kiwi(profile.id, KiwiVMEndpoint.RAW_USAGE_STATS, Snapshot(datetime.fromtimestamp(stamp, UTC), history))
    metrics = SystemMetrics(22, Memory(2**30, 2**29, 50), Swap(0, None), Disk(2**30, 2**29),
                            (1, 2, 3), 3601, Service("failed"), "running")
    repo.save_samples(profile.id, [Sample(0, Bandwidth("baseline", "ens3"), metrics, captured=stamp),
                                  Sample(0, Bandwidth("missing"), None, captured=stamp + 5)])
    panel.refresh_data()
    assert panel.tabs.count() == 4
    assert panel.overview.labels["VPS 名称"].text() == "Kiwi Fixture"
    assert panel.overview.labels["剩余流量"].text() == "7,500 原始字节"
    assert panel.overview.usage.value() == 250
    assert not panel.overview.usage.isHidden()
    assert panel.kiwi.progress.value() == 250
    assert panel.kiwi.resolution.count() == 2
    panel.kiwi.resolution.setCurrentIndex(panel.kiwi.resolution.findData(3600))
    assert panel.kiwi.chart.rx[0][1] == (1700003600, 123)
    assert panel.system.labels["CPU"].text() == "22.0%"
    assert [bar.value() for bar in panel.system.bars] == [220, 500, 0, 500]
    assert panel.system.bars[2].isHidden()
    assert panel.system.labels["x-ui.service"].text() == "失败"
    assert panel.system.labels["Xray 进程"].text() == "运行中"
    assert panel.bandwidth.chart.rx == []
    assert panel.shutdown()


def test_kiwi_schedule_respects_independent_due_times():
    clock = [0]
    schedule = ApiSchedule(clock=lambda: clock[0])
    profile = VpsProfile(live_minutes=10)
    schedule.started(schedule.due(profile))
    clock[0] = 599
    assert schedule.due(profile) == ()
    clock[0] = 600
    assert schedule.due(profile) == (KiwiVMEndpoint.LIVE_SERVICE_INFO,)
    clock[0] = 900
    assert len(schedule.due(profile, forced=True)) == 3


def test_api_stall_does_not_block_ssh_and_cancellation_cleans_up(qtbot, database, monkeypatch):
    actions = fake_ssh(monkeypatch)
    started = Event()
    requests = []

    def blocked(repo, profile, due, cancel):
        requests.append(due)
        started.set()
        assert cancel.wait(5)
        return {}

    monkeypatch.setattr(vps_panel, "refresh_kiwi", blocked)
    panel, _, _ = make_panel(qtbot, database)
    panel.manual_api()
    qtbot.waitUntil(started.is_set)
    panel.manual_api()
    panel.test_connection()
    qtbot.waitUntil(lambda: panel.confirmation is not None)
    assert requests and len(requests) == 1
    assert actions == ["discover", "close"]
    assert not panel.shutdown()
    qtbot.waitUntil(panel.shutdown)
    assert panel.api_job is panel.ssh_job is panel.confirmation is None
    assert not panel.timer.isActive()


def test_kiwi_unconfigured_or_http_failure_preserves_previous_snapshot(database, respx_mock, monkeypatch, caplog):
    repo = VpsRepository(database)
    profile = repo.prepare(VpsProfile(veid="123"))
    repo.save(profile)
    endpoint = KiwiVMEndpoint.SERVICE_INFO
    repo.save_kiwi(profile.id, endpoint, Snapshot(datetime.now(UTC), parse_service_info({"error": 0, "hostname": "Prior"})))
    store = vps_jobs.KiwiVMCredentialStore
    monkeypatch.setattr(store, "load", lambda self, veid: None)
    assert refresh_kiwi(repo, profile, [endpoint], Event()) == {"credential": "no_kiwi_credentials"}
    assert not respx_mock.calls
    secret = "fixture-only-key"
    monkeypatch.setattr(store, "load", lambda self, veid: KiwiVMCredential("123", secret))
    respx_mock.post("https://api.64clouds.com/v1/getServiceInfo").mock(return_value=httpx.Response(403, text=secret))
    assert refresh_kiwi(repo, profile, [endpoint], Event()) == {endpoint: "authentication_failed"}
    assert repo.kiwi_latest(profile.id)[endpoint][1]["hostname"] == "Prior"
    assert secret not in caplog.text


def test_cancel_interrupts_real_httpx_async_read(qtbot, database, monkeypatch, respx_mock):
    opened = Event()
    released = Event()

    class PendingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            opened.set()
            await asyncio.sleep(60)
            yield b""

        async def aclose(self):
            released.set()

    monkeypatch.setattr(vps_jobs.KiwiVMCredentialStore, "load",
                        lambda self, veid: KiwiVMCredential(veid, "synthetic-fixture-key"))
    respx_mock.post("https://api.64clouds.com/v1/getServiceInfo").mock(
        return_value=httpx.Response(200, stream=PendingBody()))
    panel, _, _ = make_panel(qtbot, database)
    panel.manual_api()
    qtbot.waitUntil(opened.is_set, timeout=3000)
    panel.cancel()
    qtbot.waitUntil(lambda: panel.api_job is None, timeout=2000)
    assert released.is_set()
    assert "已取消" in panel.api_status.text()
    assert len(respx_mock.calls) == 1
    assert panel.shutdown()


def test_gui_ssh_queue_persists_rates_and_gaps(qtbot, database, monkeypatch):
    stamp = time() - 35
    bursts = [
        Sample(0, Bandwidth("ok", "ens3", 8, 4, LinkCounters("ens3", 1, 5000100, 2500100, None),
                            5000000, 2500000, 5), None, ("ens3",), captured=stamp),
        Sample(0, Bandwidth("missing"), None, error="ssh_disconnected", captured=stamp + 5),
        Sample(0, Bandwidth("baseline", "ens3"), None, ("ens3",), captured=stamp + 25),
        Sample(0, Bandwidth("ok", "ens3", 1.6, 3.2, LinkCounters("ens3", 1, 1000000, 2000000, None),
                            1000000, 2000000, 5), None, ("ens3",), captured=stamp + 30),
    ]
    instances = []

    class Monitor:
        def __init__(self, *args, **kwargs):
            instances.append(self)
            self.running = False
            self.queued = list(bursts)
            self.refreshes = 0

        def start(self):
            self.running = True

        def refresh_now(self):
            self.refreshes += 1

        def drain(self):
            drained, self.queued = self.queued, []
            return drained

        def request_stop(self):
            self.running = False

    monkeypatch.setattr(vps_panel, "MonitoringWorker", Monitor)
    monkeypatch.setattr(vps_panel, "ssh_auth", lambda _: object())
    panel, repo, profile = make_panel(qtbot, database)
    repo.trust(profile, HostIdentity(profile.endpoint, "ssh-ed25519", "SHA256:fixture"))
    panel.start_monitoring()
    panel.start_monitoring()
    qtbot.waitUntil(lambda: panel.bandwidth.labels["VPS 接收 RX"].text() == "1.60 Mbps")
    assert len(instances) == 1 and instances[0].refreshes == 1
    assert len(panel.bandwidth.chart.rx) == len(panel.bandwidth.chart.tx) == 2
    assert panel.bandwidth.labels["当前范围实采流量"].text() == "RX 6,000,000 B / TX 4,500,000 B"
    assert panel.bandwidth.interface.findText("ens3") >= 0
    panel.cancel()
    qtbot.waitUntil(lambda: panel.monitor is None and panel.writer is None)
    assert not instances[0].running and panel.shutdown()


def test_main_window_waits_asynchronously_for_vps_jobs(qtbot, database, monkeypatch):
    from proxy_manager.security.credentials import CredentialStore
    from proxy_manager.services.sync import SyncService
    from proxy_manager.storage.repositories import Repository
    from proxy_manager.ui.main_window import MainWindow

    started, finished = Event(), Event()

    def action(repo, profile, due, cancel):
        started.set()
        assert cancel.wait(3)
        finished.set()
        return {}

    monkeypatch.setattr(vps_panel, "refresh_kiwi", action)
    repo = VpsRepository(database)
    repo.save(repo.prepare(VpsProfile(veid="123")))
    credentials = Mock(spec=CredentialStore)
    credentials.load.return_value = None
    window = MainWindow(Repository(database), credentials, SyncService(database))
    qtbot.addWidget(window)
    window.show()
    window.vps.manual_api()
    qtbot.waitUntil(started.is_set)
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=3000)
    assert finished.is_set()
    assert not window.vps.timer.isActive()
    assert window.vps.api_job is None
