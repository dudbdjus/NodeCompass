from dataclasses import dataclass, field
from datetime import date, datetime
from ipaddress import ip_address

from proxy_manager.provider.errors import ProviderError


@dataclass(frozen=True)
class ProxyItem:
    provider_id: str
    address: str
    port_http: int | None
    port_socks: int | None
    username: str | None = field(repr=False)
    password: str | None = field(repr=False)
    country: str | None
    status: str | None
    order_id: str | None
    expires_on: date | None


@dataclass(frozen=True)
class ProxyPage:
    items: tuple[ProxyItem, ...]
    page: int
    per_page: int
    total: int
    total_pages: int


def _integer(data: dict, key: str, minimum: int) -> int:
    value = data.get(key)
    if type(value) is not int or value < minimum:
        raise ProviderError("invalid_response")
    return value


def _text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProviderError("invalid_response")
    return value


def _port(data: dict, key: str) -> int | None:
    port = data.get(key)
    if port is None:
        return None
    # Live ISP records serialize ports as strings. Examples also use integers.
    if isinstance(port, str) and port.isascii() and port.isdecimal() and len(port) <= 5:
        port = int(port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProviderError("invalid_response")
    return port


def _item(data: object) -> ProxyItem:
    if not isinstance(data, dict):
        raise ProviderError("invalid_response")
    provider_id = _text(data, "id")
    address = _text(data, "ip_only") or _text(data, "ip")
    if not provider_id or not address:
        raise ProviderError("invalid_response")
    date_end = _text(data, "date_end")
    try:
        address = str(ip_address(address))
        expires_on = datetime.strptime(date_end, "%d.%m.%Y").date() if date_end else None
    except ValueError:
        address = None
    if address is None:
        raise ProviderError("invalid_response")
    return ProxyItem(
        provider_id, address, _port(data, "port_http"), _port(data, "port_socks"),
        _text(data, "login"), _text(data, "password"),
        _text(data, "country_alpha3") or _text(data, "country"),
        _text(data, "status"), _text(data, "order_id"), expires_on,
    )


def parse_page(payload: object) -> ProxyPage:
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response")
    if payload.get("status") == "error":
        raise ProviderError("business_error", 200)
    data = payload.get("data")
    if (
        payload.get("status") != "success" or payload.get("errors") != []
        or not isinstance(data, dict) or not isinstance(data.get("items"), list)
    ):
        raise ProviderError("invalid_response")
    return ProxyPage(
        tuple(_item(item) for item in data["items"]),
        _integer(data, "page", 1),
        _integer(data, "per_page", 1),
        _integer(data, "total", 0),
        _integer(data, "total_pages", 0),
    )
