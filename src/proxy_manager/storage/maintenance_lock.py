from contextlib import contextmanager

from PySide6.QtCore import QLockFile


class MaintenanceBusy(RuntimeError):
    pass


@contextmanager
def maintenance_lock(path):
    lock = QLockFile(str(path.resolve().with_suffix(path.suffix + ".maintenance.lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise MaintenanceBusy("database_maintenance_busy")
    try:
        yield
    finally:
        lock.unlock()
