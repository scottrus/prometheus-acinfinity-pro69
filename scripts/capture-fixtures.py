#!/usr/bin/env python3
"""Capture redacted API fixtures for the test suite.

Reads ACINFINITY_EMAIL and ACINFINITY_PASSWORD from the environment ONLY. Writes:

    tests/fixtures/devInfoListAll.json   one live device-list response
    tests/fixtures/dataPage.json         one history page for the first device

Every value in the REDACT set is replaced before anything is written or printed.
The login response is never written: it echoes the password back in
`appPasswordl`, and carries `refreshToken` and `secretId`.

Re-run after a firmware update or an API change, then re-read the fixture tests.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.acinfinityserver.com/api"
USER_AGENT = os.environ.get("ACINFINITY_USER_AGENT", "okhttp/3.10.0")
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# Keys whose values must never reach a fixture or a terminal. Names, not patterns:
# a substring filter fails open on a key it did not predict.
REDACT = {
    "appEmail": "redacted@example.com",
    "wifiName": "REDACTED",
    "devMacAddr": "00:00:00:00:00:00",
    "key": "REDACTED",
    "crcKey": "REDACTED",
    "lisence": "REDACTED",
    "uuid": "REDACTED",
    "did": "REDACTED",
    "appReqId": "REDACTED",
}


def redact(node):
    if isinstance(node, dict):
        return {
            k: (REDACT[k] if k in REDACT and node[k] not in (None, "") else redact(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [redact(v) for v in node]
    return node


def post(path: str, data: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode())
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    if token:
        req.add_header("token", token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    email = os.environ.get("ACINFINITY_EMAIL")
    password = os.environ.get("ACINFINITY_PASSWORD")
    if not email or not password:
        print("set ACINFINITY_EMAIL and ACINFINITY_PASSWORD in the environment", file=sys.stderr)
        return 2

    login = post("/user/appUserLogin", {"appEmail": email, "appPasswordl": password[:25]})
    if login.get("code") != 200:
        print(f"login failed: code={login.get('code')} msg={login.get('msg')}", file=sys.stderr)
        return 1
    token = login["data"]["appId"]
    del login  # echoes the password; drop it at once

    devices = post("/user/devInfoListAll", {"userId": token}, token)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "devInfoListAll.json").write_text(json.dumps(redact(devices), indent=2) + "\n")
    print(f"devInfoListAll: code={devices.get('code')} devices={len(devices.get('data') or [])}")

    first = (devices.get("data") or [{}])[0]
    dev_id = first.get("devId")
    if not dev_id:
        print("no device; skipping history", file=sys.stderr)
        return 0
    now = int(time.time())
    history = post(
        "/log/dataPage",
        {
            "appId": token,
            "devId": dev_id,
            "time": now - 2 * 3600,
            "endTime": now,
            "pageNum": 1,
            "pageSize": 2000,
        },
        token,
    )
    (OUT / "dataPage.json").write_text(json.dumps(redact(history), indent=2) + "\n")
    rows = (history.get("data") or {}).get("rows") or []
    print(f"dataPage: code={history.get('code')} rows={len(rows)}")
    if rows:
        print("row keys:", sorted(rows[0].keys()))
        print(
            "first createTime:",
            rows[0].get("createTime"),
            "last createTime:",
            rows[-1].get("createTime"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
