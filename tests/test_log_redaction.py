import logging

from proxy_manager.logging_config import suppress_http_diagnostics


def test_sensitive_http_loggers_are_disabled(caplog):
    suppress_http_diagnostics()
    with caplog.at_level(logging.DEBUG):
        for logger in ("httpx", "httpcore", "httpcore.http11"):
            logging.getLogger(logger).debug("fixture-secret-in-url")
    assert "fixture-secret-in-url" not in caplog.text

def test_startup_diagnostics_exclude_error_message_and_paths(tmp_path):
    from proxy_manager.logging_config import record_boot_failure

    try:
        raise ValueError("fixture-secret-in-error")
    except ValueError as error:
        record_boot_failure(tmp_path, "database", error)
    log = (tmp_path / "startup-error.log").read_text(encoding="utf-8")
    assert "database: ValueError" in log
    assert "test_log_redaction.py" in log
    assert "fixture-secret-in-error" not in log
    assert str(tmp_path) not in log
