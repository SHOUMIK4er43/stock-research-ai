"""Part 6C-A: structured local logging and secret redaction. Fully offline - writes
only to a tmp_path log file, never anywhere real."""

import logging

from stock_research_ai.logging_setup import RedactingFilter, configure_logging, get_logger


def _read_log(path) -> str:
    return path.read_text()


def test_configure_logging_creates_the_log_file_and_parent_directory(tmp_path) -> None:
    log_path = tmp_path / "nested" / "service.log"
    logger = configure_logging(log_path)
    logger.info("hello")
    for handler in logger.handlers:
        handler.flush()
    assert log_path.exists()
    assert "hello" in _read_log(log_path)


def test_log_lines_include_timestamp_severity_and_component(tmp_path) -> None:
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    logger = get_logger("jobs")
    logger.warning("job %s failed with %s", "job-1", "ANALYSIS_FAILED")
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    content = _read_log(log_path)
    assert "WARNING" in content
    assert "stock_research_ai.jobs" in content
    assert "job-1" in content
    assert "ANALYSIS_FAILED" in content


def test_configure_logging_is_idempotent_and_never_duplicates_handlers(tmp_path) -> None:
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    configure_logging(log_path)
    logger = get_logger("startup")
    logger.info("started once")
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    content = _read_log(log_path)
    assert content.count("started once") == 1


def test_known_secret_value_is_redacted_from_a_log_message(tmp_path) -> None:
    log_path = tmp_path / "service.log"
    configure_logging(log_path, known_secrets=["sk-ant-supersecretvalue"])
    logger = get_logger("analyst")
    logger.error("Anthropic call failed for key sk-ant-supersecretvalue")
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    content = _read_log(log_path)
    assert "sk-ant-supersecretvalue" not in content
    assert "[REDACTED]" in content


def test_known_secret_value_is_redacted_even_when_passed_as_a_log_arg(tmp_path) -> None:
    log_path = tmp_path / "service.log"
    configure_logging(log_path, known_secrets=["sk-ant-argvalue"])
    logger = get_logger("analyst")
    logger.error("credential in use: %s", "sk-ant-argvalue")
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    content = _read_log(log_path)
    assert "sk-ant-argvalue" not in content


def test_authorization_bearer_header_is_redacted_structurally(tmp_path) -> None:
    log_path = tmp_path / "service.log"
    # No known_secrets configured at all - this must still be caught by the
    # structural Authorization-header pattern, not by knowing the token value ahead
    # of time.
    configure_logging(log_path)
    logger = get_logger("http")
    logger.info("incoming header: Authorization: Bearer abc123.def456-XYZ")
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    content = _read_log(log_path)
    assert "abc123.def456-XYZ" not in content
    assert "Authorization: Bearer [REDACTED]" in content


def test_redaction_never_corrupts_ordinary_research_text_containing_secretary(tmp_path) -> None:
    """The exact false-positive the instructions warn about: a real filing signatory
    line containing the word "Secretary" must never be mangled."""
    log_path = tmp_path / "service.log"
    configure_logging(log_path, known_secrets=["sk-ant-realkey"])
    logger = get_logger("documents")
    logger.info("Signed: Sanjay Abhyankar, Company Secretary")
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    content = _read_log(log_path)
    assert "Company Secretary" in content
    assert "[REDACTED]" not in content


def test_redacting_filter_leaves_unrelated_text_completely_untouched() -> None:
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="Revenue grew 12%% year over year", args=(), exc_info=None,
    )
    filt = RedactingFilter(known_secrets=["sk-ant-unused"])
    assert filt.filter(record) is True
    assert record.msg == "Revenue grew 12%% year over year"


def test_log_file_gets_restrictive_permissions_best_effort(tmp_path) -> None:
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600
