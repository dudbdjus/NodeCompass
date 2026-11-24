import logging
from pathlib import Path
from traceback import walk_tb


def record_boot_failure(directory: Path | None, stage: str, error: Exception) -> None:
    if directory is None:
        return
    # Keep frame identifiers for support without recording values or exception text.
    frames = ", ".join(
        f"{Path(frame.f_code.co_filename).name}:{line} {frame.f_code.co_name}"
        for frame, line in walk_tb(error.__traceback__)
    )
    try:
        (directory / "startup-error.log").write_text(
            f"{stage}: {type(error).__name__}\n{frames}\n", encoding="utf-8",
        )
    except OSError:
        pass


def suppress_http_diagnostics() -> None:
    # HTTPX logs complete request URLs. The provider puts its key in the path.
    for name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
    ):
        logging.getLogger(name).disabled = True
