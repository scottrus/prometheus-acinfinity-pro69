# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Renaming or removing a metric is a major version bump.** It silently breaks
dashboards and alerting rules downstream.

**Adding a vendor API call is a privilege-surface change.** A new *read* is a minor
bump, called out under its own heading with [SECURITY.md](SECURITY.md) updated in the
same change. Anything that *writes* would be major, and is not a direction this
project intends to take.

## [Unreleased]

## [0.1.0] - 2026-09-02

First release. A fresh implementation, with `LukeEvansTech/acinfinity-exporter` as the
reference; see the README for the ten findings that shaped it.

### Added

- **The exporter.** Polls `devInfoListAll` on a timer, serves `/metrics` and `/health`,
  and publishes controller readings and per-port state with `controller_id` and `port`
  as the only labels on measurement gauges.
- **Configured port names.** `ACINFINITY_EXPORTER_PORT_NAMES`, a JSON object, sets
  `port_name` on `acinfinity_port_info`; the vendor's name is carried beside it as
  `vendor_port_name`. A rename in the app changes nothing a dashboard reads.
- **Re-login on any non-success body code.** The API signals an expired session as
  HTTP 200 plus a body code, and the token rotates on every login.
- **A login backoff** of 300 s after a refusal, so a wrong password is one attempt per
  five minutes rather than one per poll.
- **The backfill**, `acinfinity-backfill`: pages `/log/dataPage` with a time cursor and
  writes to `/api/v1/import/prometheus` with original timestamps. Skips nibble `0xF`
  in `portSpead`, which marks an empty port on a 69 PRO, and bounds the nibble loop by
  the controller's `devPortCount` so high bits do not publish phantom ports.
- **The Helm chart**, with `VMServiceScrape` and `ServiceMonitor` support, `envFrom` on
  the Secret, `portNames` rendered into the ConfigMap, and an optional backfill Job.
- **Fixtures captured live on 2026-09-02** from a 69 PRO on firmware 3.2.56, redacted
  by `scripts/capture-fixtures.py`.

### Deliberately absent

- `insideTemp`, `insideTempF`, `outsideTemp`, `outsideTempF`, `leafTemp` and every
  `thermal*` field: placeholders on a probe-less controller that decode to a plausible
  freezing line.
- `temperatureF`: one unit per measurement.
- The `sensors[]` array: null on this hardware.
- Any write path.
