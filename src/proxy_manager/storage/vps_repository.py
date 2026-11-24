from calendar import monthrange
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from time import time
from uuid import UUID, uuid4

from sqlalchemy import text

from proxy_manager.monitoring.network import Bandwidth
from proxy_manager.monitoring.scheduler import Sample
from proxy_manager.monitoring.ssh_client import Endpoint, HostIdentity
from proxy_manager.provider.kiwivm_client import KiwiVMEndpoint, Snapshot
from proxy_manager.provider.kiwivm_parser import UsageHistory
from proxy_manager.storage.maintenance_lock import maintenance_lock


@dataclass(frozen=True)
class VpsProfile:
    id: str = ""
    veid: str | None = None
    ssh_host: str | None = None
    ssh_port: int = 22
    name: str = ""
    location: str = ""
    plan: str = ""
    username: str = ""
    auth_method: str = "password"
    key_path: str = ""
    interface: str | None = None
    auto_api: bool = False
    auto_ssh: bool = False
    live_minutes: int = 10
    x_ui_version: str = ""
    xray_version: str = ""

    @property
    def endpoint(self) -> Endpoint | None:
        return Endpoint(self.ssh_host, self.ssh_port) if self.ssh_host else None

    @property
    def credential_id(self) -> UUID:
        return UUID(self.id)


@dataclass(frozen=True)
class ChartPoint:
    timestamp: float
    rx: float | int | None
    tx: float | int | None
    interface: str | None = None


def _json(data) -> str:
    def encode(value):
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError("unsupported_monitoring_value")
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=encode, allow_nan=False)


def _profile(row) -> VpsProfile:
    return VpsProfile(row.id, row.veid, row.ssh_host, row.ssh_port, **json.loads(row.configuration))


def _months_before(timestamp: float, count: int) -> str:
    date = datetime.fromtimestamp(timestamp, UTC)
    year, month = divmod(date.year * 12 + date.month - 1 - count, 12)
    day = min(date.day, monthrange(year, month + 1)[1])
    return f"{year:04d}-{month + 1:02d}-{day:02d} {date.hour:02d}:{date.minute:02d}"


def ssh_retention_cutoffs(now: float) -> tuple[float, str]:
    return now - 172800, _months_before(now, 24)


