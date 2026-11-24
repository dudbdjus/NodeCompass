"""IP2Location LITE DB3 offline geolocation."""
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import ipaddress
from pathlib import Path
import os
import re
import shutil
import struct
import sys
from tempfile import NamedTemporaryFile
from threading import RLock

from IP2Location import IP2Location

from proxy_manager.display_labels import country_label
from proxy_manager.services.geo_names_zh import has_han, localized_name

SOURCE = "IP2Location LITE DB3"
IP2_OFFLINE = SOURCE
DB_ERRORS = (OSError, ValueError, TypeError, SystemError, OverflowError, struct.error, IndexError)
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")))


def normalize_ip(raw):
    try:
        if not isinstance(raw, str) or "%" in raw:
            return None
        ip = ipaddress.ip_address(raw.strip())
        return getattr(ip, "ipv4_mapped", None) or ip
    except ValueError:
        return None


def public_ip(ip):
    return bool(ip and ip.is_global and not any((
        ip.is_multicast, ip.is_reserved, ip.is_loopback, ip.is_link_local,
        ip.is_unspecified, ip.is_private, getattr(ip, "is_site_local", False),
    )))


@dataclass(frozen=True)
class Location:
    status: str
    ip: str = ""
    version: str = ""
    country_code: str = ""
    country: str = ""
    province: str = ""
    city: str = ""
    source: str = SOURCE

    @property
    def region(self):
        return " · ".join(dict.fromkeys(filter(None, (self.province, self.city))))

    @property
    def label(self):
        return " · ".join(dict.fromkeys(filter(None, (self.country, self.province, self.city))))


def parse_location(data, ip, version):
    data = data if isinstance(data, dict) else {}
    country = data.get("country")
    code = country.get("iso_code") if isinstance(country, dict) else None
    code = code.upper() if isinstance(code, str) and re.fullmatch("[A-Za-z]{2}", code) else ""
    translated = country_label(code)
    name = translated if has_han(translated) else localized_name(country, "国家暂无中文名")
    states = data.get("subdivisions")
    states = states if isinstance(states, list) else []
    translated_states = [localized_name(section, "州／省暂无中文名") for section in states]
    province = next((state for state in translated_states if state and state != "州／省暂无中文名"),
                    "州／省暂无中文名" if "州／省暂无中文名" in translated_states else "")
    city = localized_name(data.get("city"), "城市暂无中文名")
    return Location("ok" if name or province or city else "no_record", ip, version, code, name, province, city)


class OfflineBINCache:
    errors = DB_ERRORS
    suffix = ".bin"

    def __init__(self, import_path, source_directory, *, source, filename_pattern, cache_limit):
        self.import_path = Path(import_path)
        self.source_directory = Path(source_directory)
        self.source = source
        self.filename_pattern = filename_pattern
        self.cache_limit = cache_limit
        self.cache = OrderedDict()
        self.lock = RLock()
        self.reader = None
        self.ip_version = None
        self.version = ""
        self.status = "missing"
        self.reload()

    def reload(self):
        with self.lock:
            self._close_reader()
            self.status = "missing"
            try:
                candidates = sorted((p for p in self.source_directory.glob("*")
                                     if re.fullmatch(self.filename_pattern, p.name, re.IGNORECASE)),
                                    reverse=True)
                path = self.import_path if self.import_path.is_file() else next(iter(candidates), None)
                if path is not None:
                    self.reader, self.version, self.ip_version = self._open(path)
                    self.status = "ready"
            except self.errors:
                self.status = "invalid"

    def close(self):
        self._close_reader()

    def _close_reader(self):
        with self.lock:
            if self.reader is not None:
                self.reader.close()
            self.reader = None
            self.ip_version = None
            self.version = ""
            self.cache.clear()

    def import_file(self, source):
        """Stage and validate before replacing the current import on this filesystem."""
        path = None
        try:
            self.import_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.import_path.parent, suffix=self.suffix, delete=False) as target:
                path = Path(target.name)
                with Path(source).open("rb") as handle:
                    shutil.copyfileobj(handle, target)
                target.flush()
                os.fsync(target.fileno())
            candidate, _, _ = self._open(path)
            candidate.close()
            with self.lock:
                self._close_reader()
                try:
                    os.replace(path, self.import_path)
                finally:
                    self.reload()
            return self.status == "ready"
        except self.errors:
            return False
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def lookup(self, raw):
        ip = normalize_ip(raw)
        if ip is None:
            return Location("invalid_ip", source=self.source)
        if not public_ip(ip):
            scope = ("loopback" if ip.is_loopback else "private"
                     if any(ip in net for net in PRIVATE_NETWORKS) else "reserved"
                     if ip.is_reserved or ip.is_private or ip.is_multicast else "nonpublic")
            return Location(scope, str(ip), source=self.source)
        with self.lock:
            if self.reader is None:
                return Location(self.status, str(ip), source=self.source)
            if ip.version == 6 and self.ip_version == 4:
                return Location("no_record", str(ip), self.version, source=self.source)
            key = self.version, str(ip)
            if key not in self.cache:
                try:
                    self.cache[key] = replace(parse_location(self.reader.get(str(ip)), str(ip), self.version),
                                              source=self.source)
                except self.errors:
                    self.cache[key] = Location("invalid", str(ip), self.version, source=self.source)
                if len(self.cache) > max(1, self.cache_limit):
                    self.cache.popitem(last=False)
            self.cache.move_to_end(key)
            return self.cache[key]


