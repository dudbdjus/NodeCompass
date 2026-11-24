from collections import defaultdict
from dataclasses import dataclass

from proxy_manager.services.geoip import normalize_ip, public_ip


@dataclass(frozen=True)
class PanelProxyLink:
    client: str
    tag: str
    usage: str
    usage_detail: str
    usage_bytes: int | None
    verification_note: str = ""

    @property
    def sortable_usage(self):
        return None if is_relay_client(self.client) else self.usage_bytes


def is_relay_client(name):
    return "".join(name.split()).casefold() == "中转vps"


def unique_proxy_links(data, geo_result, usage, total_bytes, *, verification_note=""):
    if data is None or geo_result is None or geo_result.status != "ok":
        return {}
    matches = defaultdict(list)
    for client in data.rows:
        if geo_result.users.get(client.record_id) != client.name:
            continue
        finding = geo_result.rows.get(client.record_id)
        ip = normalize_ip(finding.ip) if finding and finding.status == "located" else None
        if not public_ip(ip):
            continue
        parts = client.name.split("-", 2)
        tag = "-".join(parts[:2]) if len(parts) >= 2 and all(parts[:2]) else client.name
        value, detail = usage(client)
        total = total_bytes(client)
        matches[str(ip)].append(PanelProxyLink(client.name, tag, value, detail,
                                               total if total >= 0 else None, verification_note))
    return {ip: links[0] for ip, links in matches.items() if len(links) == 1}
