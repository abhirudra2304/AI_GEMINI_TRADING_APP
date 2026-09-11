"""Secrets must never reach a log handler. Regression tests for log_redaction.

NOTE: every credential-shaped literal in this file is FAKE. The first draft of
these tests used the real ANGEL_API_KEY as the fixture - while building a tool
whose entire purpose is stopping that key reaching disk. Caught before push.
Never paste a live secret into a test, even a test about secrets.
"""
import logging
import re

import pytest

import log_redaction as lr

REAL_LEAK = (
    "Error occurred while making a POST request. Headers: {'Content-type': "
    "'application/json', 'X-PrivateKey': 'FAKEKEY9', 'X-UserType': 'USER', "
    "'Authorization': 'Bearer eyJhbGciOiJIUzUxMiJ9.eyJ1c2VybmFtZSI6IkEzMDU1MDIi.abcdEFGH1234'}"
)


def test_masks_private_key_but_keeps_the_key_name():
    out = lr.redact(REAL_LEAK, literals=[])
    assert "FAKEKEY9" not in out
    assert "X-PrivateKey" in out          # still debuggable
    assert lr.MASK in out


def test_masks_bearer_token_inside_a_header_dict():
    """The Authorization rule masks the entire value - including the word
    "Bearer" - which is stricter than masking only the token. Keep it that way:
    the key name is what makes the line debuggable, not the scheme."""
    out = lr.redact(REAL_LEAK, literals=[])
    assert "eyJhbGciOiJIUzUxMiJ9" not in out
    assert "Authorization" in out
    assert lr.MASK in out


def test_masks_bare_bearer_token_outside_a_dict():
    out = lr.redact("sent header Bearer eyJabcdEFGH1234.payload.sig now", literals=[])
    assert "eyJabcdEFGH1234" not in out
    assert "Bearer" in out          # scheme kept when it is not a quoted value


def test_masks_bare_jwt():
    s = "token=eyJhbGciOiJIUzUxMiJ9.eyJ1c2VybmFtZSI6IkEzMDU1MDIi.abcdEFGH1234 rest"
    out = lr.redact(s, literals=[])
    assert "eyJhbGciOiJIUzUxMiJ9" not in out
    assert "rest" in out


def test_masks_env_style_dump():
    out = lr.redact("ANGEL_API_KEY=FAKEKEY9\nGEMINI_API_KEY=AIzaSyFake123", literals=[])
    assert "FAKEKEY9" not in out and "AIzaSyFake123" not in out


def test_masks_literal_secret_in_any_shape(monkeypatch):
    monkeypatch.setenv("ANGEL_API_KEY", "SUPERSECRET1")
    out = lr.redact("the key is SUPERSECRET1 embedded oddly")
    assert "SUPERSECRET1" not in out


def test_leaves_ordinary_text_alone():
    s = "Fetching new candles for TAALTECH from 2026-09-11 13:05."
    assert lr.redact(s, literals=[]) == s


def test_short_values_are_not_masked(monkeypatch):
    """A 3-char secret would otherwise mangle unrelated log text."""
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "abc")
    assert "abc" in lr.redact("abcdefg normal text", )


def test_end_to_end_through_a_real_handler(caplog):
    """The whole point: a handler must never receive the secret."""
    lr.install_redaction()
    logger = logging.getLogger("smoke.redaction")
    with caplog.at_level(logging.ERROR, logger="smoke.redaction"):
        logger.error(REAL_LEAK)
    written = "\n".join(r.getMessage() for r in caplog.records)
    assert "FAKEKEY9" not in written
    assert "eyJhbGciOiJIUzUxMiJ9" not in written


def test_install_is_idempotent():
    lr.install_redaction()
    assert lr.install_redaction() is False
