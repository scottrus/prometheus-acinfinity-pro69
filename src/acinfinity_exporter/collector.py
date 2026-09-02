"""Decode one device-list response into readings, and serve them as gauges.

IDENTITY IS `controller_id` AND `port`. Nothing editable goes on a measurement
gauge. The vendor app lets a user rename a controller or a port at any time, and
a label that changes ends every series it sits on and starts a new one, with no
error and no warning. So names live on the two `*_info` gauges only:

    acinfinity_controller_info{controller_id, controller_name, ...}  1
    acinfinity_port_info{controller_id, port, port_name, vendor_port_name}  1

`port_name` is CONFIGURED, from the exporter's own settings, and falls back to
the vendor name only when no configured name exists. That is what makes a rename
in the app harmless to a dashboard that joins on `port_info`.

WHAT IS NOT PUBLISHED, ON PURPOSE. The response carries `insideTemp`,
`insideTempF`, `outsideTemp`, `outsideTempF`, `leafTemp` and five `thermal*`
fields. On a 69 PRO with no such probe they read 0, and `insideTempF` reads 3200,
which decodes to 32.00 F, which is 0 C. A gauge built from them draws a
plausible, permanent freezing line. They are excluded, and a test asserts it.
The `sensors[]` array is also not decoded: this controller returns null for it,
and its own temperature and humidity arrive in `deviceInfo` instead.

TWO FAILURE STATES, HANDLED DIFFERENTLY ON PURPOSE:

    cold   never collected            reading gauges ABSENT, success=0
    warm   collected, then failed     last known values RETAINED, success=0

A fabricated 0 C before the first success reads as healthy to a threshold rule.
A retained value after a later failure was true recently, and the timestamp
gauge says exactly how recently.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "acinfinity"

# The controller reports an open circuit on an empty port as the uint16 maximum.
# A connected load reads its real resistance: this T10 reads 5100 ohms.
OPEN_CIRCUIT_OHMS = 65535

# Fields the API divides by 100.
SCALE = 100.0

# Trend enum, as the API defines it: 0 stable, 1 rising, 2 falling.
TREND_HELP = "0 stable, 1 rising, 2 falling, as the controller reports it"

# Mode enum shared by the controller and each port (`curMode`): 1 OFF, 2 ON,
# 3 AUTO, 4 timer to on, 5 timer to off, 6 cycle, 7 schedule, 8 VPD.
MODE_HELP = "1 OFF, 2 ON, 3 AUTO, 4 timer-to-on, 5 timer-to-off, 6 cycle, 7 schedule, 8 VPD"


def _scaled(raw: Any) -> float | None:
    """Divide by 100, or return None for a missing value. Never invent a 0."""
    if raw is None:
        return None
    try:
        return float(raw) / SCALE
    except (TypeError, ValueError):
        return None


def _number(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None if raw is None else float(raw)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PortReading:
    port: str
    vendor_name: str
    power_level: float | None
    online: float | None
    connected: float | None
    resistance_ohms: float | None  # None when open circuit, never 65535
    load_state: float | None
    load_type: float | None
    mode: float | None
    overcurrent: float | None
    abnormal: float | None
    automation_active: float | None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> PortReading | None:
        port = raw.get("port")
        if port is None:
            return None
        resistance = _number(raw.get("portResistance"))
        # Absent means unknown on old firmware. Never read absence as 0 ohms.
        connected: float | None = None
        if resistance is not None:
            connected = 0.0 if resistance == OPEN_CIRCUIT_OHMS else 1.0
        return cls(
            port=str(port),
            vendor_name=str(raw.get("portName") or f"Port {port}"),
            power_level=_number(raw.get("speak")),
            online=_number(raw.get("online")),
            connected=connected,
            resistance_ohms=None if connected in (None, 0.0) else resistance,
            load_state=_number(raw.get("loadState")),
            load_type=_number(raw.get("loadType")),
            mode=_number(raw.get("curMode")),
            overcurrent=_number(raw.get("overcurrentStatus")),
            abnormal=_number(raw.get("abnormalState")),
            automation_active=_number(raw.get("isOpenAutomation")),
        )


@dataclass(frozen=True, slots=True)
class ControllerReading:
    controller_id: str
    name: str
    device_code: str
    device_type: str
    firmware_version: str
    hardware_version: str
    zone_id: str
    online: float | None
    temperature_c: float | None
    humidity_percent: float | None
    vpd_kpa: float | None
    temperature_trend: float | None
    humidity_trend: float | None
    ports: tuple[PortReading, ...] = ()

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> ControllerReading | None:
        dev_id = raw.get("devId")
        if dev_id is None:
            return None
        info = raw.get("deviceInfo") or {}
        ports = tuple(
            reading
            for reading in (PortReading.from_api(p) for p in (info.get("ports") or []))
            if reading is not None
        )
        return cls(
            controller_id=str(dev_id),
            name=str(raw.get("devName") or ""),
            device_code=str(raw.get("devCode") or ""),
            device_type=str(raw.get("devType") if raw.get("devType") is not None else ""),
            firmware_version=str(raw.get("firmwareVersion") or ""),
            hardware_version=str(raw.get("hardwareVersion") or ""),
            zone_id=str(raw.get("zoneId") or ""),
            online=_number(raw.get("online")),
            temperature_c=_scaled(info.get("temperature")),
            humidity_percent=_scaled(info.get("humidity")),
            # The LIVE field is `vpdnums`, lowercase n. History uses `vpdNums`.
            # Reading the wrong one yields a gauge that never populates.
            vpd_kpa=_scaled(info.get("vpdnums")),
            temperature_trend=_number(info.get("tTrend")),
            humidity_trend=_number(info.get("hTrend")),
            ports=ports,
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One complete collection. Replaces its predecessor; never appended to."""

    controllers: tuple[ControllerReading, ...] = ()
    collected_at: float = 0.0
    duration_seconds: float = 0.0
    success: bool = False
    warnings: tuple[str, ...] = field(default=())