class VpsRepository:
    def __init__(self, database):
        self.engine = database.engine

    def active(self) -> VpsProfile:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM vps_profiles WHERE active = 1")).first()
            return _profile(row) if row else VpsProfile()

    def prepare(self, profile: VpsProfile) -> VpsProfile:
        with self.engine.connect() as conn:
            found = conn.execute(text(
                "SELECT id FROM vps_profiles WHERE veid IS :veid AND ssh_host IS :ssh_host "
                "AND ssh_port = :ssh_port LIMIT 1"
            ), {"veid": profile.veid, "ssh_host": profile.ssh_host, "ssh_port": profile.ssh_port}).scalar()
        return replace(profile, id=found or str(uuid4()))

    def save(self, profile: VpsProfile) -> None:
        values = asdict(profile)
        binding = {field: values.pop(field) for field in ("id", "veid", "ssh_host", "ssh_port")}
        binding["configuration"] = _json(values)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE vps_profiles SET active = 0 WHERE active = 1 AND id != :id"), binding)
            conn.execute(text(
                "INSERT INTO vps_profiles (id, active, veid, ssh_host, ssh_port, configuration) "
                "VALUES (:id, 1, :veid, :ssh_host, :ssh_port, :configuration) "
                "ON CONFLICT(id) DO UPDATE SET configuration=excluded.configuration, active=1"
            ), binding)

    @staticmethod
    def _pins(conn, endpoint: Endpoint):
        return conn.execute(text(
            "SELECT DISTINCT key_algorithm, key_sha256 FROM vps_profiles "
            "WHERE ssh_host=:host AND ssh_port=:port AND key_sha256 IS NOT NULL"
        ), {"host": endpoint.host, "port": endpoint.port}).all()

    def trusted(self, endpoint: Endpoint) -> HostIdentity | None:
        with self.engine.connect() as conn:
            rows = self._pins(conn, endpoint)
        if len(rows) > 1:
            raise ValueError("ssh_conflicting_host_keys")
        return HostIdentity(endpoint, *rows[0]) if rows else None

    def trust(self, profile: VpsProfile, identity: HostIdentity) -> None:
        if identity.endpoint != profile.endpoint:
            raise ValueError("ssh_host_key_changed")
        with self.engine.begin() as conn:
            if any(not HostIdentity(identity.endpoint, *row).matches(identity) for row in self._pins(conn, identity.endpoint)):
                raise ValueError("ssh_host_key_changed")
            updated = conn.execute(text(
                "UPDATE vps_profiles SET key_algorithm=:algorithm, key_sha256=:digest "
                "WHERE id=:id AND ssh_host=:host AND ssh_port=:port "
                "AND (key_sha256 IS NULL OR (key_algorithm=:algorithm AND key_sha256=:digest))"
            ), {"id": profile.id, "host": identity.endpoint.host, "port": identity.endpoint.port,
                "algorithm": identity.algorithm, "digest": identity.sha256})
            if updated.rowcount != 1:
                raise ValueError("ssh_host_key_changed")

    def forget(self, endpoint: Endpoint) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE vps_profiles SET key_algorithm=NULL, key_sha256=NULL WHERE ssh_host=:host AND ssh_port=:port"
            ), {"host": endpoint.host, "port": endpoint.port})

    @staticmethod
    def _record(conn, profile_id, source, outcome):
        conn.execute(text(
            "INSERT INTO monitoring_sync_runs (profile_id, source, finished, outcome) "
            "VALUES (:profile, :source, :finished, :outcome)"
        ), {"profile": profile_id, "source": source, "finished": time(), "outcome": outcome})

    def record(self, profile_id: str, source: str, outcome: str) -> None:
        with self.engine.begin() as conn:
            self._record(conn, profile_id, source, outcome)

    def save_kiwi(self, profile_id: str, endpoint: KiwiVMEndpoint, snapshot: Snapshot) -> bool:
        captured = snapshot.fetched_at_utc.timestamp()
        data = asdict(snapshot.data)
        with self.engine.begin() as conn:
            if isinstance(snapshot.data, UsageHistory):
                if not snapshot.data.format_recognized:
                    self._record(conn, profile_id, endpoint.value, "unrecognized_history")
                    return False
                points = data.pop("points")
                if points:
                    conn.execute(text(
                        "INSERT INTO kiwivm_usage_points VALUES (:profile, :timestamp, :cadence, :payload) ON CONFLICT DO NOTHING"
                    ), [{"profile": profile_id, "timestamp": point["timestamp"], "payload": _json(point),
                         "cadence": snapshot.data.observed_interval_seconds or 0} for point in points])
            conn.execute(text(
                "INSERT INTO kiwivm_service_snapshots (profile_id, endpoint, captured, payload) "
                "VALUES (:profile, :endpoint, :captured, :payload)"
            ), {"profile": profile_id, "endpoint": endpoint.value, "captured": captured, "payload": _json(data)})
            self._record(conn, profile_id, endpoint.value, "success")
        return True

    def kiwi_latest(self, profile_id: str) -> dict:
        result = {}
        with self.engine.connect() as conn:
            for endpoint in KiwiVMEndpoint:
                row = conn.execute(text(
                    "SELECT captured, payload FROM kiwivm_service_snapshots "
                    "WHERE profile_id=:profile AND endpoint=:endpoint ORDER BY id DESC LIMIT 1"
                ), {"profile": profile_id, "endpoint": endpoint.value}).first()
                if row:
                    result[endpoint] = (row.captured, json.loads(row.payload))
        return result

    def kiwi_cadences(self, profile_id: str) -> tuple[int, ...]:
        with self.engine.connect() as conn:
            return tuple(conn.execute(text(
                "SELECT DISTINCT cadence FROM kiwivm_usage_points WHERE profile_id=:profile ORDER BY cadence DESC"
            ), {"profile": profile_id}).scalars())

    def kiwi_points(self, profile_id: str, cadence: int) -> list[ChartPoint]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT timestamp, payload FROM kiwivm_usage_points WHERE profile_id=:profile AND cadence=:cadence "
                "ORDER BY timestamp DESC LIMIT 15000"
            ), {"profile": profile_id, "cadence": cadence}).all()
        grouped = defaultdict(set)
        for stamp, payload in rows:
            try:
                point = json.loads(payload)
            except (ValueError, RecursionError):
                point = None
            values = tuple(point.get(key) if isinstance(point, dict) else None
                           for key in ("network_in_bytes", "network_out_bytes"))
            grouped[stamp].add(tuple(value if type(value) is int and value >= 0 else None for value in values))
        return [ChartPoint(stamp, *next(iter(values))) if len(values) == 1 else ChartPoint(stamp, None, None)
                for stamp, values in sorted(grouped.items())]

    def save_samples(self, profile_id: str, samples: list[Sample]) -> None:
        with self.engine.begin() as conn:
            for sample in samples:
                bw = sample.bandwidth
                link = bw.counters
                values = {"profile": profile_id, "captured": sample.captured, "state": bw.state,
                          "interface": bw.interface, "rx": bw.rx_mbps, "tx": bw.tx_mbps,
                          "rx_bytes": str(link.rx_bytes) if link else None, "tx_bytes": str(link.tx_bytes) if link else None,
                          "rx_delta": str(bw.rx_delta) if bw.rx_delta is not None else None,
                          "tx_delta": str(bw.tx_delta) if bw.tx_delta is not None else None, "elapsed": bw.elapsed}
                inserted = conn.execute(text(
                    "INSERT INTO vps_network_samples VALUES "
                    "(:profile, :captured, :state, :interface, :rx_bytes, :tx_bytes, :rx_delta, :tx_delta, :elapsed, :rx, :tx) "
                    "ON CONFLICT DO NOTHING"
                ), values)
                if not inserted.rowcount:
                    continue
                self._roll_up(conn, profile_id, sample.captured, bw)
                if sample.system is not None:
                    conn.execute(text(
                        "INSERT INTO vps_system_samples VALUES (:profile, :captured, :payload)"
                    ), {"profile": profile_id, "captured": sample.captured, "payload": _json(asdict(sample.system))})
                if sample.error:
                    self._record(conn, profile_id, "ssh", sample.error)

    @staticmethod
    def _roll_up(conn, profile_id, timestamp, bandwidth: Bandwidth):
        minute = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M")
        has_rate = bandwidth.state == "ok" and bandwidth.elapsed is not None
        interface = bandwidth.interface or ""
        for period, bucket in (("minute", minute), ("day", minute[:10]), ("month", minute[:7])):
            key = {"profile": profile_id, "period": period, "bucket": bucket, "interface": interface}
            existing = conn.execute(text(
                "SELECT samples, gaps, seconds, rx_bytes, tx_bytes FROM vps_network_rollups "
                "WHERE profile_id=:profile AND period=:period AND bucket=:bucket AND interface=:interface"
            ), key).first()
            samples, gaps, seconds, rx, tx = existing or (0, 0, 0.0, "0", "0")
            conn.execute(text(
                "INSERT INTO vps_network_rollups VALUES "
                "(:profile, :period, :bucket, :interface, :samples, :gaps, :seconds, :rx, :tx) "
                "ON CONFLICT DO UPDATE SET samples=excluded.samples, gaps=excluded.gaps, "
                "seconds=excluded.seconds, rx_bytes=excluded.rx_bytes, tx_bytes=excluded.tx_bytes"
            ), {**key, "samples": samples + has_rate, "gaps": gaps + (not has_rate),
                "seconds": seconds + (bandwidth.elapsed if has_rate else 0),
                "rx": str(int(rx) + (bandwidth.rx_delta or 0)), "tx": str(int(tx) + (bandwidth.tx_delta or 0))})

    def latest_system(self, profile_id: str):
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT captured, payload FROM vps_system_samples WHERE profile_id=:profile ORDER BY captured DESC LIMIT 1"
            ), {"profile": profile_id}).first()
        return (row.captured, json.loads(row.payload)) if row else None

    def network_points(self, profile_id: str, hours: int, *, now=None) -> list[ChartPoint]:
        now = time() if now is None else now
        with self.engine.connect() as conn:
            if hours <= 48:
                rows = conn.execute(text(
                    "SELECT captured, rx_mbps, tx_mbps, interface FROM vps_network_samples "
                    "WHERE profile_id=:profile AND captured>=:start ORDER BY captured"
                ), {"profile": profile_id, "start": now - 3600 * hours})
                return [ChartPoint(*row) for row in rows]
            cutoff = datetime.fromtimestamp(now - 3600 * hours, UTC).strftime("%Y-%m-%d %H:%M")
            rows = conn.execute(text(
                "SELECT bucket, interface, gaps, seconds, rx_bytes, tx_bytes FROM vps_network_rollups "
                "WHERE profile_id=:profile AND period='minute' AND bucket>=:start ORDER BY bucket"
            ), {"profile": profile_id, "start": cutoff}).all()
        points = defaultdict(list)
        for bucket, iface, gaps, seconds, rx, tx in rows:
            stamp = datetime.fromisoformat(bucket).replace(tzinfo=UTC).timestamp()
            points[stamp].append(ChartPoint(stamp, int(rx) * 8 / seconds / 1e6 if seconds and not gaps else None,
                                           int(tx) * 8 / seconds / 1e6 if seconds and not gaps else None, iface))
        return [items[0] if len(items) == 1 else ChartPoint(stamp, None, None) for stamp, items in points.items()]

    def observed_bytes(self, profile_id: str, hours: int, interface: str | None, *, now=None):
        now = time() if now is None else now
        start = now - 3600 * hours
        columns = {"profile": profile_id, "start": start, "interface": interface}
        if hours <= 48:
            query = ("SELECT rx_delta, tx_delta FROM vps_network_samples WHERE profile_id=:profile "
                     "AND captured>=:start AND state='ok' AND rx_delta IS NOT NULL AND tx_delta IS NOT NULL")
        else:
            columns["start"] = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d %H:%M")
            query = ("SELECT rx_bytes, tx_bytes FROM vps_network_rollups WHERE profile_id=:profile "
                     "AND period='minute' AND bucket>=:start AND samples>0")
        if interface:
            query += " AND interface=:interface"
        with self.engine.connect() as conn:
            rows = conn.execute(text(query), columns).all()
        return (sum(int(row[0]) for row in rows), sum(int(row[1]) for row in rows)) if rows else None

    def prune(self, now: float | None = None) -> None:
        now = time() if now is None else now
        raw_cutoff, minute_cutoff = ssh_retention_cutoffs(now)
        with maintenance_lock(Path(self.engine.url.database)), self.engine.begin() as conn:
            conn.execute(text("DELETE FROM vps_network_samples WHERE captured < :cutoff"), {"cutoff": raw_cutoff})
            conn.execute(text("DELETE FROM vps_system_samples WHERE captured < :cutoff"), {"cutoff": raw_cutoff})
            conn.execute(text("DELETE FROM vps_network_rollups WHERE period='minute' AND bucket < :cutoff"),
                         {"cutoff": minute_cutoff})
