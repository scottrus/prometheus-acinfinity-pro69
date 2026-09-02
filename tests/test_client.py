"""Client behaviour, with a fake transport and no network.

The interesting cases are the ones that silently break in production: a session
expiry that arrives as HTTP 200, a login that must not be retried in a tight loop,
and a response body that must never be logged.
"""

from __future__ import annotations

import json
import logging

import pytest

from acinfinity_exporter.client import (
    DEVICES_PATH,
    HISTORY_PATH,
    LOGIN_BACKOFF_SECONDS,
    LOGIN_PATH,
    PASSWORD_MAX_CHARS,
    ACInfinityClient,
    ApiError,
    AuthenticationError,
    TransportError,
)

SECRET = "hunter2-hunter2-hunter2-hunter2"


class FakeTransport:
    """Scripted responses per path, in order. Records every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.scripts: dict[str, list[tuple[int, dict]]] = {}
        self.token_counter = 0

    def script(self, path: str, *responses: tuple[int, dict]) -> None:
        self.scripts.setdefault(path, []).extend(responses)

    def __call__(self, url: str, form, headers):
        path = url.split("/api", 1)[1]
        self.calls.append((path, dict(form), dict(headers)))
        queue = self.scripts.get(path)
        if not queue:
            raise AssertionError(f"unscripted request to {path}")
        status, body = queue.pop(0)
        if path == LOGIN_PATH and "__rotate__" in body:
            self.token_counter += 1
            body = {
                "code": 200,
                "msg": "success.",
                "data": {"appId": f"token-{self.token_counter}", "appPasswordl": SECRET},
            }
        return status, json.dumps(body).encode()


def login_ok():
    return (200, {"__rotate__": True})


def devices_ok():
    return (200, {"code": 200, "msg": "success.", "data": [{"devId": "1", "deviceInfo": {}}]})


def expired():
    return (200, {"code": 10003, "msg": "Login Expired", "data": None})


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def clock():
    state = {"now": 1000.0}

    def now():
        return state["now"]

    now.advance = lambda seconds: state.__setitem__("now", state["now"] + seconds)  # type: ignore[attr-defined]
    return now


def make_client(transport, clock):
    return ACInfinityClient("user@example.com", SECRET, transport=transport, clock=clock)


# ---------------------------------------------------------------------- login


def test_login_sends_the_vendor_field_names_and_truncates_the_password(transport, clock):
    transport.script(LOGIN_PATH, login_ok())
    client = make_client(transport, clock)
    client.login()
    path, form, headers = transport.calls[0]
    assert path == LOGIN_PATH
    assert form["appEmail"] == "user@example.com"
    assert form["appPasswordl"] == SECRET[:PASSWORD_MAX_CHARS]
    assert "token" not in headers
    assert headers["User-Agent"].startswith("prometheus-acinfinity-pro69/")


def test_token_goes_in_the_header_and_the_userid_field(transport, clock):
    transport.script(LOGIN_PATH, login_ok())
    transport.script(DEVICES_PATH, devices_ok())
    client = make_client(transport, clock)
    assert client.list_devices() == [{"devId": "1", "deviceInfo": {}}]
    path, form, headers = transport.calls[1]
    assert path == DEVICES_PATH
    assert headers["token"] == "token-1"
    assert form["userId"] == "token-1"


def test_login_refusal_raises_and_enters_backoff(transport, clock):
    transport.script(LOGIN_PATH, (200, {"code": 400, "msg": "Email or password is wrong"}))
    client = make_client(transport, clock)
    with pytest.raises(AuthenticationError, match="refused"):
        client.login()
    # A second attempt inside the backoff window never reaches the vendor.
    with pytest.raises(AuthenticationError, match="blocked"):
        client.login()
    assert len(transport.calls) == 1
    clock.advance(LOGIN_BACKOFF_SECONDS + 1)
    transport.script(LOGIN_PATH, login_ok())
    client.login()
    assert client.has_session


def test_login_response_is_never_logged(transport, clock, caplog):
    """The login body echoes the password. Not at DEBUG, not on failure, not ever."""
    transport.script(
        LOGIN_PATH, (200, {"code": 400, "msg": "nope", "data": {"appPasswordl": SECRET}})
    )
    transport.script(LOGIN_PATH, login_ok())
    client = make_client(transport, clock)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AuthenticationError):
            client.login()
        clock.advance(LOGIN_BACKOFF_SECONDS + 1)
        client.login()
    assert SECRET not in caplog.text
    assert SECRET[:PASSWORD_MAX_CHARS] not in caplog.text


# --------------------------------------------------------------- session expiry


def test_expiry_as_http_200_triggers_one_relogin_and_a_retry(transport, clock):
    """The defect the upstream exporter had: it re-authenticated on HTTP 401 only."""
    transport.script(LOGIN_PATH, login_ok(), login_ok())
    transport.script(DEVICES_PATH, expired(), devices_ok())
    client = make_client(transport, clock)
    assert client.list_devices()
    paths = [c[0] for c in transport.calls]
    assert paths == [LOGIN_PATH, DEVICES_PATH, LOGIN_PATH, DEVICES_PATH]
    assert transport.calls[3][2]["token"] == "token-2"  # the rotated token is used


def test_second_failure_after_relogin_raises_api_error(transport, clock):
    transport.script(LOGIN_PATH, login_ok(), login_ok())
    transport.script(DEVICES_PATH, expired(), (200, {"code": 500, "msg": "server fault"}))
    client = make_client(transport, clock)
    with pytest.raises(ApiError) as caught:
        client.list_devices()
    assert caught.value.code == 500
    assert len(transport.calls) == 4  # no third attempt


def test_http_401_is_also_a_retry_case(transport, clock):
    transport.script(LOGIN_PATH, login_ok(), login_ok())
    transport.script(DEVICES_PATH, (401, {"code": 401, "msg": "unauthorized"}), devices_ok())
    client = make_client(transport, clock)
    assert client.list_devices()


def test_api_error_message_carries_code_and_msg_but_no_body(transport, clock):
    transport.script(LOGIN_PATH, login_ok(), login_ok())
    transport.script(
        DEVICES_PATH,
        expired(),
        (200, {"code": 500, "msg": "fault", "data": [{"appEmail": "owner@example.org"}]}),
    )
    client = make_client(transport, clock)
    with pytest.raises(ApiError) as caught:
        client.list_devices()
    assert "owner@example.org" not in str(caught.value)


# ------------------------------------------------------------------- history


def test_history_page_sends_the_cursor_fields_as_strings(transport, clock):
    transport.script(LOGIN_PATH, login_ok())
    transport.script(
        HISTORY_PATH, (200, {"code": 200, "msg": "ok", "data": {"rows": [{"createTime": 5}]}})
    )
    client = make_client(transport, clock)
    rows = client.history_page("123", 100, 200, page_size=50)
    assert rows == [{"createTime": 5}]
    form = transport.calls[1][1]
    assert form == {
        "appId": "token-1",
        "devId": "123",
        "time": "100",
        "endTime": "200",
        "pageNum": "1",
        "pageSize": "50",
    }


def test_history_page_with_null_rows_is_empty_not_an_error(transport, clock):
    transport.script(LOGIN_PATH, login_ok())
    transport.script(HISTORY_PATH, (200, {"code": 200, "msg": "ok", "data": {"rows": None}}))
    client = make_client(transport, clock)
    assert client.history_page("123", 100, 200) == []


# ------------------------------------------------------------------ transport


def test_non_json_body_is_a_transport_error_without_the_body(transport, clock):
    class HtmlTransport:
        def __call__(self, url, form, headers):
            return 502, b"<html>Bad Gateway owner@example.org</html>"

    client = ACInfinityClient("u", "p", transport=HtmlTransport(), clock=clock)
    with pytest.raises(TransportError) as caught:
        client.login()
    assert "owner@example.org" not in str(caught.value)
