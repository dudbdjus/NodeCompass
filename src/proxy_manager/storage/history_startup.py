from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .kiwi_retention import KiwiRetention
from .upgrade_backup import HEAD_REVISION, _integrity, _read_only, _revision
from .vps_repository import ssh_retention_cutoffs


def prepare_history_startup(path: Path, *, requested: bool) -> bool:
    """A first launch performs a dry run. A later launch may opt in explicitly."""
    receipt = path.with_suffix(path.suffix + ".history-preflight.json")
    if receipt.is_file():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            previously_validated = (data.get("revision") == HEAD_REVISION and data.get("integrity") == "ok"
                                    and data.get("first_window_started") is True)
        except (ValueError, AttributeError):
            previously_validated = False
        if previously_validated and not requested:
            return False
    else:
        previously_validated = False

    observed = datetime.now(UTC)
    raw_cutoff, minute_cutoff = ssh_retention_cutoffs(observed.timestamp())
    with closing(_read_only(path)) as conn:
        if _revision(conn) != HEAD_REVISION:
            raise RuntimeError("vps_history_preflight_revision")
        _integrity(conn)
        ssh_plan = {}
        for table in ("vps_network_samples", "vps_system_samples"):
            ssh_plan[table] = conn.execute(
                f"SELECT count(*),min(captured),max(captured) FROM {table} WHERE captured<?", (raw_cutoff,)
            ).fetchone()
        ssh_plan["vps_network_rollups"] = conn.execute(
            "SELECT count(*),min(bucket),max(bucket) FROM vps_network_rollups WHERE period='minute' AND bucket<?",
            (minute_cutoff,),
        ).fetchone()
    retention = KiwiRetention(path)
    plan = retention.plan(observed.timestamp())
    _write_receipt(receipt, {"revision": HEAD_REVISION, "integrity": "ok",
                             "first_window_started": previously_validated,
                             "observed_utc": observed.isoformat(), "ssh_dry_run": ssh_plan,
                             "kiwivm_dry_run": asdict(plan)})
    return previously_validated and requested


def confirm_history_startup(path: Path) -> None:
    receipt = path.with_suffix(path.suffix + ".history-preflight.json")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if data.get("first_window_started") is not True:
        data["first_window_started"] = True
        _write_receipt(receipt, data)


def _write_receipt(receipt: Path, data) -> None:
    temporary = NamedTemporaryFile(dir=receipt.parent, delete=False, encoding="utf-8", mode="w",
                                   prefix=".history-preflight-")
    temporary_path = Path(temporary.name)
    try:
        with temporary as output:
            json.dump(data, output, indent=2)
        temporary_path.replace(receipt)
    finally:
        temporary_path.unlink(missing_ok=True)
