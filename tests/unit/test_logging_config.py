"""Unit tests for configure_project_logging: it must raise exactly this project's own
loggers to INFO, and leave the root logger and third-party loggers (boto3, etc.) alone —
otherwise CloudWatch would get flooded with SDK-internal INFO logs just to see this
project's own per-call finish_reason/usage lines (llm_router.py)."""

import logging

from integrations.logging_config import _PROJECT_LOGGER_NAMES, configure_project_logging


def _reset_levels():
    """Loggers are process-global singletons; restore NOTSET after each test so one test's
    setLevel call can't leak into another."""
    logging.getLogger().setLevel(logging.WARNING)
    for name in (*_PROJECT_LOGGER_NAMES, "integrations.llm_router", "boto3", "botocore"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_raises_every_project_logger_to_info():
    _reset_levels()
    try:
        configure_project_logging()

        for name in _PROJECT_LOGGER_NAMES:
            assert logging.getLogger(name).level == logging.INFO
    finally:
        _reset_levels()


def test_a_child_logger_resolves_to_info_via_its_package_ancestor():
    """integrations.llm_router itself is never touched directly — it inherits INFO from the
    "integrations" logger above it, the same way any future integrations.* or handler-package
    submodule logger would."""
    _reset_levels()
    try:
        configure_project_logging()

        child = logging.getLogger("integrations.llm_router")
        assert child.level == logging.NOTSET  # not set directly
        assert child.getEffectiveLevel() == logging.INFO  # resolved from its ancestor
    finally:
        _reset_levels()


def test_root_logger_and_third_party_loggers_are_left_alone():
    _reset_levels()
    root_level_before = logging.getLogger().level
    try:
        configure_project_logging()

        assert logging.getLogger().level == root_level_before
        assert logging.getLogger("boto3").level == logging.NOTSET
        assert logging.getLogger("botocore").level == logging.NOTSET
    finally:
        _reset_levels()
