"""Angel bearer tokens must not escape via exceptions or via entry points other than main.py."""
import logging

import pytest

import data_broker as db_module
import log_redaction
from data_broker import DataBroker

FAKE_JWT = "eyJhbGciOiJIUzUxMiJ9.eyJ1c2VybmFtZSI6IlQxMjM0NSJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


@pytest.fixture
def broker(monkeypatch):
    b = DataBroker.__new__(DataBroker)
    monkeypatch.setattr(b, "_enforce_api_rate_limit", lambda: None, raising=False)
    return b


def test_importing_data_broker_installs_redaction():
    assert log_redaction._installed is True


def test_exception_args_are_scrubbed_and_type_preserved(broker):
    def boom(*a, **k):
        raise ConnectionResetError(
            f"POST failed. Headers: {{'Authorization': 'Bearer {FAKE_JWT}', 'X-UserType': 'USER'}}")

    with pytest.raises(ConnectionResetError) as ei:
        broker._call_rest_api(boom)
    assert FAKE_JWT not in str(ei.value)
    assert "X-UserType" in str(ei.value)


def test_nested_and_chained_exceptions_are_scrubbed(broker):
    def boom(*a, **k):
        try:
            raise OSError(f"inner Bearer {FAKE_JWT}")
        except OSError as inner:
            raise ConnectionError("Connection aborted.", inner)

    with pytest.raises(ConnectionError) as ei:
        broker._call_rest_api(boom)
    assert FAKE_JWT not in repr(ei.value)
    assert FAKE_JWT not in str(ei.value.__context__)


def test_successful_call_returns_untouched(broker):
    assert broker._call_rest_api(lambda x: {"status": True, "x": x}, 7) == {"status": True, "x": 7}


def test_log_handler_redacts_preformatted_headers_dump():
    h = logging.StreamHandler()
    msg = f"Error occurred while making a POST request. Headers: {{'Authorization': 'Bearer {FAKE_JWT}'}}"
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, None, None)
    h.handle(rec)
    assert FAKE_JWT not in rec.getMessage()


def test_percent_placeholder_inside_quoted_secret_still_formats_and_redacts():
    h = logging.StreamHandler()
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1,
                            "Headers: {'Authorization': 'Bearer %s'}", (FAKE_JWT,), None)
    h.handle(rec)
    assert FAKE_JWT not in rec.getMessage()


def test_smallcap_index_alias_resolves_to_angel_name():
    b = DataBroker.__new__(DataBroker)
    key = b._normalize_symbol_key("NIFTY SMLCAP 100")
    b.symbol_index = {key: [{"token": "99926032", "symbol": "NIFTY SMLCAP 100"}]}
    assert b._resolve_symbol_row("NIFTY SMALLCAP 100")["token"] == "99926032"
