"""One-shot history backfill: `/log/dataPage` into VictoriaMetrics.

The vendor keeps about one record per minute of history. This reads a window of
it and writes each record to `/api/v1/import/prometheus` with its ORIGINAL
timestamp, so a dashboard shows history from before the exporter's first scrape.

IT IS A SEPARATE PROGRAM, NEVER A MODE OF THE EXPORTER. A backfill materialises
tens of thousands of rows and holds a write connection to the TSDB. The exporter
holds one small snapshot and never writes anywhere. Keeping them apart keeps the
exporter's memory bound small and its network policy at ingress only.

WHAT IS WRITTEN. Only the fields that map one-to-one onto a live gauge:

    <prefix>_controller_temperature_celsius{controller_id}
    <prefix>_controller_humidity_percent{controller_id}
    <prefix>_controller_vpd_kpa{controller_id}
    <prefix>_port_power_level{controller_id, port}

`portStatus` is not written: it means "an automation triggered this port" in
history and there is no live gauge with the same meaning. Nothing else in a
history row has a live counterpart.

LABELS. Imported samples carry NO `job` or `instance` label unless `--label` adds
one. A scraped series and an imported series for the same reading are therefore
two series in the TSDB. Query with `max by (controller_id) (...)` to read them as
one. The ecobee importer in the same estate has the same shape, on purpose.

THREE API FACTS THE PAGING DEPENDS ON:

1. `pageNum` is ignored. The cursor is `time`: send the last row's `createTime`
   plus one to get the next page.
2. The history field is `vpdNums`, uppercase N. The live field is `vpdnums`.
3. `portSpead` packs one 4-bit nibble per port, port 1 in the low nibble. On a
   69 PRO the nibble reads 0xF for an EMPTY port (verified 2026-09-02 against
   ports with `portResistance` 65535), so 0xF is a sentinel and is skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from . import __version__
from .client import DEFAULT_USER_AGENT, ACInfinityClient
from .collector import DEFAULT_NAMESPACE, _scaled

log = logging.getLogger("acinfinity_backfill")

ENV_PREFIX = "ACINFINITY_BACKFILL_"
ENV_EMAIL = "ACINFINITY_EMAIL"
ENV_PASSWORD = "ACINFINITY_PASSWORD"

DEFAULT_PAGE_SIZE = 2000
# Lines per POST. Bounds one request when a long window runs.
BATCH_LINES = 10_000
# `devPortCount` is null in every history row, but it is populated in the device
# list, and that is where the nibble width comes from. Nibbles above the real
# port count read 0, not 0xF, so a fixed width of 8 would publish phantom ports.
FALLBACK_PORT_COUNT = 8
EMPTY_PORT_NIBBLE = 0xF


class BackfillConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float
    timestamp_ms: int


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(sample: Sample) -> str:
    labels = ",".join(f'{k}="{_escape(v)}"' for k, v in sample.labels)
    return f"{sample.name}{{{labels}}} {sample.value} {sample.timestamp_ms}"


def decode_port_levels(port_spead: int | None, port_count: int) -> dict[str, int]:
    """Unpack `portSpead`. Returns {port: level} for ports that carry a reading."""
    if port_spead is None:
        return {}
    levels: dict[str, int] = {}
    for index in range(port_count):
        nibble = (int(port_spead) >> (index * 4)) & 0xF
        if nibble == EMPTY_PORT_NIBBLE:
            continue
        levels[str(index + 1)] = nibble
    return levels


def samples_from_row(
    row: Mapping[str, object],
    controller_id: str,
    prefix: str,
    extra_labels: Sequence[tuple[str, str]] = (),
    port_count: int = FALLBACK_PORT_COUNT,
) -> Iterator[Sample]:
    created = row.get("createTime")
    if created is None:
        return
    ts_ms = int(created) * 1000
    base = (*extra_labels, ("controller_id", controller_id))

    def name(suffix: str) -> str:
        return f"{prefix}_{suffix}" if prefix else suffix

    for suffix, field in (
        ("controller_temperature_celsius", "temperature"),
        ("controller_humidity_percent", "humidity"),
        ("controller_vpd_kpa", "vpdNums"),
    ):
        value = _scaled(row.get(field))
        if value is not None:
            yield Sample(name(suffix), base, value, ts_ms)

    raw_spead = row.get("portSpead")
    spead = raw_spead if isinstance(raw_spead, int) else None
    for port, level in decode_port_levels(spead, port_count).items():
        yield Sample(name("port_power_level"), (*base, ("port", port)), float(level), ts_ms)


def iter_history(
    client: ACInfinityClient,
    dev_id: str,
    since: int,
    until: int,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[Mapping[str, object]]:
    """Walk the window with a time cursor. Yields rows oldest first, no duplicates."""
    cursor = since
    last_yielded = -1
    while cursor <= until:
        rows = client.history_page(dev_id, cursor, until, page_size)
        if not rows:
            return
        progressed = False
        # Sorted, so a duplicate or a stray out-of-order row cannot hide a later one.
        for row in sorted(rows, key=lambda r: r.get("createTime") or -1):
            created = row.get("createTime")
            if not isinstance(created, int):
                continue
            if created <= last_yielded:
                continue  # a repeated row, inside a page or across the boundary
            if created > until:
                return
            last_yielded = created
            progressed = True
            yield row
        if not progressed:
            return  # no forward progress; stop rather than spin
        if len(rows) < page_size:
            return
        cursor = last_yielded + 1


class VictoriaMetricsWriter:
    def __init__(self, url: str, timeout: float = 60.0, user_agent: str = DEFAULT_USER_AGENT):
        self._url = url
        self._timeout = timeout
        self._user_agent = user_agent

    def write(self, samples: Iterable[Sample]) -> int:
        """POST in batches. Raises on the first failed batch; nothing is retried here."""
        written = 0
        batch: list[str] = []
        for sample in samples:
            batch.append(render(sample))
            if len(batch) >= BATCH_LINES:
                self._post(batch)
                written += len(batch)
                batch = []
        if batch:
            self._post(batch)
            written += len(batch)
        return written

    def _post(self, lines: list[str]) -> None:
        payload = ("\n".join(lines) + "\n").encode()
        request = urllib.request.Request(
            self._url,
            data=payload,
            method="POST",
            headers={"Content-Type": "text/plain", "User-Agent": self._user_agent},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.status >= 300:
                    raise RuntimeError(f"import returned http={response.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"import returned http={exc.code}: {exc.read()[:200]!r}") from exc
        log.info("wrote %d lines to %s", len(lines), self._url)


def parse_time(raw: str, name: str) -> int:
    """Unix seconds, or ISO 8601. A date with no offset is read as UTC."""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackfillConfigError(f"{name} must be unix seconds or ISO 8601, got {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def parse_labels(raw: str | None) -> tuple[tuple[str, str], ...]:
    if not raw or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise BackfillConfigError(f"labels must be a JSON object, got {raw!r}") from exc
    if not isinstance(parsed, dict) or not all(isinstance(v, str) for v in parsed.values()):
        raise BackfillConfigError("labels must be a JSON object of string values")
    if "controller_id" in parsed or "port" in parsed:
        raise BackfillConfigError("labels may not override controller_id or port")
    return tuple(sorted((str(k), v) for k, v in parsed.items()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acinfinity-backfill",
        description="Import AC Infinity history into VictoriaMetrics with original timestamps.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--since", default=None, help=f"Window start [{ENV_PREFIX}SINCE]")
    parser.add_argument(
        "--until", default=None, help=f"Window end, default now [{ENV_PREFIX}UNTIL]"
    )
    parser.add_argument(
        "--vm-url",
        default=None,
        help=f"VictoriaMetrics import URL, .../api/v1/import/prometheus [{ENV_PREFIX}VM_URL]",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help=f'JSON object of extra labels, e.g. \'{{"job":"acinfinity-exporter"}}\' '
        f"[{ENV_PREFIX}LABELS]",
    )
    parser.add_argument(
        "--metric-prefix",
        default=None,
        help=f"default {DEFAULT_NAMESPACE!r} [{ENV_PREFIX}METRIC_PREFIX]",
    )
    parser.add_argument(
        "--controller",
        default=None,
        help=f"Restrict to one devId or devCode; default every controller [{ENV_PREFIX}CONTROLLER]",
    )
    parser.add_argument(
        "--page-size", default=None, help=f"default {DEFAULT_PAGE_SIZE} [{ENV_PREFIX}PAGE_SIZE]"
    )
    parser.add_argument("--user-agent", default=None, help=f"[{ENV_PREFIX}USER_AGENT]")
    parser.add_argument("--log-level", default=None, help=f"[{ENV_PREFIX}LOG_LEVEL]")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the window and print a summary and the first lines; write nothing",
    )
    return parser


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    email: str
    password: str
    since: int
    until: int
    vm_url: str
    labels: tuple[tuple[str, str], ...]
    metric_prefix: str
    controller: str | None
    page_size: int
    user_agent: str
    log_level: str
    dry_run: bool


def resolve_config(argv: Sequence[str], environ: Mapping[str, str]) -> BackfillConfig:
    args = build_parser().parse_args(list(argv))

    def pick(flag_value: str | None, env_suffix: str) -> str | None:
        return flag_value if flag_value is not None else environ.get(ENV_PREFIX + env_suffix)

    email = environ.get(ENV_EMAIL)
    password = environ.get(ENV_PASSWORD)
    if not email or not password:
        raise BackfillConfigError(
            f"credentials are required and are read from the environment ONLY: "
            f"set {ENV_EMAIL} and {ENV_PASSWORD}"
        )
    since_raw = pick(args.since, "SINCE")
    if not since_raw:
        raise BackfillConfigError(f"--since is required [{ENV_PREFIX}SINCE]")
    since = parse_time(since_raw, "since")
    until_raw = pick(args.until, "UNTIL")
    until = parse_time(until_raw, "until") if until_raw else int(time.time())
    if until <= since:
        raise BackfillConfigError("until must be later than since")
    vm_url = pick(args.vm_url, "VM_URL")
    if not vm_url and not args.dry_run:
        raise BackfillConfigError(f"--vm-url is required unless --dry-run [{ENV_PREFIX}VM_URL]")
    page_raw = pick(args.page_size, "PAGE_SIZE")
    try:
        page_size = int(page_raw) if page_raw else DEFAULT_PAGE_SIZE
    except ValueError as exc:
        raise BackfillConfigError(f"page size must be an integer, got {page_raw!r}") from exc
    if page_size <= 0:
        raise BackfillConfigError("page size must be positive")
    prefix = pick(args.metric_prefix, "METRIC_PREFIX")
    return BackfillConfig(
        email=email,
        password=password,
        since=since,
        until=until,
        vm_url=vm_url or "",
        labels=parse_labels(pick(args.labels, "LABELS")),
        metric_prefix=(DEFAULT_NAMESPACE if prefix is None else prefix).rstrip("_"),
        controller=pick(args.controller, "CONTROLLER") or None,
        page_size=page_size,
        user_agent=pick(args.user_agent, "USER_AGENT") or DEFAULT_USER_AGENT,
        log_level=(pick(args.log_level, "LOG_LEVEL") or "INFO").upper(),
        dry_run=bool(args.dry_run),
    )


def select_controllers(
    devices: Sequence[Mapping[str, object]], wanted: str | None
) -> list[tuple[str, int]]:
    """(devId, port count) for every matching controller."""
    selected: list[tuple[str, int]] = []
    for device in devices:
        dev_id = device.get("devId")
        if dev_id is None:
            continue
        if wanted and wanted not in (str(dev_id), str(device.get("devCode") or "")):
            continue
        raw_count = device.get("devPortCount")
        count = (
            int(raw_count) if isinstance(raw_count, int) and raw_count > 0 else FALLBACK_PORT_COUNT
        )
        selected.append((str(dev_id), count))
    return selected


def _controller_samples(
    client: ACInfinityClient,
    config: BackfillConfig,
    dev_id: str,
    port_count: int,
    counter: list[int],
) -> Iterator[Sample]:
    """Every sample for one controller. `counter[0]` counts rows as they stream."""
    for row in iter_history(client, dev_id, config.since, config.until, config.page_size):
        counter[0] += 1
        yield from samples_from_row(row, dev_id, config.metric_prefix, config.labels, port_count)


def run(
    config: BackfillConfig, client: ACInfinityClient, writer: VictoriaMetricsWriter | None
) -> int:
    devices = client.list_devices()
    controllers = select_controllers(devices, config.controller)
    if not controllers:
        log.error("no controller matched %r among %d device(s)", config.controller, len(devices))
        return 1

    total_rows = 0
    total_samples = 0
    for dev_id, port_count in controllers:
        log.info(
            "controller %s (%d ports): window %d..%d",
            dev_id,
            port_count,
            config.since,
            config.until,
        )
        rows_seen = [0]
        samples = _controller_samples(client, config, dev_id, port_count, rows_seen)
        if writer is None:
            preview: list[str] = []
            count = 0
            for sample in samples:
                count += 1
                if len(preview) < 8:
                    preview.append(render(sample))
            print(f"controller {dev_id}: {rows_seen[0]} rows -> {count} samples (dry run)")
            for line in preview:
                print("  " + line)
        else:
            count = writer.write(samples)
            log.info("controller %s: %d rows -> %d samples written", dev_id, rows_seen[0], count)
        total_rows += rows_seen[0]
        total_samples += count

    log.info(
        "done: %d rows, %d samples, %d controller(s)", total_rows, total_samples, len(controllers)
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = resolve_config(sys.argv[1:] if argv is None else argv, os.environ)
    except BackfillConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = ACInfinityClient(config.email, config.password, user_agent=config.user_agent)
    writer = (
        None
        if config.dry_run
        else VictoriaMetricsWriter(config.vm_url, user_agent=config.user_agent)
    )
    try:
        return run(config, client, writer)
    except Exception as exc:  # a one-shot Job: fail loudly, exit non-zero
        log.error("backfill failed: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
