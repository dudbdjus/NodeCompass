import base64
from contextlib import contextmanager
import hashlib
import socket
from threading import Event, Thread

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
import paramiko
import pytest

from proxy_manager.monitoring import ssh_client as ssh
from proxy_manager.monitoring.ssh_client import (Command, Endpoint, HostIdentity, HostKeyConfirmationRequired,
                                                 KeyAuth, PasswordAuth, SshClient, SshError)


_PASSWORD = "test-only-秘密"
_PASSPHRASE = "test-only-passphrase"
_CPU_OUTPUT = b"cpu 1 2 3 4 5 6 7 8\n"


class Server(paramiko.ServerInterface):
    def __init__(self, public_key, hanging=False, hanging_auth=False):
        self.public_key = public_key
        self.hanging = hanging
        self.hanging_auth = hanging_auth
        self.auth_calls = []
        self.commands = []
        self.received_command = Event()
        self.release = Event()
        self.replies = []

    def get_banner(self):
        return _PASSWORD, "en"

    def check_auth_password(self, username, password):
        self.auth_calls.append("password")
        if self.hanging_auth:
            self.release.wait(5)
        if username == "monitor" and password == _PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.auth_calls.append("publickey")
        if username == "monitor" and key == self.public_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.commands.append(command)
        self.received_command.set()
        if self.hanging:
            self.release.wait(5)
            return False

        def reply():
            # The server transport dispatches the exec acknowledgment itself.
            self.release.wait(0.04)
            if command == Command.CPU.value.encode():
                channel.sendall(_CPU_OUTPUT)
                channel.send_exit_status(0)
            else:
                channel.send_stderr(_PASSWORD.encode())
                channel.send_exit_status(1)
            channel.shutdown_write()
            channel.close()

        thread = Thread(target=reply, daemon=True)
        self.replies.append(thread)
        thread.start()
        return True


@pytest.fixture(scope="module")
def keys():
    return [paramiko.RSAKey.generate(2048) for _ in range(3)]


@contextmanager
def local_ssh(keys, *, host_key=None, hanging=False, hanging_auth=False):
    server = Server(keys[2], hanging, hanging_auth)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)

        def accept():
            conn, _ = listener.accept()
            with paramiko.Transport(conn) as transport:
                transport.add_server_key(host_key or keys[0])
                try:
                    transport.start_server(server=server)
                    channels = []
                    while transport.is_active() and not server.release.is_set():
                        channel = transport.accept(0.05)
                        if channel is not None:
                            channels.append(channel)
                except (EOFError, OSError, paramiko.SSHException):
                    pass

        thread = Thread(target=accept, daemon=True)
        thread.start()
        endpoint = Endpoint("127.0.0.1", listener.getsockname()[1])
        try:
            yield endpoint, server
        finally:
            server.release.set()
            thread.join(6)
            for reply in server.replies:
                reply.join(2)
            assert not thread.is_alive()


def test_discovery_obtains_openssh_fingerprint_without_authentication(keys):
    with local_ssh(keys) as (endpoint, server):
        client = SshClient(endpoint)
        with pytest.raises(HostKeyConfirmationRequired) as error:
            client.connect(None, PasswordAuth("monitor", _PASSWORD))
        observed = error.value.observed
        digest = base64.b64encode(hashlib.sha256(keys[0].asbytes()).digest()).decode().rstrip("=")
        assert observed == HostIdentity(endpoint, "ssh-rsa", "SHA256:" + digest)
        assert server.auth_calls == []
        assert client._socket is None


def test_matching_pin_password_and_whitelist(keys, caplog):
    caplog.set_level("DEBUG")
    with local_ssh(keys) as (endpoint, server):
        client = SshClient(endpoint)
        auth = PasswordAuth("monitor", _PASSWORD)
        try:
            client.connect(HostIdentity.observed(endpoint, keys[0]), auth)
            assert client.run(Command.CPU) == _CPU_OUTPUT.decode()
            with pytest.raises(SshError, match="^ssh_command_not_allowed$"):
                client.run("cat /proc/stat; touch /tmp/no")
            with pytest.raises(SshError, match="^ssh_command_failed$"):
                client.run(Command.X_UI)
            assert server.commands == [Command.CPU.value.encode(), Command.X_UI.value.encode()]
            assert server.auth_calls == ["password"]
        finally:
            client.close()
        assert _PASSWORD not in repr(auth) + caplog.text


