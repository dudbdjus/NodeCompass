from uuid import uuid4

import httpx
import pytest
import win32cred

from proxy_manager.provider.panel_client import PanelCredentials, PanelError, fetch_all_clients, fetch_clients, fetch_status, fetch_subscription_url, parse_status
from proxy_manager.security.panel_credentials import PanelCredentialStore


@pytest.fixture
def credentials():
    return PanelCredentials("https://example.test:2443/private-prefix/", "fixture-panel-token")


def test_3x_ui_status_uses_only_the_configured_https_route(credentials, caplog):
    def response(request):
        assert request.method == "GET"
        assert str(request.url) == "https://example.test:2443/private-prefix/panel/api/server/status"
        assert request.headers["authorization"] == "Bearer fixture-panel-token"
        return httpx.Response(200, json={"success": True, "obj": {
            "cpu": 5.1,
            "mem": {"current": 25, "total": 100}, "swap": {"current": 0, "total": 0},
            "disk": {"current": 3, "total": 10},
            "netIO": {"up": 125000, "down": 250000},
            "netTraffic": {"sent": 99999999, "recv": 99999999},
            "tcpCount": 71, "udpCount": 3, "panelVersion": "3.6.0",
            "xray": {"state": "running", "version": "v26.7.28"},
        }})

    result = fetch_status(credentials, transport=httpx.MockTransport(response))
    assert (result.cpu, result.memory.percent, result.swap.percent, result.disk.percent) == (5.1, 25, None, 30)
    assert (result.upload, result.download, result.sockets, result.xray_state) == (125000, 250000, 74, "running")
    assert "fixture-panel-token" not in repr(credentials) + caplog.text
    assert "private-prefix" not in repr(credentials) + caplog.text


@pytest.mark.parametrize(("code", "reason"), [(302, "redirect"), (401, "auth"), (404, "path"), (429, "rate")])
def test_3x_ui_errors_do_not_follow_redirects_or_use_response_text(credentials, code, reason):
    calls = []

    def response(request):
        calls.append(request.url.host)
        return httpx.Response(code, headers={"location": "https://elsewhere.test/"}, text="hostile credential echo")

    with pytest.raises(PanelError, match=f"^{reason}$"):
        fetch_status(credentials, transport=httpx.MockTransport(response))
    assert calls == ["example.test"]


@pytest.mark.parametrize("url", [
    "http://example.test", "https://one@example.test", "https://example.test/path?query",
    "https://example.test/path/../", "https://example.test/panel#frag",
])
def test_panel_rejects_insecure_or_ambiguous_addresses(url):
    with pytest.raises(PanelError, match="^address$"):
        PanelCredentials(url, "fixture")


def test_panel_distinguishes_missing_metrics_from_zero():
    status = parse_status({"success": True, "obj": {
        "cpu": 0, "tcpCount": 0, "udpCount": True, "netIO": {"up": 0, "down": -1},
    }})
    assert status.cpu == status.tcp == status.upload == 0
    assert status.udp is status.sockets is status.download is status.memory.total is None


def test_panel_credentials_round_trip_with_dpapi(credentials):
    target = f"ProxyManager/test/3x-ui/{uuid4().hex}"
    store = PanelCredentialStore(target)
    try:
        assert store.load() is None
        store.save(credentials)
        record = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        assert "fixture-panel-token" not in record["CredentialBlob"].decode("utf-16-le")
        assert "private-prefix" not in record["CredentialBlob"].decode("utf-16-le")
        assert store.load() == credentials
    finally:
        store.delete()
    assert store.load() is None


def test_client_page_keeps_online_distinct_from_enabled(credentials):
    def response(request):
        assert request.method == "GET"
        assert request.url.path == "/private-prefix/panel/api/clients/list/paged"
        assert request.url.params["page"] == "2"
        return httpx.Response(200, json={"success": True, "obj": {
            "items": [{"email": "fixture-08", "enable": False, "group": "group",
                       "subId": "private-sub", "comment": "首尔备注", "traffic": {"up": 32, "down": 64}},
                      {"email": "another-08", "enable": True}],
            "summary": {"online": ["fixture-08"], "onlineCount": 400},
            "page": 2, "pageSize": 50, "filtered": 88,
        }})

    page = fetch_clients(credentials, 2, transport=httpx.MockTransport(response))
    assert (page.page, page.total, page.page_size) == (2, 88, 50)
    online, unknown = page.rows
    assert (online.name, online.online, online.up, online.down) == ("fixture-08", True, 32, 64)
    assert online.sub_id == "private-sub"
    assert online.comment == "首尔备注"
    assert "private-sub" not in repr(page)
    assert unknown.online is None


def test_all_client_pages_are_loaded_for_local_sorting(credentials):
    calls = []

    def response(request):
        page = int(request.url.params["page"])
        calls.append(page)
        names = [f"ISP-{number}" for number in (range(1, 201) if page == 1 else range(201, 203))]
        return httpx.Response(200, json={"success": True, "obj": {
            "items": [{"email": name} for name in names], "filtered": 202, "page": page, "pageSize": 200,
        }})

    result = fetch_all_clients(credentials, transport=httpx.MockTransport(response))
    assert calls == [1, 2]
    assert result.total == len(result.rows) == 202
    assert result.page_size == 50
    assert result.rows[-1].name == "ISP-202"


def test_empty_online_summary_marks_offline(credentials):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"success": True, "obj": {
        "items": [{"email": "fixture", "enable": True}], "summary": {"online": [], "onlineCount": 0},
        "page": 1, "pageSize": 50, "filtered": 1,
    }}))
    assert fetch_clients(credentials, transport=transport).rows[0].online is False


def test_subscription_uses_computed_address_without_requesting_subscription_host(credentials):
    def response(request):
        assert request.method == "POST"
        assert request.url.path == "/private-prefix/panel/api/setting/defaultSettings"
        assert request.url.host == "example.test"
        return httpx.Response(200, json={"success": True, "obj": {
            "subEnable": True, "subURI": "https://subscribe.test:2096/custom/",
        }})

    url = fetch_subscription_url(credentials, "fixture/segment", transport=httpx.MockTransport(response))
    assert url == "https://subscribe.test:2096/custom/fixture%2Fsegment"


def test_subscription_disabled(credentials):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"success": True, "obj": {
        "subEnable": False, "subURI": "https://subscribe.test/sub/",
    }}))
    with pytest.raises(PanelError, match="^subscription$"):
        fetch_subscription_url(credentials, "fixture", transport=transport)
