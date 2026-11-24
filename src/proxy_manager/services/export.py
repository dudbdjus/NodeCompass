import csv
from ipaddress import ip_address
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TextIO

from proxy_manager.storage.models import Proxy
from proxy_manager.storage.repositories import Repository

CSV_FIELDS = (
    "provider_id", "address", "port_http", "port_socks", "username", "country", "status",
    "order_id", "expires_on",
)


def _spreadsheet_text(value: str | None) -> str:
    if not value:
        return ""
    stripped = value.lstrip()
    if value[0] in "\t\r\n" or (stripped and stripped[0] in "=+-@"):
        return "'" + value
    return value


def _csv(handle: TextIO, rows: list[Proxy]) -> None:
    writer = csv.writer(handle)
    writer.writerow(CSV_FIELDS)
    for row in rows:
        writer.writerow((
            _spreadsheet_text(row.provider_id), row.address, row.port_http, row.port_socks,
            _spreadsheet_text(row.username), _spreadsheet_text(row.country),
            _spreadsheet_text(row.status), _spreadsheet_text(row.order_id),
            row.expires_on,
        ))


def _txt(handle: TextIO, rows: list[Proxy]) -> None:
    for row in rows:
        address = ip_address(row.address)
        host = f"[{address}]" if address.version == 6 else str(address)
        for protocol, port in (("http", row.port_http), ("socks5", row.port_socks)):
            if port is not None:
                handle.write(f"{protocol}://{host}:{port}\n")


class ExportService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def run(self, path: Path, rows: list[Proxy]) -> int:
        suffix = path.suffix.lower()
        if suffix not in {".csv", ".txt"}:
            raise ValueError("unsupported_export_format")
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", dir=path.parent, prefix=".proxy-export-", delete=False,
                encoding="utf-8-sig" if suffix == ".csv" else "utf-8", newline="",
            ) as handle:
                temporary = Path(handle.name)
                if suffix == ".csv":
                    _csv(handle, rows)
                else:
                    _txt(handle, rows)
            temporary.replace(path)
        except (OSError, ValueError):
            self.repository.log_operation("export", "failed")
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.repository.log_operation("export", "success")
        return len(rows)