def test_changed_key_refused_before_private_key_load(keys, monkeypatch):
    with local_ssh(keys, host_key=keys[1]) as (endpoint, server):
        client = SshClient(endpoint)

        def forbidden(_):
            pytest.fail("unconfirmed host must not trigger private key load")

        monkeypatch.setattr(ssh, "_load_key", forbidden)
        with pytest.raises(SshError, match="^ssh_host_key_changed$"):
            client.connect(HostIdentity.observed(endpoint, keys[0]), KeyAuth("monitor", None, _PASSPHRASE))
        assert server.auth_calls == []
        assert client._socket is None


@pytest.mark.parametrize("passphrase", [None, _PASSPHRASE, "夹具口令"])
def test_explicit_private_key_file(keys, tmp_path, passphrase):
    path = tmp_path / "test private key"
    keys[2].write_private_key_file(str(path), password=passphrase)
    with local_ssh(keys) as (endpoint, server):
        client = SshClient(endpoint)
        try:
            auth = KeyAuth("monitor", path, passphrase)
            assert str(path) not in repr(auth)
            client.connect(HostIdentity.observed(endpoint, keys[0]), auth)
            assert server.auth_calls == ["publickey"]
        finally:
            client.close()


@pytest.mark.parametrize("key_factory", [ed25519.Ed25519PrivateKey.generate,
                                         lambda: ec.generate_private_key(ec.SECP256R1())])
def test_encrypted_openssh_key_file(keys, tmp_path, key_factory):
    path = tmp_path / "user-key"
    private_key = key_factory()
    path.write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.OpenSSH,
                                               serialization.BestAvailableEncryption(_PASSPHRASE.encode())))
    auth = KeyAuth("monitor", path, _PASSPHRASE)
    public_key = ssh._load_key(auth)
    with local_ssh([*keys[:2], public_key]) as (endpoint, server):
        client = SshClient(endpoint)
        try:
            client.connect(HostIdentity.observed(endpoint, keys[0]), auth)
            assert server.auth_calls == ["publickey"]
        finally:
            client.close()


def test_bad_auth_and_redaction(keys, caplog):
    with local_ssh(keys) as (endpoint, _):
        client = SshClient(endpoint)
        wrong = "wrong-only-测试"
        with pytest.raises(SshError, match="^ssh_authentication_failed$") as error:
            client.connect(HostIdentity.observed(endpoint, keys[0]), PasswordAuth("monitor", wrong))
        assert error.value.__context__ is None
        assert wrong not in caplog.text
        assert client._socket is None


def test_connection_timeout_redacts_exception(monkeypatch):
    client = SshClient(Endpoint("127.0.0.1"))

    def timeout(*args):
        raise socket.timeout(_PASSWORD)

    monkeypatch.setattr(socket.socket, "connect", timeout)
    with pytest.raises(SshError, match="^ssh_connection_timeout$") as error:
        client.connect(None)
    assert error.value.__context__ is None
    assert client._socket is None


def test_authentication_timeout_closes_session(keys, monkeypatch):
    monkeypatch.setattr(ssh, "_AUTH_TIMEOUT", 0.4)
    with local_ssh(keys, hanging_auth=True) as (endpoint, server):
        client = SshClient(endpoint)
        with pytest.raises(SshError, match="^ssh_authentication_timeout$"):
            client.connect(HostIdentity.observed(endpoint, keys[0]), PasswordAuth("monitor", _PASSWORD))
        assert server.auth_calls == ["password"]
        assert client._socket is None


def test_command_timeout_closes_unacknowledged_exec(keys, monkeypatch):
    monkeypatch.setattr(ssh, "_COMMAND_TIMEOUT", 0.35)
    with local_ssh(keys, hanging=True) as (endpoint, server):
        client = SshClient(endpoint)
        client.connect(HostIdentity.observed(endpoint, keys[0]), PasswordAuth("monitor", _PASSWORD))
        with pytest.raises(SshError, match="^ssh_command_timeout$"):
            client.run(Command.CPU)
        assert server.received_command.is_set()
        assert client._socket is None


def test_worker_cancellation_interrupts_exec(keys):
    from proxy_manager.monitoring.scheduler import MonitoringWorker
    with local_ssh(keys, hanging=True) as (endpoint, server):
        worker = MonitoringWorker(endpoint, HostIdentity.observed(endpoint, keys[0]),
                                  PasswordAuth("monitor", _PASSWORD))
        worker.start()
        try:
            assert server.received_command.wait(4)
            assert worker.stop(timeout=2)
            assert worker._client is None
            assert worker.drain() == []
        finally:
            worker.stop()
