"""Fail closed around network access in the Windows/Python test process."""

from collections import deque
from contextlib import contextmanager
import ipaddress
import socket
import sys
from threading import local

import pytest


_DENIALS = deque()
_RESOLVING = local()
_GETADDRINFO = socket.getaddrinfo


class NetworkBlocked(AssertionError):
    """No host, URL, payload, or credential is included in the exception."""


def _deny(category):
    _DENIALS.append(category)
    raise NetworkBlocked(f"test network isolation blocked {category}")


def _loopback(host):
    if not isinstance(host, str) or "%" in host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_address(address):
    if not isinstance(address, tuple) or not address or not _loopback(address[0]):
        _deny("outbound socket")


def _audit(event, args):
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"}:
        _check_address(args[1])
    elif event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}:
        _deny("DNS")
    elif event == "socket.getaddrinfo" and not getattr(_RESOLVING, "numeric", False):
        _deny("DNS")
    elif event in {"subprocess.Popen", "os.system", "os.startfile", "os.spawn", "os.exec"}:
        _deny("external process")


def _numeric_addrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # Audit events cannot see flags. Permit only our explicitly numeric lookup.
    if (not _loopback(host) or not isinstance(port, int) or not 0 <= port <= 65535):
        _deny("DNS")
    _RESOLVING.numeric = True
    try:
        return _GETADDRINFO(host, port, family, type, proto, flags | socket.AI_NUMERICHOST | socket.AI_NUMERICSERV)
    finally:
        _RESOLVING.numeric = False


def _guard_address(method):
    def guarded(sock, *args):
        _check_address(args[-1])
        return method(sock, *args)
    return guarded


sys.addaudithook(_audit)
socket.getaddrinfo = _numeric_addrinfo
for _name in ("connect", "connect_ex", "sendto", "bind"):
    # Reject hostnames before a native socket method can resolve the address.
    setattr(socket.socket, _name, _guard_address(getattr(socket.socket, _name)))

if sys.platform == "win32":
    # The Windows Proactor bypasses socket.connect via _overlapped.ConnectEx.
    from asyncio.windows_events import IocpProactor

    _iocp_connect = IocpProactor.connect

    def _guarded_iocp_connect(self, sock, address):
        _check_address(address)
        return _iocp_connect(self, sock, address)

    IocpProactor.connect = _guarded_iocp_connect


@pytest.fixture(autouse=True)
def _no_swallowed_network_denials():
    yield
    if _DENIALS:
        categories = ", ".join(sorted(set(_DENIALS)))
        _DENIALS.clear()
        pytest.fail(f"Test attempted blocked networking or execution: {categories}", pytrace=False)


@pytest.fixture(autouse=True)
def _isolate_saved_panel_connection(monkeypatch):
    from proxy_manager.ui import panel_monitor

    # Exercise the real store separately with an ephemeral Windows credential target.
    class IsolatedPanelStore(panel_monitor.PanelCredentialStore):
        def load(self):
            return None

    monkeypatch.setattr(panel_monitor, "PanelCredentialStore", IsolatedPanelStore)


@pytest.fixture
def expected_network_denials():
    """Negative tests can assert denial without leaving a violation unaccounted for."""
    @contextmanager
    def expect(*categories):
        before = len(_DENIALS)
        yield
        assert tuple(list(_DENIALS)[before:]) == categories
        for _ in categories:
            _DENIALS.pop()

    return expect
