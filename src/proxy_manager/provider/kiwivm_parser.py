from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from ipaddress import ip_address, ip_interface

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class KiwiVMError(Exception):
    """Expose only diagnostic codes and trusted HTTP status codes."""

    def __init__(self, kind: str, status: int | None = None):
        self.kind = kind
        self.status = status
        super().__init__(kind if status is None else f"{kind} (HTTP {status})")


@dataclass(frozen=True)
class BillingTraffic:
    """KiwiVM billing bytes for the cycle ending at reset_timestamp."""

    raw_quota_bytes: int | None
    raw_used_bytes: int | None
    multiplier: Decimal | None
    reset_timestamp: int | None

    def _adjust(self, raw_bytes: int | None) -> Decimal | None:
        multiplier = self.multiplier
        if raw_bytes is None or multiplier is None:
            return None
        with localcontext() as context:
            context.prec = len(str(raw_bytes)) + len(multiplier.as_tuple().digits)
            return Decimal(raw_bytes) * multiplier

    @property
    def quota_bytes(self) -> Decimal | None:
        return self._adjust(self.raw_quota_bytes)

    @property
    def used_bytes(self) -> Decimal | None:
        return self._adjust(self.raw_used_bytes)

    @property
    def remaining_bytes(self) -> Decimal | None:
        quota, used = self.quota_bytes, self.used_bytes
        if quota is None or used is None:
            return None
        with localcontext() as context:
            context.prec = max(len(quota.as_tuple().digits), len(used.as_tuple().digits)) + 1
            return max(Decimal(0), quota - used)

    @property
    def usage_percent(self) -> Decimal | None:
        quota, used = self.raw_quota_bytes, self.raw_used_bytes
        if self.multiplier is None or not quota or used is None:
            return None
        # The same positive multiplier applies to the numerator and denominator.
        return Decimal(used) * 100 / Decimal(quota)

    @property
    def reset_at_utc(self) -> datetime | None:
        return _utc(self.reset_timestamp)


@dataclass(frozen=True)
class ServiceInfo:
    vm_type: str | None
    hostname: str | None
    node_alias: str | None
    node_location: str | None
    node_datacenter: str | None
    plan: str | None
    plan_disk: int | None
    plan_ram: int | None
    plan_swap: int | None
    ip_addresses: tuple[str, ...] | None
    suspended: bool | None
    policy_violation: bool | None
    billing: BillingTraffic


@dataclass(frozen=True)
class LiveServiceInfo:
    service: ServiceInfo
    ve_status: str | None
    ve_used_disk_space_b: int | None
    ve_disk_quota_gb: Decimal | None
    is_cpu_throttled: bool | None
    is_disk_throttled: bool | None
    live_hostname: str | None
    load_average: str | None
    mem_available_kb: int | None
    swap_total_kb: int | None
    swap_available_kb: int | None
    ssh_port: int | None


@dataclass(frozen=True)
class UsagePoint:
    timestamp: int
    network_in_bytes: int | None
    network_out_bytes: int | None
    cpu_usage: Decimal | None
    disk_read_bytes: int | None
    disk_write_bytes: int | None

    @property
    def at_utc(self) -> datetime:
        return EPOCH + timedelta(seconds=self.timestamp)


@dataclass(frozen=True)
class UsageHistory:
    points: tuple[UsagePoint, ...]
    # Observation of timestamp spacing only, not a statement about traffic semantics.
    observed_interval_seconds: int | None
    layout: str
    input_rows: int | None
    ignored_rows: int
    duplicate_rows: int = 0
    traffic_semantics: str = "unconfirmed"

    @property
    def format_recognized(self) -> bool:
        return self.layout != "unknown"


def _whole(value: object) -> int | None:
    if type(value) is str and value.isascii() and value.isdecimal():
        digits = value.lstrip("0") or "0"
        if len(digits) <= 20:
            value = int(digits)
    return value if type(value) is int and 0 <= value < 10**20 else None


def _decimal(value: object) -> Decimal | None:
    if type(value) not in (int, float, Decimal, str):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    # Keep decimal arithmetic bounded for malformed or extreme provider input.
    if not number.is_finite() or number < 0:
        return None
    representation = number.as_tuple()
    if len(representation.digits) > 28 or abs(representation.exponent) > 28:
        return None
    return number


def _flag(value: object) -> bool | None:
    if type(value) is bool:
        return value
    whole = _whole(value)
    return bool(whole) if whole in (0, 1) else None


