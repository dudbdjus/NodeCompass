from datetime import UTC, datetime
from email.utils import format_datetime
import logging
import socket
import ssl
import traceback
from urllib.parse import parse_qs

import httpcore
import httpx
import pytest

from proxy_manager.provider import kiwivm_client
from proxy_manager.provider.kiwivm_client import KiwiVMClient, KiwiVMEndpoint
from proxy_manager.provider.kiwivm_parser import KiwiVMError
from proxy_manager.security.vps_credentials import KiwiVMCredential

SECRET = "fake-kiwi-SPECIAL+&key/only"
CREDENTIAL = KiwiVMCredential("002209977", SECRET)
BASE = "https://api.64clouds.com/v1/"
SERVICE_URL = BASE + "getServiceInfo"


@pytest.mark.parametrize(("method", "endpoint", "payload"), [
    ("get_service_info", KiwiVMEndpoint.SERVICE_INFO, {"error": 0, "hostname": "fixture-vps"}),
    ("get_live_service_info", KiwiVMEndpoint.LIVE_SERVICE_INFO, {"error": 0, "ve_status": "Running"}),
    ("get_raw_usage_stats", KiwiVMEndpoint.RAW_USAGE_STATS, {"error": 0, "data": [
        {"timestamp": 1700000000, "network_in_bytes": 1234},
    ]}),
])
def test_allowed_calls_use_fixed_form_post(respx_mock, monkeypatch, method, endpoint, payload):
    route = respx_mock.post(BASE + endpoint.value).mock(return_value=httpx.Response(200, json=payload))
    flags = {}
    original = httpx.AsyncClient

    def tracked_client(**kwargs):
        flags.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(kiwivm_client.httpx, "AsyncClient", tracked_client)
    with KiwiVMClient(CREDENTIAL) as client:
        result = getattr(client, method)()
        assert client.last_success(endpoint) is result
    assert flags["verify"] is True
    assert flags["follow_redirects"] is False
    assert flags["trust_env"] is False
    assert flags["timeout"].read > 15
    assert result.fetched_at_utc.tzinfo is UTC
    [call] = route.calls
    assert call.request.method == "POST"
    assert not call.request.url.query
    assert SECRET not in str(call.request.url)
    assert call.request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(call.request.content.decode("ascii")) == {
        "veid": ["2209977"], "api_key": [SECRET],
    }


@pytest.mark.parametrize("operation", [
    "start", "restart", "stop", "kill", "snapshot/create", "snapshot/delete", "snapshot/restore",
    "reinstallOS", "basicShell/exec", "shellScript/exec", "setHostname", "setPTR", "migrate/start",
    "https://example.invalid/getServiceInfo", "../restart", "getServiceInfo", None,
])
def test_forbidden_endpoints_make_no_request(respx_mock, operation):
    with KiwiVMClient(CREDENTIAL) as client:
        with pytest.raises(KiwiVMError, match="endpoint_not_allowed"):
            client._fetch(operation, lambda *_: None)
        with pytest.raises(KiwiVMError, match="endpoint_not_allowed"):
            client._post(operation)
    assert not respx_mock.calls


@pytest.mark.parametrize(("response", "kind"), [
    (httpx.Response(200, json={"error": 1, "message": "Invalid API key"}), "invalid_api_key"),
    (httpx.Response(200, json={"error": 2, "message": "Invalid VEID"}), "invalid_veid"),
    (httpx.Response(200, json={"error": 2, "message": "Invalid VEID or API key"}), "authentication_failed"),
    (httpx.Response(200, json={"error": 99, "message": SECRET}), "api_error"),
    (httpx.Response(200, json={"hostname": "fixture"}), "invalid_response"),
    (httpx.Response(200, json={"error": 0, "email": SECRET}), "invalid_response"),
    (httpx.Response(200, text=SECRET), "invalid_response"),
    (httpx.Response(401, text=SECRET), "authentication_failed"),
    (httpx.Response(403, text=SECRET), "authentication_failed"),
    (httpx.Response(302, headers={"Location": "https://example.invalid/collect"}, text=SECRET), "http_error"),
])
def test_errors_are_safe_and_do_not_follow_redirects(respx_mock, caplog, response, kind):
    route = respx_mock.post(SERVICE_URL).mock(return_value=response)
    with caplog.at_level(logging.DEBUG), KiwiVMClient(CREDENTIAL) as client:
        with pytest.raises(KiwiVMError) as failure:
            client.get_service_info()
        assert client.last_success(KiwiVMEndpoint.SERVICE_INFO) is None
    assert route.call_count == len(respx_mock.calls) == 1
    assert failure.value.kind == kind
    assert failure.value.__cause__ is None and failure.value.__context__ is None
    assert SECRET not in "".join(traceback.format_exception(failure.value))
    assert SECRET not in caplog.text


