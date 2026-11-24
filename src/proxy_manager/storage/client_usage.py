from calendar import monthrange
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import re
import sqlite3
from time import time


BEIJING = timezone(timedelta(hours=8))


def billing_period(name: str, now: datetime) -> date | None:
    match = re.search(r"([0-9]{2})$", name)
    if not match or not 1 <= (day := int(match[1])) <= 31:
        return None
    today = now.astimezone(BEIJING).date()
    boundary = today.replace(day=min(day, monthrange(today.year, today.month)[1]))
    if today >= boundary:
        return boundary
    previous = today.replace(day=1) - timedelta(days=1)
    return previous.replace(day=min(day, previous.day))


class ClientUsage:
    def __init__(self, path):
        self.path = path

    def observe(self, panel_url, clients, *, captured=None):
        captured = time() if captured is None else captured
        now = datetime.fromtimestamp(captured, BEIJING)
        totals = {}
        panel = sha256(panel_url.rstrip("/").encode()).digest()
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS client_usage (
                    panel BLOB NOT NULL, node BLOB NOT NULL, period TEXT NOT NULL,
                    raw_up TEXT NOT NULL, raw_down TEXT NOT NULL,
                    used_up TEXT NOT NULL, used_down TEXT NOT NULL,
                    PRIMARY KEY (panel, node)
                )
            """)
            db.execute("BEGIN IMMEDIATE")
            for client in clients:
                if not client.name:
                    continue
                boundary = billing_period(client.name, now)
                up, down = client.up, client.down
                if up is None or down is None or boundary is None:
                    totals[client.name] = (up, down, boundary)
                    continue
                node = sha256(client.name.encode()).digest()
                period = boundary.isoformat()
                saved = db.execute(
                    "SELECT period, raw_up, raw_down, used_up, used_down FROM client_usage WHERE panel=? AND node=?",
                    (panel, node),
                ).fetchone()
                used_up = used_down = 0
                if saved:
                    old_period, old_up, old_down, used_up, used_down = saved
                    old_up, old_down = int(old_up), int(old_down)
                    used_up, used_down = int(used_up), int(used_down)
                    if period < old_period:
                        totals[client.name] = (None, None, boundary)
                        continue
                    if period == old_period:
                        used_up += up - old_up if up >= old_up else up
                        used_down += down - old_down if down >= old_down else down
                    else:
                        used_up = used_down = 0
                values = (period, str(up), str(down), str(used_up), str(used_down))
                if saved != values:
                    db.execute(
                        "INSERT INTO client_usage VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(panel, node) DO UPDATE SET period=excluded.period, raw_up=excluded.raw_up, "
                        "raw_down=excluded.raw_down, used_up=excluded.used_up, used_down=excluded.used_down",
                        (panel, node, *values),
                    )
                totals[client.name] = (used_up, used_down, boundary)
        return totals
