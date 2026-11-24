from contextlib import nullcontext
from dataclasses import dataclass, field
import json
import re
from time import monotonic
from urllib.parse import quote, urlsplit

import httpx

from proxy_manager.logging_config import suppress_http_diagnostics


class PanelError(ValueError):
    pass


@dataclass(frozen=True)
class PanelClient:
    name: str
    group: str | None = None
    online: bool | None = None
    up: int | None = None
    down: int | None = None
    sub_id: str | None = field(default=None, repr=False)
    comment: str | None = None
    record_id: int | None = None


@dataclass(frozen=True)
class PanelRows:
    rows: tuple[PanelClient, ...]
    total: int
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True)
class PanelCredentials:
    base_url: str = field(repr=False)
    token: str = field(repr=False)

    def __post_init__(self):
        try:
            address = urlsplit(self.base_url)
            valid = (
                address.scheme == "https" and bool(address.hostname) and address.username is None
                and address.password is None and "#" not in self.base_url and "?" not in self.base_url
                and not any(char.isspace() or ord(char) < 32 for char in self.base_url)
                and re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", address.path)
                and not {".", ".."}.intersection(address.path.split("/"))
                and (address.port is None or 1 <= address.port <= 65535)
                and "\\" not in address.netloc
            )
            if valid:
                httpx.URL(self.base_url)
        except (ValueError, TypeError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise PanelError("address")
        if not isinstance(self.token, str) or not re.fullmatch(r"[\x21-\x7e]+", self.token):
            raise PanelError("token")


@dataclass(frozen=True)
class Capacity:
    used: int | None = None
    total: int | None = None

    @property
    def percent(self):
        return 100 * self.used / self.total if self.total and self.used is not None else None


@dataclass(frozen=True)
class PanelStatus:
    cpu: int | float | None
    memory: Capacity
    swap: Capacity
    disk: Capacity
    upload: int | None
    download: int | None
    tcp: int | None
    udp: int | None
    panel_version: str | None
    xray_version: str | None
    xray_state: str | None

    @property
    def sockets(self):
        return self.tcp + self.udp if self.tcp is not None and self.udp is not None else None


def _number(value):
    return value if type(value) is int and 0 <= value < 2**64 else None


def _section(data, key):
    result = data.get(key)
    return result if isinstance(result, dict) else {}


def _capacity(data, key):
    obj = _section(data, key)
    return Capacity(_number(obj.get("current")), _number(obj.get("total")))


def _version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[vV]?[0-9][A-Za-z0-9.+_-]{0,47}", value) else None


def parse_status(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("obj"), dict):
        raise PanelError("response")
    data = payload["obj"]
    net = _section(data, "netIO")
    xray = _section(data, "xray")
    state = xray.get("state")
    cpu = data.get("cpu")
    return PanelStatus(
        cpu if type(cpu) in (float, int) and 0 <= cpu <= 100 else None,
        _capacity(data, "mem"), _capacity(data, "swap"), _capacity(data, "disk"),
        _number(net.get("up")), _number(net.get("down")),
        _number(data.get("tcpCount")), _number(data.get("udpCount")),
        _version(data.get("panelVersion")), _version(xray.get("version")),
        state if state in ("running", "stop", "error") else None,
    )


def fetch_status(credentials: PanelCredentials, *, transport=None) -> PanelStatus:
    return parse_status(_get_json(credentials, "server/status", transport=transport))


def fetch_clients(credentials: PanelCredentials, page=1, *, page_size=50, transport=None) -> PanelRows:
    if type(page) is not int or page < 1:
        raise PanelError("response")
    params = {"page": page, "pageSize": page_size, "sort": "email", "order": "ascend"}
    payload = _get_json(credentials, "clients/list/paged", params=params, max_bytes=512 * 1024, transport=transport)
    data = _response_object(payload)
    count = _number(data.get("filtered"))
    page_size = _number(data.get("pageSize"))
    page = _number(data.get("page"))
    if not page or not page_size or count is None:
        raise PanelError("response")
    items = data.get("items")
    if items is None and count == 0:
        items = []
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise PanelError("response")
    summary = _section(data, "summary")
    names = summary.get("online")
    online_names = {name for name in names if isinstance(name, str)} if isinstance(names, list) else set()
    complete = _number(summary.get("onlineCount")) == len(online_names)
    return PanelRows(tuple(_panel_row(item, online_names, complete) for item in items), count, page, page_size)


def fetch_all_clients(credentials: PanelCredentials, *, transport=None) -> PanelRows:
    first = fetch_clients(credentials, page_size=200, transport=transport)
    rows = list(first.rows)
    for number in range(2, (first.total + first.page_size - 1) // first.page_size + 1):
        page = fetch_clients(credentials, number, page_size=first.page_size, transport=transport)
        if page.page != number or page.total != first.total or page.page_size != first.page_size:
            raise PanelError("pagination")
        rows.extend(page.rows)
    if first.page != 1 or len(rows) != first.total or len({row.name for row in rows}) != first.total:
        raise PanelError("pagination")
    return PanelRows(tuple(rows), first.total)


def _panel_row(row, online_names, complete):
    def label(key):
        value = row.get(key)
        return value if isinstance(value, str) and 0 < len(value) <= 512 else None

    name = label("email") or ""
    traffic = _section(row, "traffic")
    online = True if name in online_names else False if complete and name else None
    return PanelClient(name, label("group"), online, _number(traffic.get("up")),
                       _number(traffic.get("down")), label("subId"), label("comment"),
                       _number(row.get("id")))


def _response_object(payload):
    if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("obj"), dict):
        return payload["obj"]
    raise PanelError("response")


def fetch_subscription_url(credentials, sub_id, *, transport=None):
    if not sub_id or sub_id in {".", ".."}:
        raise PanelError("subscription")
    # The tagged panel uses a read-only POST for its computed subscription address.
    data = _response_object(_get_json(credentials, "setting/defaultSettings", method="POST", transport=transport))
    base = data.get("subURI")
    if data.get("subEnable") is not True or not isinstance(base, str):
        raise PanelError("subscription")
    try:
        address = urlsplit(base)
        valid = (address.scheme in {"https", "http"} and address.hostname and address.username is None
                 and not address.query and not address.fragment and "\\" not in base
                 and not any(char.isspace() or ord(char) < 32 for char in base))
    except ValueError:
        valid = False
    if not valid:
        raise PanelError("subscription")
    return base.rstrip("/") + "/" + quote(sub_id, safe="")


def _get_json(credentials, path, *, method="GET", params=None, data=None, max_bytes=128 * 1024,
              transport=None, session=None):
    suppress_http_diagnostics()
    deadline = monotonic() + 8
    try:
        with (nullcontext(session) if session is not None else
              httpx.Client(timeout=3, verify=True, follow_redirects=False, trust_env=False, transport=transport)) as client:
            with client.stream(
                method, credentials.base_url.rstrip("/") + "/panel/api/" + path, params=params, data=data,
                headers={"Authorization": "Bearer " + credentials.token, "X-Requested-With": "XMLHttpRequest",
                         "Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                if response.is_redirect:
                    raise PanelError("redirect")
                if response.status_code in (401, 403):
                    raise PanelError("auth")
                if response.status_code == 404:
                    raise PanelError("path")
                if response.status_code == 429:
                    raise PanelError("rate")
                if response.status_code != 200:
                    raise PanelError("http")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise PanelError("response")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if monotonic() > deadline:
                        raise PanelError("timeout")
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise PanelError("response")
                return json.loads(body)
    except PanelError:
        raise
    except httpx.TimeoutException:
        reason = "timeout"
    except httpx.HTTPError:
        reason = "network"
    except (ValueError, UnicodeError, RecursionError):
        reason = "response"
    raise PanelError(reason)
