"""Tests for app.logging_config.configure_logging.

The ``app`` logger is a process-wide singleton, so the fixture snapshots and restores its level
and handlers around each test — otherwise configuring it here would leak into (or be leaked into
by) the rest of the suite, which imports ``app.main`` (and so calls ``configure_logging``).
"""

import logging

import pytest

from app import logging_config


@pytest.fixture
def clean_app_logger():
    app_logger = logging.getLogger("app")
    saved_level = app_logger.level
    saved_handlers = app_logger.handlers[:]
    # Start each test without this module's handler so a fresh configure is observable.
    app_logger.handlers = [
        h
        for h in app_logger.handlers
        if getattr(h, "name", None) != logging_config._HANDLER_NAME
    ]
    try:
        yield app_logger
    finally:
        app_logger.setLevel(saved_level)
        app_logger.handlers = saved_handlers


def _our_handlers(app_logger: logging.Logger) -> list[logging.Handler]:
    return [
        h
        for h in app_logger.handlers
        if getattr(h, "name", None) == logging_config._HANDLER_NAME
    ]


def test_adds_one_stdout_handler_at_info(clean_app_logger, monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)  # exercise the INFO default
    logging_config.configure_logging()

    handlers = _our_handlers(clean_app_logger)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert clean_app_logger.level == logging.INFO


def test_is_idempotent(clean_app_logger):
    logging_config.configure_logging()
    logging_config.configure_logging()

    assert len(_our_handlers(clean_app_logger)) == 1  # not stacked


def test_respects_log_level_env(clean_app_logger, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    logging_config.configure_logging()

    assert clean_app_logger.level == logging.WARNING


def test_an_app_info_log_reaches_the_handler(clean_app_logger, monkeypatch):
    # The point of the module: an app.* INFO record must actually be emitted, not filtered by
    # an inherited WARNING level. Prove it end-to-end through a captured stream.
    import io

    monkeypatch.delenv("LOG_LEVEL", raising=False)  # default INFO
    logging_config.configure_logging()
    buffer = io.StringIO()
    handler = _our_handlers(clean_app_logger)[0]
    handler.setStream(buffer)  # redirect our stdout handler at the buffer

    logging.getLogger("app.stocks.somewhere").info("hello-info")

    assert "hello-info" in buffer.getvalue()
