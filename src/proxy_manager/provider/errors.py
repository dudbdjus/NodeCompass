class ProviderError(Exception):
    def __init__(self, kind: str, status: int | None = None):
        self.kind = kind
        self.status = status
        super().__init__(kind if status is None else f"{kind} (HTTP {status})")
