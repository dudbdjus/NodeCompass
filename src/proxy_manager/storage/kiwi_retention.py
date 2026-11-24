"""Bounded retention for KiwiVM snapshots and raw points in an existing 0005 database."""

from calendar import monthrange
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from threading import Event
from time import monotonic, time
from uuid import uuid4

from .maintenance_lock import maintenance_lock
from .upgrade_backup import (HEAD_REVISION, _integrity, _read_only, _revision,
                             verified_backup, verify_backup_digest)


_DAY = 86400
_BATCH_SIZE = 250
SERVICE, LIVE, HISTORY = _SOURCES = ("getServiceInfo", "getLiveServiceInfo", "getRawUsageStats")
_MIN_TIME = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
_MAX_TIME = datetime(2100, 1, 1, tzinfo=UTC).timestamp()


class RetentionClockError(RuntimeError):
    pass


class RetentionCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class BillingBoundary:
    current_reset: int
    previous_reset: int
    previous_snapshot: int


@dataclass(frozen=True)
class CleanupRange:
    profile_id: str
    source: str
    count: int
    first: float
    last: float
    max_rowid: int
    boundary: BillingBoundary | None


@dataclass(frozen=True)
class CleanupPlan:
    ranges: list[CleanupRange]
    deferred_profiles: list[str]


def cutoffs(now: float) -> tuple[float, int]:
    if not math.isfinite(now) or not _MIN_TIME <= now < _MAX_TIME:
        raise RetentionClockError("kiwi_retention_clock_out_of_range")
    instant = datetime.fromtimestamp(now, UTC)
    year = instant.year - 2
    date = instant.replace(year=year, day=min(instant.day, monthrange(year, instant.month)[1]))
    return (instant - timedelta(days=90)).timestamp(), math.ceil(date.timestamp())


def _observations(conn, profile_id, now):
    rows = conn.execute("""
        SELECT id, endpoint, captured,
               CASE WHEN json_valid(payload) THEN
                    json_extract(payload, CASE WHEN endpoint='getLiveServiceInfo'
                        THEN '$.service.billing.reset_timestamp' ELSE '$.billing.reset_timestamp' END)
               END
        FROM kiwivm_service_snapshots WHERE profile_id=? AND endpoint IN (?,?)
        AND captured <= ? ORDER BY captured, id
    """, (profile_id, *_SOURCES[:2], now))
    return [(rowid, source, observed, reset) for rowid, source, observed, reset in rows
            if type(reset) is int and _MIN_TIME <= observed < reset < _MAX_TIME]


def _current_reset(conn, profile_id, now):
    current = set()
    for source, field in ((SERVICE, "$.billing.reset_timestamp"),
                          (LIVE, "$.service.billing.reset_timestamp")):
        row = conn.execute("""
            SELECT captured, CASE WHEN json_valid(payload) THEN json_extract(payload, ?) END
            FROM kiwivm_service_snapshots WHERE profile_id=? AND endpoint=? AND captured BETWEEN ? AND ?
            ORDER BY id DESC LIMIT 1
        """, (field, profile_id, source, now - 2 * _DAY, now)).fetchone()
        if row and now - 2 * _DAY <= row[0] <= now and type(row[1]) is int and now < row[1] < _MAX_TIME:
            current.add(row[1])
    return next(iter(current)) if len(current) == 1 else None


def _cycle(conn, profile_id, now):
    reset = _current_reset(conn, profile_id, now)
    if reset is None:
        return None
    observations = _observations(conn, profile_id, now)
    first_current = min(observed for _, _, observed, candidate in observations if candidate == reset)
    prior = [(candidate, rowid) for rowid, _, observed, candidate in observations
             if observed < first_current and candidate <= first_current]
    anchor = max(prior, default=None)
    return BillingBoundary(reset, *anchor) if anchor else None


