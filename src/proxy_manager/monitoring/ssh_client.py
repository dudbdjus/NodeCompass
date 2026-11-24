import base64
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import hmac
import ipaddress
import logging
from pathlib import Path
import socket
from threading import Event, Lock, Timer

import paramiko


class Command(Enum):
    IPV4_ROUTE = "ip -j route show default"
    IPV6_ROUTE = "ip -j -6 route show default"
    LINKS = "ip -j -s -d link show"
    BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
    CPU = "cat /proc/stat"
    MEMORY = "cat /proc/meminfo"
    LOAD = "cat /proc/loadavg"
    UPTIME = "cat /proc/uptime"
    ROOT_DISK = "LC_ALL=C df -P -B1 /"
    X_UI = "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result,ExecMainStatus x-ui.service"
    PROCESSES = "LC_ALL=C ps -eo comm"
    OS_RELEASE = "cat /etc/os-release 2>/dev/null || cat /usr/lib/os-release"


class SshError(RuntimeError):
    """Contains only a diagnostic identifier defined by this module."""


class HostKeyConfirmationRequired(SshError):
    def __init__(self, observed: "HostIdentity"):
        super().__init__("ssh_host_confirmation_required")
        self.observed = observed


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int = 22

    def __post_init__(self):
        try:
            host = ipaddress.ip_address(self.host)
            if type(self.host) is str and (type(self.port) is int and 1 <= self.port <= 65535):
                object.__setattr__(self, "host", str(host))
                return
        except ValueError:
            pass
        raise ValueError("ssh_invalid_endpoint")


@dataclass(frozen=True)
class HostIdentity:
    endpoint: Endpoint
    algorithm: str
    sha256: str

    @classmethod
    def observed(cls, endpoint: Endpoint, key: paramiko.PKey) -> "HostIdentity":
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii")
        return cls(endpoint, key.get_name(), "SHA256:" + digest.rstrip("="))

    def matches(self, observed: "HostIdentity") -> bool:
        return (self.endpoint == observed.endpoint and self.algorithm == observed.algorithm
                and hmac.compare_digest(self.sha256.encode("utf-8"), observed.sha256.encode("ascii")))


@dataclass(frozen=True)
class PasswordAuth:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class KeyAuth:
    username: str = field(repr=False)
    path: Path = field(repr=False)
    passphrase: str | None = field(default=None, repr=False)


# Paramiko can log server-supplied text during negotiation and authentication.
# Suppress only this client's dedicated logger, including setup before start_client.
_TRANSPORT_LOG = __name__ + ".transport"
logging.getLogger(_TRANSPORT_LOG).disabled = True
_CONNECT_TIMEOUT = 8
_AUTH_TIMEOUT = 8
_COMMAND_TIMEOUT = 5
_OUTPUT_LIMIT = 1024 * 1024


def _load_key(auth: KeyAuth) -> paramiko.PKey:
    for key_type in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_type.from_private_key_file(str(auth.path), password=auth.passphrase)
        except (paramiko.SSHException, ValueError, TypeError):
            continue
        except OSError:
            break
    raise SshError("ssh_private_key_unavailable")


