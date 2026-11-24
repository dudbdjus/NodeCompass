from collections.abc import Callable
from threading import Thread


class Worker(Thread):
    def __init__(self, job: Callable[[], object]):
        super().__init__(name="ProxyManager operation")
        self.job = job
        self.result = None
        self.failed = False

    def run(self) -> None:
        try:
            self.result = self.job()
        except Exception:
            self.failed = True