def _selection(profile_id, source, now, boundary):
    snapshot_cutoff, history_cutoff = cutoffs(now)
    if source == "usage_points":
        if boundary is None:
            return None
        return ("kiwivm_usage_points", "timestamp", "profile_id=? AND timestamp<?",
                (profile_id, min(history_cutoff, boundary.previous_reset)))

    # Keep the most recent successful snapshot for each source as a display fallback.
    # History-fetch metadata carries no billing fields.
    predicate = """profile_id=? AND endpoint=? AND captured<? AND id<>(SELECT id
        FROM kiwivm_service_snapshots WHERE profile_id=? AND endpoint=? ORDER BY id DESC LIMIT 1)"""
    parameters = [profile_id, source, snapshot_cutoff, profile_id, source]
    if source != "getRawUsageStats":
        reset_field = ("$.service.billing.reset_timestamp" if source == "getLiveServiceInfo"
                       else "$.billing.reset_timestamp")
        # A missing/malformed reset or future reset stays protected. When the preceding cycle
        # boundary is known, also preserve any snapshot captured in the current cycle.
        predicate += """ AND (CASE WHEN json_valid(payload) THEN json_type(payload, ?) END)='integer'
            AND (CASE WHEN json_valid(payload) THEN json_extract(payload, ?) END) BETWEEN ? AND ?"""
        parameters.extend((reset_field, reset_field, _MIN_TIME, now))
        if boundary:
            predicate += " AND captured<? AND id<>?"
            parameters.extend((boundary.previous_reset, boundary.previous_snapshot))
        else:
            # Preserve observations until a previous boundary can be established.
            return None
    return "kiwivm_service_snapshots", "captured", predicate, tuple(parameters)


def _check_cancel(cancel):
    if cancel.is_set():
        raise RetentionCancelled("kiwi_retention_cancelled")