class SshClient:
    """A single SSH session. close() may interrupt it from another thread."""

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self._lock = Lock()
        self._closed = Event()
        self._socket: socket.socket | None = None
        self._transport: paramiko.Transport | None = None
        self._started = False

    def interrupt(self) -> None:
        """Unblock network I/O without joining Paramiko from the caller's thread."""
        with self._lock:
            self._closed.set()
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self) -> None:
        self.interrupt()
        with self._lock:
            transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def _handshake(self) -> paramiko.Transport:
        family = socket.AF_INET6 if ":" in self.endpoint.host else socket.AF_INET
        with self._lock:
            if self._closed.is_set() or self._started:
                raise SshError("ssh_session_closed")
            self._started = True
            sock = self._socket = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect((self.endpoint.host, self.endpoint.port))
        with self._lock:
            if self._closed.is_set():
                raise SshError("ssh_session_closed")
            transport = self._transport = paramiko.Transport(sock)
            transport.set_log_channel(_TRANSPORT_LOG)
        transport.banner_timeout = _CONNECT_TIMEOUT
        # start_client's timeout may return before negotiation finished.
        transport.handshake_timeout = _CONNECT_TIMEOUT
        transport.start_client(timeout=_CONNECT_TIMEOUT)
        return transport

    def _authenticate(self, transport: paramiko.Transport, auth: PasswordAuth | KeyAuth) -> None:
        completed = Event()
        if type(auth) is PasswordAuth:
            transport.auth_password(auth.username, auth.password, event=completed, fallback=False)
        elif type(auth) is KeyAuth:
            transport.auth_publickey(auth.username, _load_key(auth), event=completed)
        else:
            raise SshError("ssh_invalid_authentication")
        if not completed.wait(_AUTH_TIMEOUT):
            raise SshError("ssh_authentication_timeout")
        if not transport.is_active():
            raise SshError("ssh_disconnected")
        if not transport.is_authenticated():
            raise SshError("ssh_authentication_failed")

    def connect(self, approved: HostIdentity | None, auth: PasswordAuth | KeyAuth | None = None) -> None:
        """Use None to discover the fingerprint, then use a fresh session after approval."""
        connected = False
        try:
            transport = self._handshake()
            if not transport.initial_kex_done:
                raise SshError("ssh_negotiation_timeout")
            observed = HostIdentity.observed(self.endpoint, transport.get_remote_server_key())
            if approved is None:
                raise HostKeyConfirmationRequired(observed)
            if not approved.matches(observed):
                raise SshError("ssh_host_key_changed")
            self._authenticate(transport, auth)
            connected = True
            return
        except SshError:
            raise
        except TimeoutError:
            code = "ssh_connection_timeout"
        except paramiko.AuthenticationException:
            code = "ssh_authentication_failed"
        except (paramiko.SSHException, EOFError, OSError, ValueError, TypeError):
            code = "ssh_connection_failed"
        finally:
            if not connected:
                self.close()
        raise SshError(code)

    def run(self, command: Command) -> str:
        if type(command) is not Command:
            raise SshError("ssh_command_not_allowed")
        return self._run(command.value)

    def read_xray_snapshot(self) -> str:
        from .xray_snapshot import SNAPSHOT_SCRIPT
        return self._run("python3 -I -B -", SNAPSHOT_SCRIPT.encode("utf-8"), timeout=12)

    def _run(self, command: str, data: bytes | None = None, *, timeout=_COMMAND_TIMEOUT) -> str:
        with self._lock:
            transport = self._transport
        if transport is None or not transport.is_authenticated():
            raise SshError("ssh_disconnected")

        expired = Event()

        def interrupt():
            expired.set()
            self.close()

        timer = Timer(timeout, interrupt)
        timer.daemon = True
        timer.start()
        channel = None
        try:
            channel = transport.open_session(timeout=timeout)
            channel.settimeout(timeout)
            channel.exec_command(command)
            if data is not None:
                channel.sendall(data)
                channel.shutdown_write()
            output = bytearray()
            output_size = 0
            while not self._closed.is_set():
                if channel.recv_ready():
                    block = channel.recv(32768)
                    output.extend(block)
                    output_size += len(block)
                if channel.recv_stderr_ready():
                    output_size += len(channel.recv_stderr(32768))
                if output_size > _OUTPUT_LIMIT:
                    raise SshError("ssh_command_output_limit")
                if (channel.closed or channel.eof_received) and channel.exit_status_ready():
                    if channel.recv_ready() or channel.recv_stderr_ready():
                        continue
                    status = channel.recv_exit_status()
                    if status < 0:
                        break
                    if status != 0:
                        raise SshError("ssh_command_failed")
                    if not expired.is_set() and not self._closed.is_set():
                        return output.decode("utf-8")
                    break
                self._closed.wait(0.01)
        except UnicodeError:
            raise SshError("ssh_command_invalid_output") from None
        except TimeoutError:
            expired.set()
            self.close()
        except (paramiko.SSHException, EOFError, OSError):
            pass
        finally:
            timer.cancel()
            if channel is not None:
                channel.close()
        raise SshError("ssh_command_timeout" if expired.is_set() else "ssh_disconnected")
