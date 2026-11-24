import os
from pathlib import Path


def data_directory(explicit=None) -> Path:
    override = explicit if explicit is not None else os.environ.get("PROXY_MANAGER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ["LOCALAPPDATA"]) / "ProxyManager"