class KiwiRetention:
    def __init__(self, path: Path, *, wall_clock=time, steady_clock=monotonic):
        self.path = path.resolve()
        self.wall_clock = wall_clock
        self.steady_clock = steady_clock
        self.state_path = self.path.with_suffix(self.path.suffix + ".kiwi-retention.json")
        self.audit_path = self.path.with_suffix(self.path.suffix + ".kiwi-retention.jsonl")

    def _audit(self, event, **details):
        with self.audit_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"event": event, "at": self.wall_clock(), **details}) + "\n")
            output.flush()
            os.fsync(output.fileno())

    def _save_state(self, state):
        output = NamedTemporaryFile(dir=self.path.parent, delete=False, prefix=".kiwi-retention-", mode="w",
                                    encoding="utf-8")
        path = Path(output.name)
        try:
            with output:
                json.dump(state, output)
                output.flush()
                os.fsync(output.fileno())
            path.replace(self.state_path)
        finally:
            path.unlink(missing_ok=True)

    def _clock_state(self, now, steady):
        cutoffs(now)
        if not self.state_path.exists():
            return {"wall": now, "steady": steady}, False
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        wall_delta = now - state["wall"]
        steady_delta = steady - state["steady"]
        if 0 <= wall_delta <= 2 * _DAY and steady_delta < 0:
            # A normal reboot loses the older monotonic reference. Re-observe for a day.
            return {**state, "wall": now, "steady": steady}, False
        if not (math.isfinite(wall_delta) and math.isfinite(steady_delta) and wall_delta >= 0
                and steady_delta >= 0 and abs(wall_delta - steady_delta) <= 120):
            raise RetentionClockError("kiwi_retention_clock_discontinuity")
        return state, steady_delta >= _DAY

    def _plan(self, now, cancel):
        snapshot_cutoff, history_cutoff = cutoffs(now)
        ranges = []
        deferred = []
        with closing(_read_only(self.path)) as conn:
            conn.set_progress_handler(cancel.is_set, 1000)
            conn.execute("BEGIN")
            if _revision(conn) != HEAD_REVISION:
                raise RuntimeError("kiwi_retention_unsupported_revision")
            profiles = conn.execute("SELECT id FROM vps_profiles WHERE veid IS NOT NULL").fetchall()
            for (profile_id,) in profiles:
                _check_cancel(cancel)
                boundary = _cycle(conn, profile_id, now)
                if boundary is None and conn.execute("""
                    SELECT 1 FROM kiwivm_usage_points WHERE profile_id=? AND timestamp<?
                    UNION ALL
                    SELECT 1 FROM kiwivm_service_snapshots WHERE profile_id=? AND endpoint IN (?,?) AND captured<?
                    LIMIT 1
                """, (profile_id, history_cutoff, profile_id, SERVICE, LIVE, snapshot_cutoff)).fetchone():
                    deferred.append(profile_id)
                for source in (*_SOURCES, "usage_points"):
                    selection = _selection(profile_id, source, now, boundary)
                    if selection is None:
                        continue
                    table, stamp, predicate, parameters = selection
                    count, first, last, high = conn.execute(
                        f"SELECT count(*), min({stamp}), max({stamp}), max(rowid) FROM {table} WHERE {predicate}",
                        parameters,
                    ).fetchone()
                    if count:
                        ranges.append(CleanupRange(profile_id, source, count, first, last, high,
                                                   None if source == HISTORY else boundary))
        return CleanupPlan(ranges, deferred)

    def plan(self, now=None, *, cancel=None):
        """Plan only. Does not write a backup, state file, audit, or database."""
        with maintenance_lock(self.path):
            return self._plan(self.wall_clock() if now is None else now, cancel or Event())

    def _backup(self, state, cancel):
        name = state.get("backup")
        if name and Path(name).name == name:
            backup = self.path.parent / "backups" / name
            with closing(_read_only(backup)) as saved:
                verify_backup_digest(backup)
                if _revision(saved) != HEAD_REVISION:
                    raise RuntimeError("kiwi_retention_backup_revision")
            return
        with closing(_read_only(self.path)) as source:
            backup = verified_backup(self.path, source, "before-kiwi-retention", cancel=cancel)
        state["backup"] = backup.name
        self._save_state(state)
        self._audit("backup_verified", filename=backup.name, sha256=verify_backup_digest(backup))

    def _delete_batch(self, conn, item, now, remaining, run_id, cancel):
        conn.execute("BEGIN IMMEDIATE")
        try:
            _check_cancel(cancel)
            if _revision(conn) != HEAD_REVISION:
                raise RuntimeError("kiwi_retention_unsupported_revision")
            if item.boundary and _current_reset(conn, item.profile_id, self.wall_clock()) != item.boundary.current_reset:
                conn.execute("ROLLBACK")
                return 0
            selection = _selection(item.profile_id, item.source, now, item.boundary)
            table, stamp, predicate, parameters = selection
            bounded = f"{predicate} AND {stamp} BETWEEN ? AND ? AND rowid<=?"
            bounds = (*parameters, item.first, item.last, item.max_rowid)
            rows = conn.execute(f"""DELETE FROM {table} WHERE rowid IN (
                   SELECT rowid FROM {table} WHERE {bounded} ORDER BY {stamp}, rowid LIMIT ?
                ) AND {bounded} RETURNING {stamp}""",
                (*bounds, min(remaining, _BATCH_SIZE), *bounds)).fetchall()
            if rows:
                conn.execute("""INSERT INTO monitoring_sync_runs (profile_id,source,finished,outcome)
                             VALUES (?, ?, ?, ?)""",
                             (item.profile_id, "retention:" + item.source, now, f"{run_id}:{len(rows)}"))
                self._audit("batch_before_commit", run_id=run_id, profile_id=item.profile_id, source=item.source,
                            count=len(rows), first=min(rows)[0], last=max(rows)[0])
            _check_cancel(cancel)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return len(rows)

    def run_daily(self, cancel):
        """Apply on a worker. The first observation and reboot only anchor the clock."""
        with maintenance_lock(self.path):
            now, steady = self.wall_clock(), self.steady_clock()
            run_id = uuid4().hex
            deleted = 0
            try:
                _check_cancel(cancel)
                state, due = self._clock_state(now, steady)
                if not due:
                    if state["wall"] == now:
                        self._save_state(state)
                    return "waiting"
                plan = self._plan(now, cancel)
                if plan.deferred_profiles:
                    self._audit("cycle_boundary_unconfirmed", run_id=run_id, profiles=plan.deferred_profiles)
                if not plan.ranges:
                    self._save_state({**state, "wall": now, "steady": steady})
                    return "empty"
                self._audit("plan", run_id=run_id, ranges=[asdict(item) for item in plan.ranges])
                self._backup(state, cancel)
                with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                             autocommit=True, timeout=0.35)) as conn:
                    for item in plan.ranges:
                        remaining = item.count
                        while remaining:
                            _check_cancel(cancel)
                            if abs((self.wall_clock() - now) - (self.steady_clock() - steady)) > 120:
                                raise RetentionClockError("kiwi_retention_clock_discontinuity")
                            n = self._delete_batch(conn, item, now, remaining, run_id, cancel)
                            if not n:
                                break
                            remaining -= n
                            deleted += n
                            if cancel.wait(0.01):
                                _check_cancel(cancel)
                with closing(_read_only(self.path)) as conn:
                    conn.set_progress_handler(cancel.is_set, 1000)
                    _integrity(conn)
                self._save_state({**state, "wall": now, "steady": steady})
                self._audit("complete", run_id=run_id, deleted=deleted, integrity="ok")
                return "completed"
            except Exception as error:
                self._audit("stopped", run_id=run_id, committed_rows=deleted,
                            reason="cancelled" if cancel.is_set() else type(error).__name__)
                raise
