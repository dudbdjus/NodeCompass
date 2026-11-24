from dataclasses import dataclass, field
import math
import shlex
from uuid import UUID


@dataclass(frozen=True)
class Memory:
    total_bytes: int
    available_bytes: int
    used_percent: float


@dataclass(frozen=True)
class Swap:
    total_bytes: int
    used_percent: float | None


@dataclass(frozen=True)
class Disk:
    total_bytes: int
    used_bytes: int


@dataclass(frozen=True)
class Service:
    state: str
    substate: str | None = None
    result: str | None = None
    exit_status: int | None = None


@dataclass(frozen=True)
class Version:
    value: str
    source: str = field(default="manual", init=False)


@dataclass(frozen=True)
class SystemMetrics:
    cpu_percent: float | None
    memory: Memory | None
    swap: Swap | None
    root_disk: Disk | None
    load: tuple[float, float, float] | None
    uptime_seconds: float | None
    x_ui: Service
    xray: str
    x_ui_version: Version | None = None
    xray_version: Version | None = None
    os_release: str | None = None


def parse_os_release(raw: str | None) -> str | None:
    values = {}
    for line in (raw or "")[:16384].splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in {"PRETTY_NAME", "NAME", "VERSION_ID", "VERSION"}:
            continue
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            continue
        if len(parts) == 1 and 0 < len(parts[0]) <= 160 and parts[0].isprintable():
            values[key] = parts[0]
    version = values.get("VERSION_ID")
    pretty = values.get("PRETTY_NAME")
    if pretty:
        return f"{pretty} · {version}" if version and version not in pretty else pretty
    name = values.get("NAME")
    suffix = version or values.get("VERSION")
    return f"{name} {suffix}" if name and suffix else name


def _integers(fields: list[str]) -> tuple[int, ...] | None:
    try:
        values = tuple(int(field) for field in fields)
    except ValueError:
        return None
    return values if all(0 <= value < 2**64 for value in values) else None


def _numbers(raw: str | None, count: int) -> tuple[float, ...] | None:
    fields = (raw or "").split()
    if len(fields) < count:
        return None
    try:
        values = tuple(float(field) for field in fields[:count])
    except ValueError:
        return None
    return values if all(math.isfinite(value) and value >= 0 for value in values) else None


def parse_uptime(raw: str | None) -> float | None:
    result = _numbers(raw, 1)
    return result[0] if result else None


def parse_load(raw: str | None) -> tuple[float, float, float] | None:
    result = _numbers(raw, 3)
    return (result[0], result[1], result[2]) if result else None


def parse_boot_id(raw: str | None) -> str | None:
    try:
        return str(UUID((raw or "").strip()))
    except ValueError:
        return None


def parse_memory(raw: str | None) -> tuple[Memory | None, Swap | None]:
    values = {}
    for line in (raw or "").splitlines():
        key, _, text = line.partition(":")
        fields = text.split()
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"} and len(fields) == 2 and fields[1] == "kB":
            number = _integers(fields[:1])
            if number is not None:
                values[key] = number[0] * 1024
    total, avail = values.get("MemTotal", 0), values.get("MemAvailable", -1)
    memory = Memory(total, avail, (total - avail) / total * 100) if total and 0 <= avail <= total else None
    total, free = values.get("SwapTotal", -1), values.get("SwapFree", -1)
    swap = Swap(total, (total - free) / total * 100 if total else None) if 0 <= free <= total else None
    return memory, swap


def parse_disk(raw: str | None) -> Disk | None:
    for line in (raw or "").splitlines()[1:]:
        fields = line.rsplit(maxsplit=5)
        if len(fields) == 6 and fields[-1] == "/":
            values = _integers(fields[1:3])
            if values and 0 <= values[1] <= values[0] and values[0] > 0:
                return Disk(*values)
    return None


def parse_service(raw: str | None) -> Service:
    values = dict(line.split("=", 1) for line in (raw or "").splitlines() if "=" in line)
    loaded, active = values.get("LoadState"), values.get("ActiveState")
    if loaded == "not-found":
        return Service("not_found")
    if loaded not in {"loaded", "masked", "error", "bad-setting"}:
        return Service("unknown")
    if active not in {"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance", "refreshing"}:
        return Service("unknown")
    substate, result = values.get("SubState"), values.get("Result")
    exit_code = _integers([values.get("ExecMainStatus", "")])
    return Service(active, substate if substate in {"running", "exited", "dead", "failed", "auto-restart", "start", "stop"} else None,
                   result if result in {"success", "exit-code", "signal", "timeout", "core-dump", "start-limit-hit", "resources", "watchdog", "oom-kill"} else None,
                   exit_code[0] if exit_code else None)


_XRAY_NAMES = {"xray", "xray-linux-amd64", "xray-linux-amd6", "xray-linux-arm64", "xray-linux-arm6"}


def parse_xray(raw: str | None) -> str:
    lines = (raw or "").splitlines()
    if not lines or lines[0].strip() != "COMMAND":
        return "unknown"
    return "running" if any(line.strip() in _XRAY_NAMES for line in lines[1:]) else "not_observed"


class CpuSampler:
    def __init__(self):
        self._previous: tuple[int, ...] | None = None

    def reset(self) -> None:
        self._previous = None

    def sample(self, raw: str | None) -> float | None:
        fields = (raw or "").partition("\n")[0].split()
        new = _integers(fields[1:9]) if len(fields) >= 9 and fields[0] == "cpu" else None
        old, self._previous = self._previous, new
        if old is None or new is None:
            return None
        delta = tuple(after - before for before, after in zip(old, new))
        total = sum(delta)
        if total == 0 or min(delta) < 0:
            return None
        return (total - delta[3] - delta[4]) / total * 100