def _utc(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    try:
        return EPOCH + timedelta(seconds=timestamp)
    except OverflowError:
        return None


def _text(value: object, secret: str) -> str | None:
    if not isinstance(value, str) or not value.isprintable():
        return None
    normalized = value.strip()
    return normalized if normalized and (not secret or secret not in normalized) else None


def _addresses(value: object, secret: str) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    addresses = []
    for entry in value:
        candidate = _text(entry, secret)
        if candidate is None:
            continue
        try:
            addresses.append(str(ip_interface(candidate) if "/" in candidate else ip_address(candidate)))
        except ValueError:
            continue
    return tuple(addresses)


def _error_kind(message: object) -> str:
    if not isinstance(message, str):
        return "api_error"
    text = " ".join(message.casefold().replace("_", " ").split()).rstrip(".!")
    if text in {"invalid veid", "veid invalid", "veid is invalid"}:
        return "invalid_veid"
    if text in {"invalid api key", "api key invalid", "api key is invalid"}:
        return "invalid_api_key"
    if text in {"invalid veid or api key", "authentication failed", "unauthorized", "access denied"}:
        return "authentication_failed"
    return "api_error"


def _payload(value: object) -> dict:
    if not isinstance(value, dict):
        raise KiwiVMError("invalid_response")
    error = value.get("error")
    if type(error) is str and len(error) <= 10 and error.removeprefix("-").isascii() and error.removeprefix("-").isdecimal():
        error = int(error)
    if type(error) is not int:
        raise KiwiVMError("invalid_response")
    if error != 0:
        raise KiwiVMError(_error_kind(value.get("message")))
    return value


def _service(data: dict, secret: str) -> ServiceInfo:
    multiplier = _decimal(data.get("monthly_data_multiplier"))
    timestamp = _whole(data.get("data_next_reset"))
    billing = BillingTraffic(
        raw_quota_bytes=_whole(data.get("plan_monthly_data")),
        raw_used_bytes=_whole(data.get("data_counter")),
        multiplier=multiplier if multiplier else None,
        reset_timestamp=timestamp,
    )
    return ServiceInfo(
        vm_type=_text(data.get("vm_type"), secret),
        hostname=_text(data.get("hostname"), secret),
        node_alias=_text(data.get("node_alias"), secret),
        node_location=_text(data.get("node_location"), secret),
        node_datacenter=_text(data.get("node_datacenter"), secret),
        plan=_text(data.get("plan"), secret),
        plan_disk=_whole(data.get("plan_disk")),
        plan_ram=_whole(data.get("plan_ram")),
        plan_swap=_whole(data.get("plan_swap")),
        ip_addresses=_addresses(data.get("ip_addresses"), secret),
        suspended=_flag(data.get("suspended")),
        policy_violation=_flag(data.get("policy_violation")),
        billing=billing,
    )


def _has_service(info: ServiceInfo) -> bool:
    return any(value is not None for value in vars(info).values() if value is not info.billing) or any(
        value is not None for value in vars(info.billing).values()
    )


def parse_service_info(value: object, *, secret: str = "") -> ServiceInfo:
    payload = _payload(value)
    nested = payload.get("data")
    result = _service(nested if isinstance(nested, dict) else payload, secret)
    if not _has_service(result):
        raise KiwiVMError("invalid_response")
    return result


def parse_live_service_info(value: object, *, secret: str = "") -> LiveServiceInfo:
    payload = _payload(value)
    nested = payload.get("data")
    data = nested if isinstance(nested, dict) else payload
    port = _whole(data.get("ssh_port"))
    result = LiveServiceInfo(
        service=_service(data, secret),
        ve_status=_text(data.get("ve_status"), secret),
        ve_used_disk_space_b=_whole(data.get("ve_used_disk_space_b")),
        ve_disk_quota_gb=_decimal(data.get("ve_disk_quota_gb")),
        is_cpu_throttled=_flag(data.get("is_cpu_throttled")),
        is_disk_throttled=_flag(data.get("is_disk_throttled")),
        live_hostname=_text(data.get("live_hostname"), secret),
        load_average=_text(data.get("load_average"), secret),
        mem_available_kb=_whole(data.get("mem_available_kb")),
        swap_total_kb=_whole(data.get("swap_total_kb")),
        swap_available_kb=_whole(data.get("swap_available_kb")),
        ssh_port=port if port is not None and 1 <= port <= 65535 else None,
    )
    if not _has_service(result.service) and all(
        item is None for item in vars(result).values() if item is not result.service
    ):
        raise KiwiVMError("invalid_response")
    return result


def _point(row: object, *, secret: str, indexed_timestamp: int | None = None) -> UsagePoint | None:
    if not isinstance(row, dict):
        return None

    def field(name: str, default=None):
        raw = row.get(name, default)
        return None if secret and isinstance(raw, str) and secret in raw else raw

    timestamp = _whole(field("timestamp", indexed_timestamp))
    if _utc(timestamp) is None or (indexed_timestamp is not None and timestamp != indexed_timestamp):
        return None
    result = UsagePoint(
        timestamp=timestamp,
        network_in_bytes=_whole(field("network_in_bytes")),
        network_out_bytes=_whole(field("network_out_bytes")),
        cpu_usage=_decimal(field("cpu_usage")),
        disk_read_bytes=_whole(field("disk_read_bytes")),
        disk_write_bytes=_whole(field("disk_write_bytes")),
    )
    return result if any(item is not None for item in tuple(vars(result).values())[1:]) else None


def parse_usage_history(value: object, *, secret: str = "") -> UsageHistory:
    payload = _payload(value)
    data = payload.get("data")
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if isinstance(data, list):
        label = "named_rows"
        parsed = (_point(row, secret=secret) for row in data)
    elif isinstance(data, dict) and all(_whole(timestamp) is not None for timestamp in data):
        label = "timestamp_map"
        parsed = (
            None if secret and secret in timestamp else _point(row, secret=secret, indexed_timestamp=_whole(timestamp))
            for timestamp, row in data.items()
        )
    else:
        return UsageHistory((), None, "unknown", None, 0)

    valid = [point for point in parsed if point is not None]
    if data and not valid:
        return UsageHistory((), None, "unknown", len(data), len(data))
    # Deduplicate exactly equal points. Conflicting variants remain available for investigation.
    ordered = tuple(sorted(dict.fromkeys(valid), key=lambda point: point.timestamp))
    stamps = sorted({point.timestamp for point in ordered})
    intervals = {later - earlier for earlier, later in zip(stamps, stamps[1:])}
    interval = next(iter(intervals)) if len(intervals) == 1 else None
    return UsageHistory(ordered, interval, label, len(data), len(data) - len(valid), len(valid) - len(ordered))
