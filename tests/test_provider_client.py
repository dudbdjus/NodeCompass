import logging

import httpx
import pytest

from proxy_manager.provider.client import ProxySellerClient
from proxy_manager.provider.errors import ProviderError

KEY = "fixture-only-secret"
URL = f"https://proxy-seller.com/personal/api/v2/{KEY}/proxy/list/isp?page=1&per_page=100"
VALID = {
    "status": "success",
    "errors": [],
    "data": {
        "items": [{"id": "1", "ip_only": "192.0.2.1", "port_http": 8080, "password": "test-secret"}],
        "page": 1, "per_page": 100, "total": 1, "total_pages": 1,
    },
}


def test_get_isp_page(respx_mock, caplog):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, json=VALID))
    with caplog.at_level(logging.DEBUG), ProxySellerClient() as client:
        result = client.list_isp(KEY, 1)
    assert route.call_count == 1
    assert result.total == 1
    assert result.items[0].address == "192.0.2.1"
    assert "test-secret" not in repr(result)
    assert KEY not in caplog.text


@pytest.mark.parametrize(("response", "kind"), [
    (httpx.Response(200, json={"status": "error", "errors": [{"message": KEY}]}), "business_error"),
    (httpx.Response(200, text=KEY), "invalid_response"),
    (httpx.Response(200, json={"status": "success", "errors": [], "data": {"items": []}}), "invalid_response"),
    (httpx.Response(302, headers={"Location": "https://example.invalid/"}), "http_error"),
    (httpx.Response(403, text=KEY), "http_error"),
])
def test_safe_http_and_business_errors(respx_mock, response, kind):
    route = respx_mock.get(URL).mock(return_value=response)
    with ProxySellerClient() as client, pytest.raises(ProviderError) as error:
        client.list_isp(KEY, 1)
    assert route.call_count == 1
    assert error.value.kind == kind
    assert KEY not in str(error.value)
    assert error.value.__context__ is None


def test_transport_error_has_no_sensitive_context(respx_mock):
    respx_mock.get(URL).mock(side_effect=httpx.ConnectError(KEY))
    with ProxySellerClient() as client, pytest.raises(ProviderError) as error:
        client.list_isp(KEY, 1)
    assert error.value.kind == "transport_error"
    assert error.value.__context__ is None
    assert KEY not in str(error.value)


def test_429_wait_then_success(respx_mock):
    route = respx_mock.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(429, headers={"Retry-After": "invalid"}),
        httpx.Response(200, json=VALID),
    ])
    waits = []
    with ProxySellerClient(wait=waits.append) as client:
        assert client.list_isp(KEY, 1).total == 1
    assert route.call_count == 3
    assert waits == [3, 2]


@pytest.mark.parametrize(("header", "calls"), [("", 4), ("120", 1)])
def test_429_is_bounded(respx_mock, header, calls):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": header}))
    with ProxySellerClient(wait=lambda _: None) as client, pytest.raises(ProviderError) as error:
        client.list_isp(KEY, 1)
    assert error.value.kind == "rate_limited"
    assert route.call_count == calls

def _response_with_ports(http_port, socks_port):
    record = {**VALID["data"]["items"][0], "port_http": http_port, "port_socks": socks_port}
    return httpx.Response(200, json={
        **VALID, "data": {**VALID["data"], "items": [record]},
    })


def test_live_wire_format_of_isp_ports(respx_mock):
    respx_mock.get(URL).mock(return_value=_response_with_ports("8080", "1080"))
    with ProxySellerClient() as client:
        record = client.list_isp(KEY, 1).items[0]
    assert (record.port_http, record.port_socks) == (8080, 1080)


@pytest.mark.parametrize("port", ["0", "65536", " 8080", "１２３４", "1.0", "", False])
def test_rejects_invalid_isp_port(respx_mock, port):
    respx_mock.get(URL).mock(return_value=_response_with_ports(port, 1080))
    with ProxySellerClient() as client, pytest.raises(ProviderError) as error:
        client.list_isp(KEY, 1)
    assert error.value.kind == "invalid_response"
