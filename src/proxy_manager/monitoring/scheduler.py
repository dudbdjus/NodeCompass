from collections import deque
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
import math
from time import monotonic, time

from .network import Bandwidth, BandwidthSampler, parse_links, select_link
from .ssh_client import Command, Endpoint, HostIdentity, KeyAuth, PasswordAuth, SshClient, SshError
from .system import (CpuSampler, SystemMetrics, Version, parse_boot_id, parse_disk, parse_load,
                     parse_memory, parse_os_release, parse_service, parse_uptime, parse_xray)


@dataclass(frozen=True)
class Sample:
    monotonic_time: float
    bandwidth: Bandwidth
    system: SystemMetrics | None
    interfaces: tuple[str, ...] = ()
    error: str | None = None
    captured: float = field(default_factory=time)


_UNAVAILABLE = {"ssh_command_failed", "ssh_command_output_limit", "ssh_command_invalid_output"}
_RETRYABLE = {"ssh_connection_timeout", "ssh_negotiation_timeout", "ssh_connection_failed",
              "ssh_disconnected", "ssh_command_timeout", "ssh_authentication_timeout"}


class Collector:
    def __init__(self, interval: float = 5, clock=monotonic):
        self._clock = clock
        self._bandwidth = BandwidthSampler(max_gap=3 * interval)
        self._cpu = CpuSampler()
        self._boot: tuple[str | None, float | None] | None = None
        self._os_release: str | None = None
        self._os_checked = False

    def reset_network(self) -> None:
        self._bandwidth.reset()

    def collect(self, client: SshClient, interface: str | None = None,
                x_ui_version: Version | None = None, xray_version: Version | None = None) -> Sample:
        def read(command: Command) -> str | None:
            try:
                return client.run(command)
            except SshError as error:
                if str(error) not in _UNAVAILABLE:
                    raise
                return None

        boot = parse_boot_id(read(Command.BOOT_ID))
        ipv4 = read(Command.IPV4_ROUTE) if interface is None else None
        ipv6 = read(Command.IPV6_ROUTE) if interface is None else None
        links = parse_links(read(Command.LINKS))
        now, captured = self._clock(), time()
        uptime = parse_uptime(read(Command.UPTIME))
        if self._boot is not None:
            old_boot, old_uptime = self._boot
            if boot != old_boot or (uptime is not None and old_uptime is not None and uptime < old_uptime):
                self._cpu.reset()
                self._os_release = None
                self._os_checked = False
        if not self._os_checked:
            self._os_release = parse_os_release(read(Command.OS_RELEASE))
            self._os_checked = True
        self._boot = (boot, uptime)
        link = select_link(links, ipv4, ipv6, interface)
        bandwidth = self._bandwidth.sample(link, now, boot, uptime)
        cpu = self._cpu.sample(read(Command.CPU))
        memory, swap = parse_memory(read(Command.MEMORY))
        system = SystemMetrics(cpu, memory, swap, parse_disk(read(Command.ROOT_DISK)),
                               parse_load(read(Command.LOAD)), uptime, parse_service(read(Command.X_UI)),
                               parse_xray(read(Command.PROCESSES)), x_ui_version, xray_version, self._os_release)
        return Sample(now, bandwidth, system, tuple(sorted(links)), captured=captured)


def _network_gap(code: str) -> Sample:
    return Sample(monotonic(), Bandwidth("missing"), None, error=code)


class MonitoringWorker:
    """Bounded in-memory samples from one verified endpoint, with no UI or database side effects."""

    def __init__(self, endpoint: Endpoint, approved: HostIdentity, auth: PasswordAuth | KeyAuth,
                 *, interface: str | None = None, interval: float = 5,
                 x_ui_version: Version | None = None, xray_version: Version | None = None):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("ssh_invalid_sampling_interval")
        if type(approved) is not HostIdentity or approved.endpoint != endpoint:
            raise SshError("ssh_host_confirmation_required")
        self._endpoint = endpoint
        self._approved = approved
        self._auth = auth
        self._interface = interface
        self._interval = interval
        self._versions = (x_ui_version, xray_version)
        self._state_lock = Lock()
        self._stop = Event()
        self._wake = Event()
        self._interface_revision = 0
        self._client: SshClient | None = None
        self._thread: Thread | None = None
        self._samples: deque[Sample] = deque(maxlen=512)

    def set_interface(self, name: str | None) -> None:
        with self._state_lock:
            if name != self._interface:
                self._interface = name
                self._interface_revision += 1
                self._wake.set()

    def refresh_now(self):
        self._wake.set()

    def interrupt_connection(self) -> bool:
        """Close the current client socket and let normal reconnection run."""
        with self._state_lock:
            client = self._client
        if client is None or self._stop.is_set():
            return False
        client.interrupt()
        self._wake.set()
        return True

    def drain(self) -> list[Sample]:
        with self._state_lock:
            samples = list(self._samples)
            self._samples.clear()
        return samples

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = Thread(target=self._run, name="vps-ssh-monitor", daemon=True)
            self._thread.start()

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._state_lock:
            client = self._client
        if client is not None:
            client.interrupt()

    def stop(self, timeout: float = 10) -> bool:
        """Cancel the active connection and return whether the worker has exited."""
        self.request_stop()
        with self._state_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.running

    def _publish(self, sample: Sample) -> None:
        with self._state_lock:
            if not self._stop.is_set():
                self._samples.append(sample)

    def _run(self) -> None:
        retry_delay = self._interval
        attempts = 0
        while not self._stop.is_set():
            client = SshClient(self._endpoint)
            with self._state_lock:
                if self._stop.is_set():
                    break
                self._client = client
            try:
                client.connect(self._approved, self._auth)
                collector = Collector(self._interval)
                selection = None
                while not self._stop.is_set():
                    started = monotonic()
                    with self._state_lock:
                        interface = self._interface
                        revision = self._interface_revision
                    if selection != revision:
                        collector.reset_network()
                        selection = revision
                    self._publish(collector.collect(client, interface, *self._versions))
                    retry_delay = self._interval
                    attempts = 0
                    self._wake.wait(max(0, self._interval - (monotonic() - started)))
                    self._wake.clear()
            except SshError as error:
                code = str(error)
                attempts += 1
                exhausted = code in _RETRYABLE and attempts >= 5
                self._publish(_network_gap("ssh_retry_exhausted" if exhausted else code))
                if code not in _RETRYABLE or exhausted:
                    return
            finally:
                client.close()
                with self._state_lock:
                    self._client = None
            self._stop.wait(retry_delay)
            retry_delay = min(60, retry_delay * 2)