def parse_devices(raw_devices: Iterable[Mapping[str, Any]]) -> tuple[ControllerReading, ...]:
    return tuple(
        reading
        for reading in (ControllerReading.from_api(d) for d in raw_devices)
        if reading is not None
    )


def collect_snapshot(
    raw_devices: Iterable[Mapping[str, Any]], started: float | None = None
) -> Snapshot:
    # `started` is when the FETCH began, so the duration covers the vendor round trip.
    began = time.time() if started is None else started
    controllers = parse_devices(raw_devices)
    warnings: list[str] = []
    if not controllers:
        warnings.append("the device list is empty: the account sees no controller")
    return Snapshot(
        controllers=controllers,
        collected_at=began,
        duration_seconds=max(0.0, time.time() - began),
        success=True,
        warnings=tuple(warnings),
    )


def resolve_port_name(
    port_names: Mapping[str, str],
    controller_id: str,
    port: str,
    vendor_name: str,
) -> str:
    """Configured name wins. `<controller_id>/<port>` beats a bare `<port>` key."""
    return port_names.get(f"{controller_id}/{port}") or port_names.get(port) or vendor_name


class ACInfinityCollector(Collector):
    """Serves the last snapshot. Does no I/O: a scrape must never reach the vendor.

    Collection runs on its own timer, in `__main__`. If scrapes drove collection,
    then a second vmagent replica or one manual curl would double the calls made
    to the vendor cloud, and the account's session would be re-created on their
    schedule rather than ours.
    """

    def __init__(
        self,
        namespace: str = DEFAULT_NAMESPACE,
        port_names: Mapping[str, str] | None = None,
    ) -> None:
        self._namespace = namespace.rstrip("_")
        self._port_names = dict(port_names or {})
        self._snapshot = Snapshot()

    def _name(self, suffix: str) -> str:
        """Every metric passes through here. A half-applied prefix is worse than none."""
        return f"{self._namespace}_{suffix}" if self._namespace else suffix

    def update(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    def mark_failure(self) -> None:
        """Keep the previous readings, but stop claiming the collection succeeded."""
        self._snapshot = replace(self._snapshot, success=False)

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def collect(self):
        snap = self._snapshot

        yield GaugeMetricFamily(
            self._name("collection_success"),
            "1 if the most recent collection attempt succeeded, 0 otherwise",
            value=1 if snap.success else 0,
        )
        yield GaugeMetricFamily(
            self._name("last_collection_timestamp_seconds"),
            "Unix time of the last SUCCESSFUL collection; 0 if none has succeeded",
            value=snap.collected_at,
        )
        yield GaugeMetricFamily(
            self._name("collection_duration_seconds"),
            "Wall-clock duration of the last successful collection",
            value=snap.duration_seconds,
        )

        # Cold start: no reading gauges at all. See the module docstring.
        if not snap.controllers:
            return

        yield from self._controller_families(snap.controllers)
        yield from self._port_families(snap.controllers)

    # ------------------------------------------------------------ controllers

    def _controller_families(self, controllers: Iterable[ControllerReading]):
        controllers = tuple(controllers)
        cid = ["controller_id"]

        info = GaugeMetricFamily(
            self._name("controller_info"),
            "Controller identity. Always 1. The editable name lives here and nowhere else",
            labels=[
                "controller_id",
                "controller_name",
                "device_code",
                "device_type",
                "firmware_version",
                "hardware_version",
                "zone_id",
            ],
        )
        for c in controllers:
            info.add_metric(
                [
                    c.controller_id,
                    c.name,
                    c.device_code,
                    c.device_type,
                    c.firmware_version,
                    c.hardware_version,
                    c.zone_id,
                ],
                1,
            )
        yield info

        families = [
            ("controller_online", "1 if the cloud reports the controller online", "online"),
            (
                "controller_temperature_celsius",
                "Temperature at the controller's own probe, degrees Celsius",
                "temperature_c",
            ),
            (
                "controller_humidity_percent",
                "Relative humidity at the controller's own probe, percent",
                "humidity_percent",
            ),
            (
                "controller_vpd_kpa",
                "Vapour pressure deficit computed by the controller, kilopascals",
                "vpd_kpa",
            ),
            (
                "controller_temperature_trend",
                f"Temperature trend: {TREND_HELP}",
                "temperature_trend",
            ),
            ("controller_humidity_trend", f"Humidity trend: {TREND_HELP}", "humidity_trend"),
        ]
        for suffix, doc, attr in families:
            family = GaugeMetricFamily(self._name(suffix), doc, labels=cid)
            for c in controllers:
                value = getattr(c, attr)
                if value is not None:
                    family.add_metric([c.controller_id], value)
            yield family

    # ------------------------------------------------------------------ ports

    def _port_families(self, controllers: Iterable[ControllerReading]):
        pairs = [(c, p) for c in controllers for p in c.ports]
        labels = ["controller_id", "port"]

        info = GaugeMetricFamily(
            self._name("port_info"),
            "Port identity. Always 1. port_name is the CONFIGURED name, or the vendor "
            "name when none is configured; vendor_port_name is what the app shows",
            labels=[*labels, "port_name", "vendor_port_name"],
        )
        for c, p in pairs:
            name = resolve_port_name(self._port_names, c.controller_id, p.port, p.vendor_name)
            info.add_metric([c.controller_id, p.port, name, p.vendor_name], 1)
        yield info

        families = [
            (
                "port_power_level",
                "Current output level of the load on this port, 0 to 10. The app "
                "calls this speed for a fan. API field `speak`",
                "power_level",
            ),
            ("port_online", "1 if the controller reports the port online", "online"),
            (
                "port_connected",
                "1 if a load is electrically present (port resistance is not the "
                "open-circuit sentinel 65535). Absent on firmware that does not report it",
                "connected",
            ),
            (
                "port_resistance_ohms",
                "Measured resistance of the connected load. ABSENT on an empty port "
                "rather than 65535",
                "resistance_ohms",
            ),
            ("port_load_state", "Load state as reported (`loadState`), 0 or 1", "load_state"),
            (
                "port_load_type",
                "Load type as reported (`loadType`). Collected because it was seen to "
                "change between reads; a history is what makes that visible",
                "load_type",
            ),
            ("port_mode", f"Current mode of the port (`curMode`): {MODE_HELP}", "mode"),
            ("port_overcurrent", "1 if the port reports an overcurrent condition", "overcurrent"),
            ("port_abnormal", "1 if the port reports an abnormal state", "abnormal"),
            (
                "port_automation_active",
                "1 if an Advance automation program controls this port (`isOpenAutomation`)",
                "automation_active",
            ),
        ]
        for suffix, doc, attr in families:
            family = GaugeMetricFamily(self._name(suffix), doc, labels=labels)
            for c, p in pairs:
                value = getattr(p, attr)
                if value is not None:
                    family.add_metric([c.controller_id, p.port], value)
            yield family
