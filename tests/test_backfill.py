"""History decode and paging, replayed offline against a captured page.

FIXTURE PROVENANCE. `fixtures/dataPage.json` is one live `/log/dataPage` response
for a two-hour window ending 2026-09-02 14:40 UTC, from the same 69 PRO as the
device-list fixture, captured with `scripts/capture-fixtures.py`. 116 rows, one
per minute with one four-minute gap. `portSpead` reads 0 in 77 rows and 65520
(0xFFF0) in 39: ports 2 to 4 carry nibble 0xF and are EMPTY on this controller.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acinfinity_exporter.backfill import (
    BackfillConfigError,
    Sample,
    decode_port_levels,
    iter_history,
    parse_labels,
    parse_time,
    render,
    resolve_config,
    samples_from_row,
    select_controllers,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dataPage.json"
CREDS = {"ACINFINITY_EMAIL": "u@example.com", "ACINFINITY_PASSWORD": "p"}


def rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())["data"]["rows"]


# --------------------------------------------------------------------- decode


def test_nibble_0xf_is_an_empty_port_and_is_skipped():
    assert decode_port_levels(0xFFF0, 4) == {"1": 0}
    assert decode_port_levels(0, 4) == {"1": 0, "2": 0, "3": 0, "4": 0}
    assert decode_port_levels(0xFFF3, 4) == {"1": 3}
    assert decode_port_levels(None, 4) == {}


def test_port_count_bounds_the_nibble_loop_because_high_bits_read_zero():
    """0xFFF0 is 16 bits. With a width of 8, ports 5 to 8 would read level 0 and publish
    four phantom ports. The width must come from the device list's devPortCount."""
    assert set(decode_port_levels(0xFFF0, 8)) == {"1", "5", "6", "7", "8"}
    assert set(decode_port_levels(0xFFF0, 4)) == {"1"}


def test_history_row_uses_vpdnums_with_the_uppercase_n():
    row = {"createTime": 100, "temperature": 2218, "humidity": 7812, "vpdNums": 58, "vpdnums": 999}
    out = {s.name: s.value for s in samples_from_row(row, "9", "acinfinity")}
    assert out["acinfinity_controller_vpd_kpa"] == pytest.approx(0.58)
    assert out["acinfinity_controller_temperature_celsius"] == pytest.approx(22.18)
    assert out["acinfinity_controller_humidity_percent"] == pytest.approx(78.12)


def test_timestamps_are_milliseconds_and_labels_carry_identity_only():
    row = {"createTime": 1788352920, "temperature": 2218, "portSpead": 0xFFF3}
    out = list(samples_from_row(row, "9", "acinfinity", port_count=4))
    assert all(s.timestamp_ms == 1788352920000 for s in out)
    levels = [s for s in out if s.name == "acinfinity_port_power_level"]
    assert len(levels) == 1
    assert levels[0].labels == (("controller_id", "9"), ("port", "1"))
    assert levels[0].value == 3.0


def test_extra_labels_are_rendered_and_identity_cannot_be_overridden():
    row = {"createTime": 1, "temperature": 100}
    sample = next(samples_from_row(row, "9", "acinfinity", (("job", "acinfinity-exporter"),)))
    assert render(sample) == (
        "acinfinity_controller_temperature_celsius"
        '{job="acinfinity-exporter",controller_id="9"} 1.0 1000'
    )
    with pytest.raises(BackfillConfigError, match="controller_id"):
        parse_labels('{"controller_id": "x"}')


def test_fixture_decodes_to_one_sample_per_field_per_row():
    samples = [s for row in rows() for s in samples_from_row(row, "9", "acinfinity", (), 4)]
    by_name: dict[str, int] = {}
    for s in samples:
        by_name[s.name] = by_name.get(s.name, 0) + 1
    assert by_name["acinfinity_controller_temperature_celsius"] == 116
    assert by_name["acinfinity_controller_humidity_percent"] == 116
    assert by_name["acinfinity_controller_vpd_kpa"] == 116
    # 77 rows with portSpead 0 give 4 ports each; 39 rows with 0xFFF0 give port 1 only.
    assert by_name["acinfinity_port_power_level"] == 77 * 4 + 39
    assert "3200" not in {render(s) for s in samples}


