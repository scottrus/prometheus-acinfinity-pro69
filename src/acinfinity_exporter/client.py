"""The AC Infinity cloud API, reduced to the three reads this project makes.

THERE IS NO LOCAL API. The controller talks only to `www.acinfinityserver.com`, and
so does every client that has been written for it. Each collection is one WAN
round trip to a vendor cloud.

This module owns the I/O and nothing else. Every decode step lives in
`collector.py` and `backfill.py`, so the arithmetic is tested against captured
fixtures with no network and no credential.

Four facts about the API shape everything here:

1. HTTP 200 does not mean success. The body carries a `code` field, and 200 there is
   the only success value. A session expiry arrives as HTTP 200 with a non-200 body
   code, never as HTTP 401.
2. The login token rotates. Two logins seconds apart return two different
   32-character tokens. It is a session, not a user id.
3. The login response echoes the password back, in a field named `appPasswordl`.
   It also carries `refreshToken` and `secretId`. This module never logs it and
   discards it as soon as the token is copied out.
4. The device-list response carries the account email on every device record. A
   raw response must never reach a log line at any level.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

API_BASE = "https://www.acinfinityserver.com/api"
LOGIN_PATH = "/user/appUserLogin"
DEVICES_PATH = "/user/devInfoListAll"
HISTORY_PATH = "/log/dataPage"

# The server truncates the password to 25 characters. Both reference clients that
# checked disagree on whether a longer password is truncated or rejected, so the
# client truncates first and the question never reaches the server.
PASSWORD_MAX_CHARS = 25

# An honest identity. Verified accepted by the API on 2026-09-02. The reference
# clients imitate the vendor app's User-Agent; that is not necessary and this
# project does not do it by default.
DEFAULT_USER_AGENT = (
    f"prometheus-acinfinity-pro69/{__version__} "
    "(+https://github.com/scottrus/prometheus-acinfinity-pro69)"
)

# After a failed login the client refuses to try again for this long. A wrong
# password would otherwise produce one login attempt per poll, forever.
LOGIN_BACKOFF_SECONDS = 300

# A transport takes (url, form fields, headers) and returns (http status, body).
# Tests inject one; production uses the urllib implementation below.
Transport = Callable[[str, Mapping[str, str], Mapping[str, str]], tuple[int, bytes]]


class ACInfinityError(RuntimeError):
    """Base class. Every failure the client raises is one of these."""


class AuthenticationError(ACInfinityError):
    """Login was refused, or is in backoff after a refusal."""


class ApiError(ACInfinityError):
    """The API answered with a non-success body code, after any re-login."""

    def __init__(self, path: str, code: Any, msg: Any) -> None:
        super().__init__(f"{path} returned code={code!r} msg={msg!r}")
        self.path = path
        self.code = code
        self.msg = msg


class TransportError(ACInfinityError):
    """The request did not complete: DNS, TLS, timeout, or a non-JSON body."""


def urllib_transport(timeout: float) -> Transport:
    """Build the production transport. Form-encoded POST, JSON in, bytes out."""

    def post(url: str, form: Mapping[str, str], headers: Mapping[str, str]) -> tuple[int, bytes]:
        body = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx still carries a body. Return it so the caller reads the code.
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"POST {url} failed: {exc}") from exc

    return post


class ACInfinityClient:
    """Login, list devices, page history. Nothing here writes to a controller."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        transport: Transport | None = None,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        api_base: str = API_BASE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._email = email
        self._password = password[:PASSWORD_MAX_CHARS]
        self._transport = transport or urllib_transport(timeout)
        self._user_agent = user_agent
        self._api_base = api_base.rstrip("/")
        self._clock = clock
        self._token: str | None = None
        self._login_blocked_until = 0.0
        self.login_count = 0

    # ------------------------------------------------------------------ login

    def login(self) -> None:
        """Obtain a session token. Raises AuthenticationError on refusal or backoff."""
        now = self._clock()
        if now < self._login_blocked_until:
            remaining = int(self._login_blocked_until - now)
            raise AuthenticationError(f"login refused earlier; retry blocked for {remaining}s")

        status, body = self._post(
            LOGIN_PATH,
            {"appEmail": self._email, "appPasswordl": self._password},
            token=None,
        )
        self.login_count += 1
        code = body.get("code")
        if status != 200 or code != 200:
            self._login_blocked_until = now + LOGIN_BACKOFF_SECONDS
            self._token = None
            # The body is not included. It would echo the password on some failure paths.
            raise AuthenticationError(
                f"login refused: http={status} code={code!r} msg={body.get('msg')!r}"
            )
        token = (body.get("data") or {}).get("appId")
        if not token:
            self._login_blocked_until = now + LOGIN_BACKOFF_SECONDS
            raise AuthenticationError("login succeeded but returned no appId token")
        self._token = str(token)
        log.info("logged in to the AC Infinity API (login #%d)", self.login_count)

    @property
    def has_session(self) -> bool:
        return self._token is not None

    # ------------------------------------------------------------------ reads

    def list_devices(self) -> list[dict[str, Any]]:
        """POST devInfoListAll. One call returns every controller, port and sensor."""
        body = self._call(DEVICES_PATH, lambda token: {"userId": token})
        data = body.get("data")
        if not isinstance(data, list):
            raise ApiError(DEVICES_PATH, body.get("code"), "data is not a list")
        return data

    def history_page(
        self,
        dev_id: str,
        start: int,
        end: int,
        page_size: int = 2000,
    ) -> list[dict[str, Any]]:
        """POST log/dataPage for one window. Rows come back oldest first.

        `pageNum` is sent because the API expects it, and it is ignored by the
        server. Paging is done by the caller with a time cursor: the next request
        starts at the last row's `createTime` plus one.
        """
        body = self._call(
            HISTORY_PATH,
            lambda token: {
                "appId": token,
                "devId": str(dev_id),
                "time": str(int(start)),
                "endTime": str(int(end)),
                "pageNum": "1",
                "pageSize": str(int(page_size)),
            },
        )
        data = body.get("data") or {}
        rows = data.get("rows") if isinstance(data, dict) else None
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise ApiError(HISTORY_PATH, body.get("code"), "rows is not a list")
        return rows

    # --------------------------------------------------------------- plumbing

    def _call(
        self,
        path: str,
        form_for: Callable[[str], Mapping[str, str]],
    ) -> dict[str, Any]:
        """Authenticated read with one transparent re-login.

        The API signals an expired session as HTTP 200 plus a non-200 body code. So
        the rule is simple: any non-success body code on a read earns exactly one
        fresh login and one retry. A second failure is raised as ApiError.

        Reads only. A write must never be replayed this way, because the first
        attempt may have been applied before the expiry response was sent.
        """
        if self._token is None:
            self.login()
        assert self._token is not None
        status, body = self._post(path, form_for(self._token), token=self._token)
        if status == 200 and body.get("code") == 200:
            return body

        log.warning(
            "%s answered http=%s code=%r msg=%r; logging in again and retrying once",
            path,
            status,
            body.get("code"),
            body.get("msg"),
        )
        self._token = None
        self.login()
        assert self._token is not None
        status, body = self._post(path, form_for(self._token), token=self._token)
        if status == 200 and body.get("code") == 200:
            return body
        raise ApiError(path, body.get("code"), body.get("msg"))

    def _post(
        self,
        path: str,
        form: Mapping[str, str],
        *,
        token: str | None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {
            "User-Agent": self._user_agent,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        if token:
            headers["token"] = token
        status, raw = self._transport(self._api_base + path, form, headers)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            # The body is not logged: an HTML error page is harmless, but a JSON
            # body that failed for another reason may hold the account email.
            raise TransportError(f"{path} returned http={status} with a non-JSON body") from exc
        if not isinstance(body, dict):
            raise TransportError(f"{path} returned http={status} with a non-object body")
        return status, body