def ip2_location_data(code, country, region, city):
    def named(value):
        return {"names": {"en": value}} if isinstance(value, str) and value.strip() not in {"", "-"} else {}
    province = named(region)
    if code == "US" and isinstance(region, str) and region.strip().casefold() == "new york":
        province["names"]["zh-CN"] = "纽约州"
    if code == "US" and isinstance(city, str) and city.strip().casefold() == "new york":
        city = "New York City"
    return {"country": {**named(country), "iso_code": code},
            "subdivisions": [province], "city": named(city)}


class IP2BINReader(IP2Location):
    def get(self, ip):
        entry = self.get_all(ip)
        return ip2_location_data(entry.country_short, entry.country_long, entry.region, entry.city) if entry else {}


class GeoIP(OfflineBINCache):
    def __init__(self, directory, *, source_directory=None, cache_limit=8192):
        root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
        if source_directory is None:
            source_directory = root / "ip2location"
            if not getattr(sys, "frozen", False) and not source_directory.is_dir():
                source_directory = root / "IP2LOCATION-LITE-DB3.BIN"
        super().__init__(Path(directory) / "ip2location" / "active.bin", source_directory,
                         source=SOURCE, filename_pattern=r"IP2LOCATION-LITE-DB3(?:\.IPV6)?\.BIN",
                         cache_limit=cache_limit)

    def _open(self, path):
        reader = IP2BINReader()
        try:
            if not 32 < path.stat().st_size <= 256 * 1024**2:
                raise ValueError("invalid_bin_size")
            reader.open(str(path))
            # The pinned SDK exposes the DB3 header through these fields; check tables before decoding.
            if reader._dbtype != 3 or reader._dbcolumn != 4 or not 0 < reader._dbyear < 100:
                raise ValueError("invalid_db3_type")
            for count, addr, width in ((reader._ipv4dbcount, reader._ipv4dbaddr, 16),
                                       (reader._ipv6dbcount, reader._ipv6dbaddr, 28)):
                if count and (addr < 33 or addr - 1 + count * width > path.stat().st_size):
                    raise ValueError("invalid_bin_table")
            if not reader._ipv4dbcount:
                raise ValueError("missing_ipv4_table")
            stamp = datetime(2000 + reader._dbyear, reader._dbmonth, reader._dbday).strftime("%Y-%m-%d")
            reader.get("8.8.8.8")
            if reader._ipv6dbcount:
                reader.get("2001:4860:4860::8888")
            return reader, stamp, 6 if reader._ipv6dbcount else 4
        except Exception:
            reader.close()
            raise


def proxy_location_fields(row, geo):
    location = geo.lookup(row.address) if geo else Location("missing")
    if isinstance(geo, GeoIP):
        return location.country, location.province, location.city
    supplier_country = country_label(row.country)
    supplier_country = supplier_country if has_han(supplier_country) else ("国家暂无中文名" if supplier_country else "")
    return location.country or supplier_country, location.province, location.city