def test_render_escapes_label_values():
    s = Sample("m", (("a", 'x"y\\z'),), 1.0, 5)
    assert render(s) == 'm{a="x\\"y\\\\z"} 1.0 5'


# --------------------------------------------------------------------- paging


class PagingClient:
    """Serves a fixed row list through the time-cursor contract the API uses."""

    def __init__(self, all_rows, page_size):
        self._rows = sorted(all_rows, key=lambda r: r["createTime"])
        self._page = page_size
        self.requests: list[tuple[int, int]] = []

    def history_page(self, dev_id, start, end, page_size):
        self.requests.append((start, end))
        selected = [r for r in self._rows if start <= r["createTime"] <= end]
        return selected[: self._page]


def test_paging_advances_by_time_cursor_and_yields_every_row_once():
    all_rows = [{"createTime": t} for t in range(100, 100 + 25)]
    client = PagingClient(all_rows, page_size=10)
    out = [r["createTime"] for r in iter_history(client, "9", 100, 200, page_size=10)]
    assert out == list(range(100, 125))
    assert client.requests[0] == (100, 200)
    assert client.requests[1] == (110, 200)  # last createTime + 1
    assert len(client.requests) == 3


def test_paging_stops_on_a_repeated_boundary_row_rather_than_spinning():
    class StuckClient:
        def __init__(self):
            self.calls = 0

        def history_page(self, dev_id, start, end, page_size):
            self.calls += 1
            return [{"createTime": 50}] * page_size  # never advances

    client = StuckClient()
    out = list(iter_history(client, "9", 0, 100, page_size=2))
    assert out == [{"createTime": 50}]
    assert client.calls == 2


def test_paging_drops_rows_past_until():
    all_rows = [{"createTime": t} for t in (1, 2, 3, 4)]
    client = PagingClient(all_rows, page_size=10)
    assert [r["createTime"] for r in iter_history(client, "9", 1, 3)] == [1, 2, 3]


# --------------------------------------------------------------------- config


def test_since_accepts_unix_and_iso_and_defaults_iso_to_utc():
    assert parse_time("1788352920", "since") == 1788352920
    assert parse_time("2026-09-01T21:22:00Z", "since") == 1788297720
    assert parse_time("2026-09-01T00:00:00", "since") == parse_time(
        "2026-09-01T00:00:00+00:00", "since"
    )
    with pytest.raises(BackfillConfigError):
        parse_time("yesterday", "since")


def test_vm_url_is_required_unless_dry_run():
    with pytest.raises(BackfillConfigError, match="vm-url"):
        resolve_config(["--since", "1"], CREDS)
    cfg = resolve_config(["--since", "1", "--dry-run"], CREDS)
    assert cfg.dry_run and cfg.vm_url == ""


def test_until_must_follow_since():
    with pytest.raises(BackfillConfigError, match="later"):
        resolve_config(["--since", "10", "--until", "5", "--dry-run"], CREDS)


def test_env_fallbacks():
    cfg = resolve_config(
        [],
        {
            **CREDS,
            "ACINFINITY_BACKFILL_SINCE": "100",
            "ACINFINITY_BACKFILL_UNTIL": "200",
            "ACINFINITY_BACKFILL_VM_URL": "http://vm/api/v1/import/prometheus",
            "ACINFINITY_BACKFILL_LABELS": '{"job": "acinfinity-exporter"}',
            "ACINFINITY_BACKFILL_METRIC_PREFIX": "acme_",
        },
    )
    assert (cfg.since, cfg.until) == (100, 200)
    assert cfg.labels == (("job", "acinfinity-exporter"),)
    assert cfg.metric_prefix == "acme"


def test_select_controllers_by_devid_or_devcode():
    devices = [
        {"devId": "1", "devCode": "AAA", "devPortCount": 4},
        {"devId": "2", "devCode": "BBB", "devPortCount": None},
    ]
    assert select_controllers(devices, None) == [("1", 4), ("2", 8)]
    assert select_controllers(devices, "BBB") == [("2", 8)]
    assert select_controllers(devices, "1") == [("1", 4)]
    assert select_controllers(devices, "nope") == []
