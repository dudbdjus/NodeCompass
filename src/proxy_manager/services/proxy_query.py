from proxy_manager.display_labels import country_label, status_label
from proxy_manager.services.geoip import normalize_ip, proxy_location_fields
from proxy_manager.storage.models import Proxy
from proxy_manager.storage.repositories import Repository


class ProxyQuery:
    def __init__(self, repository: Repository):
        self.repository = repository

    def load(self, profile_id: str | None) -> list[Proxy]:
        return self.repository.list_proxies(profile_id) if profile_id else []

    @staticmethod
    def filter(rows: list[Proxy], search: str, country: str, status: str, *, geo=None, panel_links=None) -> list[Proxy]:
        query = search.strip().casefold()
        country = country_label(country)
        status = status_label(status)
        result = []
        for row in rows:
            displayed_country, province, city = proxy_location_fields(row, geo)
            link = panel_links.get(str(normalize_ip(row.address))) if panel_links else None
            if country and displayed_country != country:
                continue
            if status and status_label(row.status) != status:
                continue
            fields = (
                row.address, row.provider_id, row.order_id, row.username, row.country, row.status,
                displayed_country, status_label(row.status), province, city, row.region, row.tag,
                link.tag if link else None,
            )
            ports = tuple(str(port) for port in (row.port_http, row.port_socks) if port is not None)
            if not query or any(query in value.casefold() for value in (*fields, *ports) if value):
                result.append(row)
        return result
