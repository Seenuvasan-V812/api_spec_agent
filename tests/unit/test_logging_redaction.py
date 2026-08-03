import logging

from openapi_agent.logging_utils import REDACTED, get_logger, redact, register_secret


def test_registered_secret_is_masked():
    register_secret("super-secret-value-123")
    assert "super-secret-value-123" not in redact("key is super-secret-value-123 ok")


def test_api_key_shapes_are_masked_without_registration():
    for sample in (
        "AIzaSyB1234567890abcdefghijklmnopqrstuv",
        "sk-ant-abc123def456ghi789jkl",
        "sk-abcdefghijklmnopqrstuvwx",
    ):
        assert sample not in redact(f"calling with {sample}")


def test_key_value_pairs_masked():
    text = redact('api_key="abcdef1234567890"')
    assert "abcdef1234567890" not in text
    assert REDACTED in text


def test_logger_filter_applies(caplog):
    register_secret("another-secret-98765")
    logger = get_logger("test")
    logger.propagate = True
    with caplog.at_level(logging.INFO, logger="openapi_agent.test"):
        logger.info("value=%s", "another-secret-98765")
    assert "another-secret-98765" not in caplog.text
