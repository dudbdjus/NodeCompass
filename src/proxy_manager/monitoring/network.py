from dataclasses import dataclass, field
import json
import math


@dataclass(frozen=True)
class LinkCounters:
    name: str
    index: int
    rx_bytes: int
    tx_bytes: int
    kind: str | None


@dataclass(frozen=True)
class Bandwidth:
    state: str
    interface: str | None = None
    rx_mbps: float | None = None
    tx_mbps: float | None = None
    counters: LinkCounters | None = field(default=None, compare=False)
    rx_delta: int | None = field(default=None, compare=False)
    tx_delta: int | None = field(default=None, compare=False)
    elapsed: float | None = field(default=None, compare=False)


def _objects(raw: str | None) -> list[dict]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return []
    return [value for value in parsed if isinstance(value, dict)] if isinstance(parsed, list) else []


def parse_links(raw: str | None) -> dict[str, LinkCounters]:
    links = {}
    for entry in _objects(raw):
        name = entry.get("ifname")
        index = entry.get("ifindex")
        if not isinstance(name, str) or not name or name == "lo" or type(index) is not int or index < 1:
            continue
        try:
            counters = entry.get("stats64", entry.get("stats"))
            rx, tx = (counters[direction]["bytes"] for direction in ("rx", "tx"))
        except (TypeError, KeyError):
            continue
        if any(type(count) is not int or not 0 <= count < 2**64 for count in (rx, tx)):
            continue
        info = entry.get("linkinfo")
        kind = info.get("info_kind") if isinstance(info, dict) else None
        links[name] = LinkCounters(name, index, rx, tx, kind if isinstance(kind, str) else None)
    return links


_OVERLAYS = frozenset({
    "bridge", "dummy", "geneve", "gre", "gretap", "ifb", "ip6gre", "ip6tnl",
    "ipip", "sit", "tun", "veth", "vxlan", "wireguard",
})


def select_link(links: dict[str, LinkCounters], ipv4: str | None, ipv6: str | None,
                manual: str | None = None) -> LinkCounters | None:
    if manual is not None:
        return links.get(manual)
    for raw_routes in (ipv4, ipv6):
        candidates: list[tuple[int, str]] = []
        for route in _objects(raw_routes):
            metric = route.get("metric", 0)
            if route.get("dst") != "default" or type(metric) is not int or metric < 0:
                continue
            hops = route.get("nexthops")
            for hop in [route, *hops] if isinstance(hops, list) else [route]:
                name = hop.get("dev") if isinstance(hop, dict) else None
                link = links.get(name) if isinstance(name, str) else None
                if link is not None and link.kind not in _OVERLAYS:
                    candidates.append((metric, name))
        if candidates:
            return links[min(candidates)[1]]
    return None


class BandwidthSampler:
    def __init__(self, max_gap: float):
        self._max_gap = max_gap
        self._previous: tuple[LinkCounters, float, str | None, float | None] | None = None

    def reset(self) -> None:
        self._previous = None

    def sample(self, link: LinkCounters | None, now: float, boot_id: str | None,
               uptime: float | None) -> Bandwidth:
        if link is None or not math.isfinite(now) or (boot_id is None and uptime is None):
            self.reset()
            return Bandwidth("missing")
        previous = self._previous
        self._previous = (link, now, boot_id, uptime)
        if previous is None:
            return Bandwidth("baseline", link.name, counters=link)
        old, old_time, old_boot, old_uptime = previous
        seconds = now - old_time
        if ((link.name, link.index) != (old.name, old.index) or boot_id != old_boot
                or (uptime is not None and old_uptime is not None and uptime < old_uptime)
                or (boot_id is None and (uptime is None or old_uptime is None))
                or not 0 < seconds <= self._max_gap
                or link.rx_bytes < old.rx_bytes or link.tx_bytes < old.tx_bytes):
            return Bandwidth("baseline", link.name, counters=link)
        rx_delta, tx_delta = link.rx_bytes - old.rx_bytes, link.tx_bytes - old.tx_bytes
        return Bandwidth("ok", link.name, rx_delta * 8 / seconds / 1e6, tx_delta * 8 / seconds / 1e6,
                         link, rx_delta, tx_delta, seconds)
