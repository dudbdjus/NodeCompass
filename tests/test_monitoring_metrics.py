from dataclasses import replace
import json
from threading import Event

import pytest

from proxy_manager.monitoring import scheduler
from proxy_manager.monitoring.network import Bandwidth, BandwidthSampler, LinkCounters, parse_links, select_link
from proxy_manager.monitoring.scheduler import Collector, MonitoringWorker
from proxy_manager.monitoring.ssh_client import Command, Endpoint, HostIdentity, PasswordAuth, SshError
from proxy_manager.monitoring.system import (CpuSampler, Disk, Memory, Service, Swap, Version, parse_disk,
                                             parse_load, parse_memory, parse_service, parse_uptime, parse_xray)


_BOOT = "11111111-1111-4111-8111-111111111111"
_OTHER_BOOT = "22222222-2222-4222-8222-222222222222"


def link(name, index, rx=1, tx=1, kind=None):
    return {"ifname": name, "ifindex": index, "stats64": {"rx": {"bytes": rx}, "tx": {"bytes": tx}},
            "linkinfo": {"info_kind": kind}}


def routes(*dev_metric):
    return json.dumps([{"dst": "default", "dev": name, "metric": metric} for name, metric in dev_metric])


def test_default_interface_uses_one_route_and_excludes_overlay():
    links = parse_links(json.dumps([link("lo", 1), link("eth0", 2, 120, 240), link("eth1", 3, 50, 60),
                                    link("tun0", 4, 99999, 99999, "tun")]))
    selected = select_link(links, routes(("tun0", 1), ("eth1", 200), ("eth0", 100)), None)
    assert selected == LinkCounters("eth0", 2, 120, 240, None)
    assert "lo" not in links
    assert select_link(links, routes(("tun0", 0)), routes(("eth1", 15))).name == "eth1"
    assert select_link(links, routes(("eth0", 1)), None, "eth1").name == "eth1"
    assert select_link(links, routes(("eth0", 1)), None, "tun0").rx_bytes == 99999
    assert select_link(links, routes(("eth0", 1)), None, "eth0; reboot") is None


def test_multipath_is_deterministic_and_counters_can_use_stats():
    legacy = link("eth1", 3, 50, 60)
    legacy["stats"] = legacy.pop("stats64")
    links = parse_links(json.dumps([legacy, link("eth0", 2), link("bad", 4, -1), 5]))
    multipath = '[{"dst":"default","nexthops":[{"dev":"eth1"},{"dev":"eth0"}]}]'
    assert select_link(links, multipath, None).name == "eth0"
    assert links["eth1"].tx_bytes == 60
    assert set(links) == {"eth0", "eth1"}
    assert parse_links("{no json") == {}


def test_bandwidth_actual_interval_and_real_zero():
    sampler = BandwidthSampler(15)
    initial = LinkCounters("eth0", 2, 10, 20, None)
    assert sampler.sample(initial, 100, _BOOT, 40) == Bandwidth("baseline", "eth0")
    updated = replace(initial, rx_bytes=12_000_010, tx_bytes=3_000_020)
    assert sampler.sample(updated, 106, _BOOT, 46) == Bandwidth("ok", "eth0", 16, 4)
    assert sampler.sample(updated, 111, _BOOT, 51) == Bandwidth("ok", "eth0", 0, 0)


@pytest.mark.parametrize("update,now,boot,uptime", [
    ({"rx_bytes": 1}, 105, _BOOT, 45),
    ({"tx_bytes": 1}, 105, _BOOT, 45),
    ({"name": "eth1"}, 105, _BOOT, 45),
    ({"index": 3}, 105, _BOOT, 45),
    ({}, 105, _OTHER_BOOT, 45),
    ({}, 105, _BOOT, 10),
    ({}, 130, _BOOT, 70),
    ({}, 100, _BOOT, 40),
])
def test_bandwidth_resets_on_discontinuous_counters(update, now, boot, uptime):
    sampler = BandwidthSampler(15)
    initial = LinkCounters("eth0", 2, 10, 20, None)
    sampler.sample(initial, 100, _BOOT, 40)
    current = replace(initial, **update)
    assert sampler.sample(current, now, boot, uptime) == Bandwidth("baseline", current.name)