def test_key_is_redacted_from_allowed_text_fields(respx_mock, caplog):
    respx_mock.post(SERVICE_URL).mock(return_value=httpx.Response(200, json={
        "error": 0, "hostname": f"echo {SECRET}", "node_alias": SECRET, "plan_ram": 1024,
        "email": "not-for-storage@example.invalid",
    }))
    with caplog.at_level(logging.DEBUG), KiwiVMClient(CREDENTIAL) as client:
        result = client.get_service_info()
    assert result.data.hostname is None and result.data.node_alias is None
    assert result.data.plan_ram == 1024
    assert SECRET not in repr(result) + repr(CREDENTIAL) + caplog.text
    assert "not-for-storage" not in repr(result)


def test_retry_after_and_server_error_recover(respx_mock):
    route = respx_mock.post(SERVICE_URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(500),
        httpx.Response(200, json={"error": 0, "hostname": "fixture-vps"}),
    ])
    waits = []
    with KiwiVMClient(CREDENTIAL, wait=waits.append) as client:
        assert client.get_service_info().data.hostname == "fixture-vps"
    assert waits == [3, 2]
    assert route.call_count == 3


def test_http_date_retry_after(monkeypatch, respx_mock):
    instant = datetime(2026, 11, 21, 8, tzinfo=UTC)

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(kiwivm_client, "datetime", FixedTime)
    route = respx_mock.post(SERVICE_URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": format_datetime(instant.replace(second=10), usegmt=True)}),
        httpx.Response(200, json={"error": 0, "hostname": "fixture-vps"}),
    ])
    waits = []
    with KiwiVMClient(CREDENTIAL, wait=waits.append) as client:
        client.get_service_info()
    assert waits == [10.0]
    assert route.call_count == 2


@pytest.mark.parametrize(("status", "header", "attempts", "kind"), [
    (429, "120", 1, "rate_limited"),
    (429, "invalid", 3, "rate_limited"),
    (500, "", 3, "http_error"),
])
def test_retry_exhaustion_keeps_last_success(respx_mock, status, header, attempts, kind):
    route = respx_mock.post(SERVICE_URL).mock(side_effect=[
        httpx.Response(200, json={"error": 0, "hostname": "fixture-vps"}),
        *[httpx.Response(status, headers={"Retry-After": header}) for _ in range(attempts)],
    ])
    waits = []
    with KiwiVMClient(CREDENTIAL, wait=waits.append) as client:
        success = client.get_service_info()
        with pytest.raises(KiwiVMError, match=kind):
            client.get_service_info()
        assert client.last_success(KiwiVMEndpoint.SERVICE_INFO) is success
    assert route.call_count == attempts + 1
    assert len(waits) == attempts - 1


@pytest.mark.parametrize(("exception_type", "cause", "kind", "attempts"), [
    (httpcore.ReadTimeout, None, "timeout", 3),
    (httpcore.ConnectError, socket.gaierror, "dns_error", 3),
    (httpcore.ConnectError, ssl.SSLCertVerificationError, "tls_error", 1),
    (httpcore.ConnectError, None, "transport_error", 3),
])
def test_transport_error_types_are_redacted(monkeypatch, exception_type, cause, kind, attempts):
    from httpcore._backends.anyio import AnyIOBackend

    calls = []

    async def offline_connect(self, host, port, **options):
        calls.append((host, port))
        if cause:
            original = cause(SECRET)
            raise exception_type(original) from original
        raise exception_type(SECRET)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", offline_connect)
    with KiwiVMClient(CREDENTIAL, wait=lambda _: None) as client:
        with pytest.raises(KiwiVMError) as failure:
            client.get_service_info()
    assert failure.value.kind == kind
    assert failure.value.__context__ is None
    assert SECRET not in "".join(traceback.format_exception(failure.value))
    assert calls == [("api.64clouds.com", 443)] * attempts


def test_cyclic_mock_transport_error_does_not_stall(respx_mock):
    route = respx_mock.post(SERVICE_URL).mock(side_effect=httpx.ReadTimeout(SECRET))
    with KiwiVMClient(CREDENTIAL, wait=lambda _: None) as client:
        with pytest.raises(KiwiVMError, match="timeout"):
            client.get_service_info()
    assert route.call_count == 3


def test_unknown_history_is_reported_and_preserves_last_good_history(respx_mock):
    route = respx_mock.post(BASE + "getRawUsageStats").mock(side_effect=[
        httpx.Response(200, json={"error": 0, "data": [{"timestamp": 1700000000, "network_in_bytes": 32}]}),
        httpx.Response(200, json={"error": 0, "data": [[1700000000, 42, SECRET]]}),
    ])
    with KiwiVMClient(CREDENTIAL) as client:
        valid = client.get_raw_usage_stats()
        unknown = client.get_raw_usage_stats()
        assert unknown.data.layout == "unknown"
        assert unknown.data.input_rows == 1
        assert client.last_success(KiwiVMEndpoint.RAW_USAGE_STATS) is valid
        assert SECRET not in repr(unknown)
    assert route.call_count == 2
