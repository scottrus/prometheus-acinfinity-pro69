"""Decode and exposition, replayed offline against a captured device list.

FIXTURE PROVENANCE. `fixtures/devInfoListAll.json` is a live `devInfoListAll`
response from a UIS Controller 69 PRO (devType 11, firmware 3.2.56) with a
CLOUDLINE T10 on port 1 and ports 2 to 4 empty, captured 2026-09-02 with
`scripts/capture-fixtures.py`. Account email, Wi-Fi name, MAC address and the
vendor's opaque keys are redacted. It pins the format as it was on that date; a
passing test does not prove the live API still returns that shape.

The humidity in the capture reads 76.72 % because the probe had been submerged
the day before. That is the sensor, not a decode error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from acinfinity_exporter.collector import (
    OPEN_CIRCUIT_OHMS,
    ACInfinityCollector,
    ControllerReading,
    PortReading,
    collect_snapshot,
    parse_devices,
    resolve_port_name,
)

FIXTURE = Path(__file__).parent / "fixtures" / "devInfoListAll.json"
CONTROLLER_ID = "1424979258063671880"


def load_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text())["data"]


@pytest.fixture
def controller() -> ControllerReading:
    readings = parse_devices(load_fixture())
    assert len(readings) == 1
    return readings[0]


def families(collector) -> dict:
    return {f.name: f for f in collector.collect()}


def samples(collector, name: str) -> list:
    return families(collector)[name].samples


# -------------------------------------------------------------------- decode


def test_controller_identity_and_scaled_readings(controller):
    assert controller.controller_id == CONTROLLER_ID
    assert controller.name == "AC CLOUDLINE T10"
    assert controller.device_code == "NSZQB"
    assert controller.device_type == "11"
    assert controller.firmware_version == "3.2.56"
    assert controller.hardware_version == "1.1"
    assert controller.zone_id == "America/New_York"
    assert controller.online == 1
    assert controller.temperature_c == pytest.approx(21.04)
    assert controller.humidity_percent == pytest.approx(76.72)
    assert controller.vpd_kpa == pytest.approx(0.58)  # from `vpdnums`, lowercase n
    assert controller.temperature_trend == 2
    assert controller.humidity_trend == 1


def test_vpd_reads_the_lowercase_live_field_not_the_history_spelling():
    """Upstream read `vpd`; the field is `vpdnums`. A wrong name yields no gauge, ever."""
    raw = {"devId": "9", "deviceInfo": {"vpdNums": 164, "vpdnums": 58}}
    reading = ControllerReading.from_api(raw)
    assert reading.vpd_kpa == pytest.approx(0.58)


def test_ports_decode_with_the_fan_on_port_1_and_three_empty_ports(controller):
    assert [p.port for p in controller.ports] == ["1", "2", "3", "4"]
    fan = controller.ports[0]
    assert fan.vendor_name == "Port 1"
    assert fan.connected == 1
    assert fan.resistance_ohms == 5100
    assert fan.power_level == 0
    assert fan.mode == 7  # schedule
    assert fan.online == 1
    assert fan.load_type == 0
    for empty in controller.ports[1:]:
        assert empty.connected == 0
        assert empty.resistance_ohms is None, "65535 must never be published as ohms"
        assert empty.online == 0


def test_absent_resistance_is_unknown_not_zero_ohms():
    """Old firmware omits `portResistance`. Absence must not read as a short circuit."""
    port = PortReading.from_api({"port": 1, "portName": "x"})
    assert port.connected is None
    assert port.resistance_ohms is None


def test_open_circuit_sentinel_value():
    assert OPEN_CIRCUIT_OHMS == 65535


def test_missing_reading_is_absent_not_zero():
    raw = {"devId": "9", "deviceInfo": {"temperature": None, "humidity": 5000}}
    reading = ControllerReading.from_api(raw)
    assert reading.temperature_c is None
    assert reading.humidity_percent == 50.0


def test_device_without_devid_is_skipped():
    assert parse_devices([{"devName": "ghost"}]) == ()


# ------------------------------------------------------------------ port names


def test_configured_port_name_wins_over_the_vendor_default():
    names = {"1": "Closet exhaust T10"}
    assert resolve_port_name(names, CONTROLLER_ID, "1", "Port 1") == "Closet exhaust T10"
    assert resolve_port_name(names, CONTROLLER_ID, "2", "Port 2") == "Port 2"


def test_controller_scoped_port_name_beats_a_bare_port_key():
    names = {"1": "generic", f"{CONTROLLER_ID}/1": "specific"}
    assert resolve_port_name(names, CONTROLLER_ID, "1", "Port 1") == "specific"
    assert resolve_port_name(names, "other", "1", "Port 1") == "generic"


def test_port_info_carries_both_the_configured_and_the_vendor_name():
    collector = ACInfinityCollector(port_names={"1": "Closet exhaust T10"})
    collector.update(collect_snapshot(load_fixture()))
    info = {s.labels["port"]: s.labels for s in samples(collector, "acinfinity_port_info")}
    assert info["1"]["port_name"] == "Closet exhaust T10"
    assert info["1"]["vendor_port_name"] == "Port 1"
    assert info["2"]["port_name"] == "Port 2"


def test_names_appear_on_info_gauges_only():
    """A rename must not end a measurement series. Identity is controller_id and port."""
    collector = ACInfinityCollector()
    collector.update(collect_snapshot(load_fixture()))
    for family in collector.collect():
        if family.name.endswith("_info"):
            continue
        for sample in family.samples:
            assert set(sample.labels) <= {"controller_id", "port"}, family.name


# --------------------------------------------------------------- exclusions


def test_placeholder_temperature_fields_are_never_published():
    """`insideTempF` reads 3200 (= 0 C) on a probe-less controller. A gauge from it
    draws a plausible, permanent freezing line."""
    collector = ACInfinityCollector()
    collector.update(collect_snapshot(load_fixture()))
    names = set(families(collector))
    banned = ("inside", "outside", "leaf", "thermal", "sensor_")
    offenders = {n for n in names if any(b in n for b in banned)}
    assert offenders == set(), offenders
    text = generate_latest(_registry_with(collector)).decode()
    assert "3200" not in text
    assert " 32.0" not in text


def _registry_with(collector):
    registry = CollectorRegistry()
    registry.register(collector)
    return registry


# -------------------------------------------------------------- exposition


def test_cold_start_publishes_meta_metrics_only():
    names = set(families(ACInfinityCollector()))
    assert names == {
        "acinfinity_collection_success",
        "acinfinity_last_collection_timestamp_seconds",
        "acinfinity_collection_duration_seconds",
    }


def test_failure_after_success_retains_last_known_values():
    collector = ACInfinityCollector()
    collector.update(collect_snapshot(load_fixture()))
    collector.mark_failure()
    fam = families(collector)
    assert fam["acinfinity_collection_success"].samples[0].value == 0
    assert fam["acinfinity_controller_temperature_celsius"].samples[0].value == pytest.approx(21.04)


def test_exposition_renders_as_valid_prometheus_text():
    collector = ACInfinityCollector()
    collector.update(collect_snapshot(load_fixture()))
    text = generate_latest(_registry_with(collector)).decode()
    assert "# TYPE acinfinity_controller_vpd_kpa gauge" in text
    assert f'acinfinity_controller_vpd_kpa{{controller_id="{CONTROLLER_ID}"}} 0.58' in text
    assert (
        f'acinfinity_port_resistance_ohms{{controller_id="{CONTROLLER_ID}",port="1"}} 5100.0'
        in text
    )
    assert 'port="2"} 65535' not in text
    assert "acinfinity_collection_success 1.0" in text
    assert "# HELP acinfinity_port_power_level" in text


def test_every_metric_family_respects_the_prefix():
    collector = ACInfinityCollector(namespace="acme")
    collector.update(collect_snapshot(load_fixture()))
    names = set(families(collector))
    assert names
    assert {n for n in names if not n.startswith("acme_")} == set()


def test_series_count_is_about_fifty_for_one_controller_with_four_ports():
    """The plan sized the TSDB impact at about 50 series. Assert the order of magnitude."""
    collector = ACInfinityCollector()
    collector.update(collect_snapshot(load_fixture()))
    count = sum(len(f.samples) for f in collector.collect())
    assert 40 <= count <= 60, count


def test_empty_device_list_is_a_successful_collection_with_a_warning():
    snapshot = collect_snapshot([])
    assert snapshot.success
    assert snapshot.controllers == ()
    assert any("empty" in w for w in snapshot.warnings)