def test_missing_counters_and_unknown_boot_do_not_fill_zero():
    sampler = BandwidthSampler(15)
    initial = LinkCounters("eth0", 2, 10, 20, None)
    sampler.sample(initial, 100, None, 40)
    assert sampler.sample(None, 105, None, 45) == Bandwidth("missing")
    assert sampler.sample(initial, 110, None, None) == Bandwidth("missing")
    assert sampler.sample(initial, 115, None, 55) == Bandwidth("baseline", "eth0")
    sampler.reset()
    assert sampler.sample(initial, 120, None, 60) == Bandwidth("baseline", "eth0")


def test_cpu_deltas_ignore_guest_and_clear_bad_data():
    sampler = CpuSampler()
    assert sampler.sample("cpu 100 20 40 200 10 5 5 5 30 2") is None
    assert sampler.sample("cpu 120 20 50 250 30 5 5 5 50 2") == 30
    assert sampler.sample("cpu 119 20 50 300 30 5 5 5 51 2") is None
    assert sampler.sample("cpu 119 20 50 300 30 5 5 5 51 2") is None
    assert sampler.sample("malformed") is None
    assert sampler.sample("cpu 125 20 55 350 30 5 5 5 51 2") is None


def test_system_parsing_and_units():
    text = "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 20 kB\nSwapFree: 5 kB"
    assert parse_memory(text) == (Memory(1024000, 409600, 60), Swap(20480, 75))
    assert parse_memory("MemTotal: foo kB\nSwapTotal: 0 kB\nSwapFree: 0 kB") == (None, Swap(0, None))
    assert parse_memory("SwapTotal: 10 kB\nSwapFree: 20 kB") == (None, None)
    assert parse_disk("header\n/dev/root with space 10000 2000 7000 23% /") == Disk(10000, 2000)
    assert parse_disk("header\n/dev/data 3000 1000 2000 34% /data") is None
    assert parse_load("0.12 0.20 1.03 2/210 5") == (0.12, 0.2, 1.03)
    assert parse_load("NaN 0.00 0.00") is None
    assert parse_uptime("432.17 607.84") == 432.17
    assert parse_uptime("-3 25") is None


def test_x_ui_service_and_xray_process_independent():
    assert parse_service("LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=1") \
        == Service("failed", "failed", "exit-code", 1)
    assert parse_service("LoadState=not-found\nActiveState=inactive") == Service("not_found")
    assert parse_service(None) == Service("unknown")
    assert parse_service("LoadState=loaded\nActiveState=active\nSubState=running") == Service("active", "running")
    assert parse_xray("COMMAND\nsystemd\nxray-linux-amd6\nx-ui") == "running"
    assert parse_xray("COMMAND\nxray-checker\nmy-xray\nx-ui") == "not_observed"
    assert parse_xray("permission denied") == "unknown"


class FixtureClient:
    def __init__(self):
        self.commands = []
        self.uptime = 100.25
        self.bytes = 1_000_000

    def run(self, command):
        self.commands.append(command)
        outputs = {
            Command.BOOT_ID: _BOOT,
            Command.IPV4_ROUTE: routes(("eth0", 0)),
            Command.IPV6_ROUTE: "[]",
            Command.LINKS: json.dumps([link("eth0", 2, self.bytes, self.bytes), link("eth1", 3)]),
            Command.UPTIME: str(self.uptime),
            Command.CPU: "cpu 1 2 3 4 5 6 7 8",
            Command.MEMORY: "MemTotal: 1000 kB\nMemAvailable: 400 kB",
            Command.ROOT_DISK: "header\n/dev/root 10000 3000 7000 30% /",
            Command.LOAD: "0.12 0.20 1.03 2/210 5",
            Command.PROCESSES: "COMMAND\nsystemd\nxray\nx-ui",
            Command.OS_RELEASE: 'NAME="Debian GNU/Linux"\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"',
        }
        if command == Command.X_UI:
            raise SshError("ssh_command_failed")
        return outputs[command]


