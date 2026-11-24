import asyncio
import socket
import sys

import httpx
import pytest


# These are reserved documentation destinations, never real provider endpoints.
_UNAVAILABLE = "https://phase4a-egress.invalid/"
_EXTERNAL_ADDRESS = ("192.0.2.1", 443)
_LOOPBACK = "127.0.0.1"


def test_dns_and_unconnected_udp_are_blocked(expected_network_denials):
    with expected_network_denials("DNS", "DNS", "DNS", "outbound socket"):
        for resolve in (
            lambda: socket.getaddrinfo("phase4a-egress.invalid", 443),
            lambda: socket.gethostbyname("phase4a-egress.invalid"),
            lambda: socket.gethostbyaddr(_LOOPBACK),
        ):
            with pytest.raises(AssertionError, match="test network isolation blocked DNS"):
                resolve()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            with pytest.raises(AssertionError, match="test network isolation blocked outbound socket"):
                udp.sendto(b"", _EXTERNAL_ADDRESS)

    assert socket.getaddrinfo(_LOOPBACK, 443, type=socket.SOCK_STREAM)[0][4] == (_LOOPBACK, 443)


def test_direct_socket_calls_and_cached_native_method_are_blocked(expected_network_denials):
    import _socket

    with socket.socket() as client, expected_network_denials("outbound socket", "outbound socket", "outbound socket"):
        for connect in (
            lambda: client.connect(("phase4a-egress.invalid", 443)),
            lambda: client.connect_ex(_EXTERNAL_ADDRESS),
            lambda: _socket.socket.connect(client, _EXTERNAL_ADDRESS),
        ):
            with pytest.raises(AssertionError, match="test network isolation blocked outbound socket"):
                connect()


def test_unmocked_httpx_async_fails_closed(expected_network_denials):
    async def request():
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            await client.get(_UNAVAILABLE)

    with expected_network_denials("DNS"), pytest.raises(AssertionError, match="test network isolation blocked DNS"):
        asyncio.run(request())


def test_respx_passthrough_fails_closed(respx_mock, expected_network_denials):
    route = respx_mock.get(_UNAVAILABLE).pass_through()
    with httpx.Client(trust_env=False, timeout=1) as client:
        with expected_network_denials("DNS"), pytest.raises(AssertionError, match="test network isolation blocked DNS"):
            client.get(_UNAVAILABLE)
    assert route.called


def test_mock_transport_falling_back_to_default_transport_is_blocked(expected_network_denials):
    with httpx.HTTPTransport(trust_env=False) as fallback:
        transport = httpx.MockTransport(fallback.handle_request)
        with httpx.Client(transport=transport, trust_env=False, timeout=1) as client:
            with expected_network_denials("DNS"), pytest.raises(AssertionError, match="test network isolation blocked DNS"):
                client.get(_UNAVAILABLE)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP")
def test_windows_proactor_cannot_bypass_socket_guard(expected_network_denials):
    async def connect():
        with socket.socket() as client:
            await asyncio.get_running_loop().sock_connect(client, _EXTERNAL_ADDRESS)

    with expected_network_denials("outbound socket"), pytest.raises(
        AssertionError, match="test network isolation blocked outbound socket"
    ):
        asyncio.run(connect())
