from collections import Counter
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from proxy_manager.monitoring.ssh_client import SshError
from proxy_manager.monitoring.xray_snapshot import SNAPSHOT_SCRIPT
from proxy_manager.provider.panel_client import PanelCredentials
from proxy_manager.services.panel_geo import (
    Finding, Outbound, ReadError, classify, inspect_panel, ordinary_route, panel_read,
)


@pytest.fixture
def scenario():
    users = [f"test-user-{n:02}" for n in range(23)]
    snapshot = {
        "stage": "ok", "process": [100, 1], "started_from_file": True, "script": False,
        "disk": [["direct", "freedom", [], False], ["blocked", "blackhole", [], False]],
        "rules": [{"ip": ["geoip:private"], "outboundTag": "blocked"},
                  {"protocol": ["bittorrent"], "outboundTag": "blocked"}],
    }
    for n, name in enumerate(users[:-1], 1):
        snapshot["disk"].append([f"proxy-{n}", "socks", [[f"8.8.4.{n}", 1080]], False])
        snapshot["rules"].append({"user": [name], "outboundTag": f"proxy-{n}"})
    snapshot["runtime"] = sorted(snapshot["disk"])
    options = SimpleNamespace(error=None, changed=False, route_failure=False, before=None)
    observed = []

    def handler(req):
        path = req.url.path.split("/panel/api/")[1]
        observed.append((req.method, path))
        if path == "clients/list":
            obj = [{"id": n, "email": u, "inboundIds": [7]} for n, u in enumerate(users, 1)]
        elif path == "inbounds/list/slim":
            obj = [{"id": 7, "tag": "test-in", "enable": True, "settings": {
                "clients": [{"email": name, "enable": True} for name in users]}}]
        elif path == "xray/routeTest":
            if options.route_failure:
                return httpx.Response(503)
            form = parse_qs(req.content.decode())
            assert form["ip"][0] in {"8.8.8.8", "1.1.1.1"} and "domain" not in form
            assert form["inboundTag"] == ["test-in"]
            n = users.index(form["email"][0]) + 1
            obj = {"matched": n <= 22, "outboundTag": f"proxy-{n}" if n <= 22 else ""}
        else:
            pytest.fail("unexpected RPC method")
        return httpx.Response(200, json={"success": True, "obj": obj})

    class Pinned:
        count = 0
        def __init__(self, endpoint):
            self.endpoint = endpoint
        def connect(self, pin, auth):
            assert pin == "approved" and auth == "local-fixture"
            if options.error:
                raise SshError(options.error)
        def read_xray_snapshot(self):
            self.count += 1
            result = deepcopy(snapshot)
            if options.changed and self.count == 2:
                result["process"][1] += 1
            return json.dumps(result)
        def close(self):
            observed.append("closed")

    def run():
        return inspect_panel(PanelCredentials("https://example.test", "fixture-token"),
                             SimpleNamespace(endpoint="local-fixture"), "approved", Event(),
                             ssh_factory=Pinned, read_auth=lambda _: "local-fixture", transport=httpx.MockTransport(handler))
    return run, options, observed


def test_23_client_mapping_and_default(scenario):
    run, _, observed = scenario
    result = run()
    assert result.status == "ok"
    assert Counter(v.status for v in result.rows.values()) == {"located": 22, "default_direct": 1}
    assert len({v.ip for v in result.rows.values() if v.ip}) == 22
    assert all(v.conditional for v in result.rows.values())
    assert result.rows[23] == Finding("default_direct", conditional=True)
    assert observed.count(("POST", "xray/routeTest")) == 46
    assert observed[-1] == "closed"


@pytest.mark.parametrize("error", ["ssh_host_key_changed", "ssh_disconnected"])
def test_pinned_failure_does_not_call_panel(scenario, error):
    run, options, observed = scenario
    options.error = error
    result = run()
    assert result.status == "unavailable" and result.diagnostic == error and not result.rows
    assert observed == ["closed"]


def test_rpc_failure_and_runtime_change_do_not_return_old_rows(scenario):
    run, options, _ = scenario
    assert run().rows
    options.route_failure = True
    assert run().status == "unavailable" and not run().rows
    options.route_failure = False
    options.changed = True
    changed = run()
    assert changed.status == "changed" and not changed.rows


def test_order_and_conditional_selectors():
    u = {"user": ["a"], "outboundTag": "ordinary"}
    d = {"domain": ["domain:example.test"], "outboundTag": "other"}
    b = {"ip": ["geoip:private"], "outboundTag": "blocked"}
    assert ordinary_route([b, u, d], "a", "in") == ("ordinary", True)
    assert ordinary_route([d, u], "a", "in") == ("ambiguous", True)
    assert ordinary_route([u, d], "a", "in") == ("ordinary", False)
    assert ordinary_route([{"user": ["a"], "balancerTag": "bal"}], "a", "in") == ("ambiguous", True)


@pytest.mark.parametrize(("protocol", "servers", "chain", "status"), [
    ("freedom", (), False, "direct"), ("blackhole", (), False, "blocked"),
    ("socks", (("sample.example", 1080),), False, "domain"),
    ("http", (("[2001:4860::8888]", 8080),), False, "located"),
    ("socks", (("127.0.0.1", 1080),), False, "nonpublic"),
    ("socks", (("8.8.8.8", 1080),), True, "ambiguous"),
    ("socks", (("8.8.8.8", 1080), ("1.1.1.1", 1080)), False, "ambiguous"),
])
def test_classification(protocol, servers, chain, status):
    assert classify(Outbound(protocol, servers, chain)).status == status


@pytest.mark.parametrize("path", ["server/getConfigJson", "xray/", "xray/update", "xray/testOutbound",
                                "xray/AddOutbound", "xray/RemoveOutbound", "xray/AlterOutbound"])
def test_rpc_white_list_rejects_before_access(path):
    with pytest.raises(ReadError, match="panel_method_not_allowed"):
        panel_read(None, path)
    assert 'subprocess.run([exe,"api","lso",' in SNAPSHOT_SCRIPT
    assert not any(method in SNAPSHOT_SCRIPT for method in ("AddOutbound", "RemoveOutbound", "AlterOutbound"))


def test_route_rejects_arbitrary_context():
    with pytest.raises(ReadError, match="route_context_not_allowed"):
        panel_read(None, "xray/routeTest", data={"domain": "example.test"})