def test_collector_publishes_separate_states_without_wire_input():
    times = iter((0, 5, 10))
    collector = Collector(clock=lambda: next(times))
    client = FixtureClient()
    first = collector.collect(client, x_ui_version=Version("2.0"))
    assert first.bandwidth == Bandwidth("baseline", "eth0")
    assert first.system.x_ui == Service("unknown")
    assert first.system.xray == "running"
    assert first.system.x_ui_version == Version("2.0")
    assert first.system.x_ui_version.source == "manual"
    assert first.system.xray_version is None
    assert first.system.os_release == "Debian GNU/Linux 12 (bookworm)"
    client.bytes *= 2
    client.uptime += 5
    assert collector.collect(client).bandwidth == Bandwidth("ok", "eth0", 1.6, 1.6)
    assert client.commands.count(Command.OS_RELEASE) == 1
    client.commands.clear()
    switched = collector.collect(client, "eth1")
    assert switched.bandwidth == Bandwidth("baseline", "eth1")
    assert Command.IPV4_ROUTE not in client.commands
    assert Command.IPV6_ROUTE not in client.commands
    assert switched.interfaces == ("eth0", "eth1")


def test_worker_reconnect_marks_gap_and_uses_fresh_baseline(monkeypatch):
    endpoint = Endpoint("127.0.0.1")
    pin = HostIdentity(endpoint, "ssh-rsa", "SHA256:fixture")
    clients = []
    reconnect_collected = Event()

    class Client(FixtureClient):
        def __init__(self, actual_endpoint):
            super().__init__()
            assert actual_endpoint == endpoint
            self.number = len(clients)
            clients.append(self)
            self.closed = False
            self.cycles = 0

        def connect(self, approved, auth):
            assert approved == pin

        def run(self, command):
            if command == Command.BOOT_ID:
                self.cycles += 1
                if self.number == 0 and self.cycles == 3:
                    raise SshError("ssh_disconnected")
                self.bytes += 100
            return super().run(command)

        def close(self):
            self.closed = True

        interrupt = close

    worker = MonitoringWorker(endpoint, pin, PasswordAuth("monitor", "fixture"), interval=0.05)
    publish = worker._publish

    def capture(sample):
        publish(sample)
        if len(clients) == 2 and not sample.error:
            reconnect_collected.set()

    monkeypatch.setattr(worker, "_publish", capture)
    monkeypatch.setattr(scheduler, "SshClient", Client)
    worker.start()
    try:
        assert reconnect_collected.wait(3)
    finally:
        assert worker.stop()
    samples = worker.drain()
    assert [sample.bandwidth.state for sample in samples[:4]] == ["baseline", "ok", "missing", "baseline"]
    assert samples[2].error == "ssh_disconnected"
    assert samples[2].bandwidth.rx_mbps is None
    assert samples[3].system.cpu_percent is None
    assert all(client.closed for client in clients)


def test_worker_terminates_on_changed_pin_without_retry(monkeypatch):
    endpoint = Endpoint("127.0.0.1")
    clients = []

    class ChangedHost:
        def __init__(self, _):
            clients.append(self)

        def connect(self, *_):
            raise SshError("ssh_host_key_changed")

        def close(self):
            pass

    monkeypatch.setattr(scheduler, "SshClient", ChangedHost)
    worker = MonitoringWorker(endpoint, HostIdentity(endpoint, "ssh-rsa", "SHA256:fixture"),
                              PasswordAuth("monitor", "fixture"), interval=0.05)
    worker.start()
    worker._thread.join(3)
    assert not worker._thread.is_alive()
    assert len(clients) == 1
    assert worker.drain()[0].error == "ssh_host_key_changed"
